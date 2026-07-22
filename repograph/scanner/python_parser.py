"""Python source scanner.

Walks a repository, parses every .py file with the stdlib `ast` module and
extracts a semantic model:

  Entities  : module, function (incl. methods), class, api (decorated route
              handlers), llm_call (detected calls to LLM provider SDKs)
  Relations : contains, imports, calls, inherits

Call resolution is best-effort and deliberately conservative: an edge is only
created when the target can be resolved through imports, the local module, the
enclosing class (self.x), or a project-wide unique name.
"""

from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from typing import Optional

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env",
    "node_modules", ".repograph", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".tox", ".eggs",
}

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "options", "head"}
ROUTE_ATTRS = HTTP_METHODS | {"route", "websocket", "api_route"}

LLM_MODULE_HINTS = (
    "openai", "anthropic", "fireworks", "litellm", "cohere", "groq",
    "mistralai", "google.generativeai", "ollama", "together",
)
LLM_CALL_SUFFIXES = (
    "chat.completions.create", "completions.create", "messages.create",
    "generate_content", "litellm.completion", "responses.create",
)
NETWORK_HINTS = ("requests", "httpx", "urllib", "aiohttp", "socket")

RISK_PATTERNS = {
    "eval_exec": ("eval", "exec"),
    "subprocess": ("subprocess.", "os.system", "os.popen"),
    "deserialization": ("pickle.load", "pickle.loads", "yaml.load", "marshal.load"),
    "secrets_access": ("os.environ", "os.getenv", "getenv("),
    "sql_raw": ("execute(", "executemany(", "executescript("),
}


@dataclass
class Entity:
    id: str
    kind: str          # module | function | class | api | llm_call
    name: str          # short display name
    qualname: str      # fully qualified name within the repo
    file: str          # repo-relative path
    line: int = 0
    end_line: int = 0
    snippet: str = ""
    hash: str = ""     # content hash of the full source segment
    meta: dict = field(default_factory=dict)


@dataclass
class Relation:
    src: str
    dst: str
    kind: str          # contains | imports | calls | inherits


@dataclass
class ScanResult:
    root: str
    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    files_scanned: int = 0
    parse_errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def dotted_name(node: ast.AST) -> str:
    """Best-effort dotted name for a call target / attribute chain."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return dotted_name(node.func)
    return ""


def module_qualname(root: str, path: str) -> str:
    rel = os.path.relpath(path, root)
    rel = rel[:-3] if rel.endswith(".py") else rel
    parts = rel.replace("\\", "/").split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1] or [os.path.basename(root)]
    return ".".join(p for p in parts if p)


def snippet_of(lines: list[str], start: int, end: int, limit: int = 4000) -> str:
    text = "".join(lines[max(start - 1, 0):end])
    return text[:limit]


def content_hash(lines: list[str], start: int, end: int) -> str:
    """Hash of the FULL source segment (snippet may be truncated)."""
    text = "".join(lines[max(start - 1, 0):end])
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


_SQL_KEYWORDS = ("select ", "insert ", "update ", "delete ", "where ",
                 "from ", "drop ", "create table")


def _is_sql_text(value) -> bool:
    return isinstance(value, str) and any(kw in value.lower() for kw in _SQL_KEYWORDS)


def _looks_like_sql(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and _is_sql_text(node.value)


# --------------------------------------------------------------------------- #
# per-module visitor
# --------------------------------------------------------------------------- #

class ModuleVisitor(ast.NodeVisitor):
    def __init__(self, mod_qual: str, rel_path: str, lines: list[str]):
        self.mod_qual = mod_qual
        self.rel_path = rel_path
        self.lines = lines
        self.mod_id = f"mod:{mod_qual}"

        self.entities: list[Entity] = []
        self.relations: list[Relation] = []
        # alias -> imported dotted path ("np" -> "numpy", "cfg" -> "app.config")
        self.imports: dict[str, str] = {}
        self.imported_modules: set[str] = set()
        # (caller_entity_id, dotted_call_name, line)
        self.pending_calls: list[tuple[str, str, int]] = []
        # local/instance variable -> class name it was assigned from
        #   service = TriageService(...)   ->  {"service": "TriageService"}
        #   self.repo = TicketRepository() ->  {"self.repo": "TicketRepository"}
        self.var_types: dict[str, str] = {}
        # (class_entity_id, base_dotted_name)
        self.pending_bases: list[tuple[str, str]] = []
        self._scope: list[tuple[str, str]] = []  # (kind, qualname) stack

    # -- scope helpers ------------------------------------------------------ #
    @property
    def scope_qual(self) -> str:
        return self._scope[-1][1] if self._scope else self.mod_qual

    @property
    def scope_id(self) -> str:
        if not self._scope:
            return self.mod_id
        kind, qual = self._scope[-1]
        return f"{'cls' if kind == 'class' else 'fn'}:{qual}"

    def enclosing_class(self) -> Optional[str]:
        for kind, qual in reversed(self._scope):
            if kind == "class":
                return qual
        return None

    def enclosing_function_id(self) -> Optional[str]:
        for kind, qual in reversed(self._scope):
            if kind == "function":
                return f"fn:{qual}"
        return None

    # -- imports ------------------------------------------------------------ #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            self.imports[local] = alias.name
            self.imported_modules.add(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = "." * node.level + (node.module or "")
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self.imports[local] = f"{base}.{alias.name}" if base else alias.name
        if node.module:
            self.imported_modules.add(base)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Track `var = SomeClass(...)` so `var.method()` can resolve later.
        if isinstance(node.value, ast.Call):
            ctor = dotted_name(node.value.func)
            cls = ctor.split(".")[-1]
            if cls and cls[:1].isupper():          # heuristic: class names are Capitalised
                for tgt in node.targets:
                    key = self._assign_key(tgt)
                    if key:
                        self.var_types[key] = cls
        self.generic_visit(node)

    def _assign_key(self, tgt: ast.AST) -> Optional[str]:
        if isinstance(tgt, ast.Name):
            return tgt.id
        if isinstance(tgt, ast.Attribute):
            base = dotted_name(tgt.value)
            return f"{base}.{tgt.attr}" if base else None
        return None

    # -- definitions -------------------------------------------------------- #
    def _route_decorator(self, node) -> Optional[dict]:
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            name = dotted_name(target)
            last = name.split(".")[-1].lower()
            if last in ROUTE_ATTRS and "." in name:
                path = ""
                if isinstance(dec, ast.Call) and dec.args:
                    first = dec.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        path = first.value
                method = last.upper() if last in HTTP_METHODS else "ROUTE"
                return {"http_method": method, "route": path, "framework_obj": name.split(".")[0]}
        return None

    def _handle_function(self, node) -> None:
        qual = f"{self.scope_qual}.{node.name}"
        route = self._route_decorator(node)
        kind = "api" if route else "function"
        ent_id = f"fn:{qual}"
        end = getattr(node, "end_lineno", node.lineno)
        meta: dict = {
            "docstring": bool(ast.get_docstring(node)),
            "async": isinstance(node, ast.AsyncFunctionDef),
            "signals": self._risk_signals(node),
        }
        if route:
            meta.update(route)
        self.entities.append(Entity(
            id=ent_id, kind=kind, name=node.name, qualname=qual,
            file=self.rel_path, line=node.lineno, end_line=end,
            snippet=snippet_of(self.lines, node.lineno, end),
            hash=content_hash(self.lines, node.lineno, end), meta=meta,
        ))
        self.relations.append(Relation(self.scope_id, ent_id, "contains"))

        self._scope.append(("function", qual))
        self._collect_calls(node, ent_id)
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._handle_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qual = f"{self.scope_qual}.{node.name}"
        ent_id = f"cls:{qual}"
        end = getattr(node, "end_lineno", node.lineno)
        self.entities.append(Entity(
            id=ent_id, kind="class", name=node.name, qualname=qual,
            file=self.rel_path, line=node.lineno, end_line=end,
            snippet=snippet_of(self.lines, node.lineno, min(end, node.lineno + 40)),
            hash=content_hash(self.lines, node.lineno, end),
            meta={"docstring": bool(ast.get_docstring(node))},
        ))
        self.relations.append(Relation(self.scope_id, ent_id, "contains"))
        for base in node.bases:
            name = dotted_name(base)
            if name:
                self.pending_bases.append((ent_id, name))

        self._scope.append(("class", qual))
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        self._scope.pop()

    # -- calls & signals ----------------------------------------------------- #
    def _collect_calls(self, fn_node, ent_id: str) -> None:
        for sub in ast.walk(fn_node):
            if isinstance(sub, ast.Call):
                name = dotted_name(sub.func)
                if name:
                    self.pending_calls.append((ent_id, name, sub.lineno))
                self._maybe_llm_call(ent_id, name, sub.lineno)

    # Method names that actually invoke a model (vs. constructing a client).
    _LLM_METHODS = ("create", "generate_content", "complete", "completion",
                    "stream", "invoke", "generate")

    def _maybe_llm_call(self, caller_id: str, name: str, line: int) -> None:
        if not name:
            return
        root = name.split(".")[0]
        resolved_root = self.imports.get(root, root)
        last = name.split(".")[-1]
        # A known generation call path (e.g. chat.completions.create), OR a
        # call into a provider module whose method actually runs inference.
        # Constructing a client (openai.OpenAI(...)) is NOT an LLM call.
        from_provider = any(resolved_root.startswith(h) for h in LLM_MODULE_HINTS)
        is_llm = (
            any(name.endswith(sfx) for sfx in LLM_CALL_SUFFIXES)
            or (from_provider and last in self._LLM_METHODS)
        )
        if not is_llm:
            return
        llm_id = f"llm:{caller_id.split(':', 1)[1]}:{line}"
        if not any(e.id == llm_id for e in self.entities):
            self.entities.append(Entity(
                id=llm_id, kind="llm_call", name=name.split(".")[-2] + "." + name.split(".")[-1]
                if "." in name else name,
                qualname=name, file=self.rel_path, line=line, end_line=line,
                snippet=snippet_of(self.lines, line, line),
                hash=content_hash(self.lines, line, line),
                meta={"call": name},
            ))
            self.relations.append(Relation(caller_id, llm_id, "calls"))

    def _risk_signals(self, fn_node) -> list[str]:
        """Detect risk signals by inspecting the AST, not by substring-scanning
        source text. This avoids false positives like `self._execute(...)`
        matching the `exec` rule or a var named `evaluate` matching `eval`."""
        found: set[str] = set()

        for sub in ast.walk(fn_node):
            if isinstance(sub, ast.Call):
                target = dotted_name(sub.func)
                last = target.split(".")[-1]

                # eval/exec: only the builtins, as bare names
                if isinstance(sub.func, ast.Name) and sub.func.id in ("eval", "exec"):
                    found.add("eval_exec")
                # subprocess / os command execution
                if target.startswith("subprocess.") or target in ("os.system", "os.popen"):
                    found.add("subprocess")
                # unsafe deserialization
                if target in ("pickle.load", "pickle.loads", "yaml.load",
                              "marshal.load", "marshal.loads"):
                    found.add("deserialization")
                # secrets access
                if target in ("os.getenv",) or target.startswith("os.environ"):
                    found.add("secrets_access")
                # raw SQL execution (method name, whole-word)
                if last in ("execute", "executemany", "executescript"):
                    found.add("sql_raw")

            # SQL string built with %-formatting or f-strings / concatenation:
            # a strong smell of injection even when .execute is a layer away.
            if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
                if _looks_like_sql(sub.left):
                    found.add("sql_raw")
            if isinstance(sub, ast.JoinedStr):        # f-string
                if any(isinstance(v, ast.Constant) and _is_sql_text(v.value)
                       for v in sub.values):
                    found.add("sql_raw")

            # os.environ[...] subscription, and os.environ.get(...)
            if isinstance(sub, ast.Attribute):
                if dotted_name(sub).startswith("os.environ"):
                    found.add("secrets_access")

            # network libraries actually imported in this function's scope
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                mod = (sub.module if isinstance(sub, ast.ImportFrom)
                       else sub.names[0].name if sub.names else "") or ""
                if any(mod.split(".")[0] == h for h in NETWORK_HINTS):
                    found.add("network")

        # network calls via module-qualified name (requests.get, httpx.post…),
        # counting the module only if it was imported at file scope.
        for sub in ast.walk(fn_node):
            if isinstance(sub, ast.Call):
                root = dotted_name(sub.func).split(".")[0]
                resolved = self.imports.get(root, root)
                if resolved.split(".")[0] in NETWORK_HINTS:
                    found.add("network")

        return sorted(found)


# --------------------------------------------------------------------------- #
# repo scan + cross-file resolution
# --------------------------------------------------------------------------- #

def iter_python_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(filenames):
            if fname.endswith(".py"):
                yield os.path.join(dirpath, fname)


def scan_repository(root: str) -> ScanResult:
    root = os.path.abspath(root)
    result = ScanResult(root=root)
    visitors: list[ModuleVisitor] = []

    for path in iter_python_files(root):
        rel = os.path.relpath(path, root).replace("\\", "/")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            tree = ast.parse(source)
        except SyntaxError as exc:
            result.parse_errors.append(f"{rel}: {exc}")
            continue

        result.files_scanned += 1
        mod_qual = module_qualname(root, path)
        lines = source.splitlines(keepends=True)
        visitor = ModuleVisitor(mod_qual, rel, lines)

        result.entities.append(Entity(
            id=visitor.mod_id, kind="module", name=mod_qual.split(".")[-1],
            qualname=mod_qual, file=rel, line=1, end_line=len(lines),
            hash=content_hash(lines, 1, len(lines)),
            meta={"loc": len(lines)},
        ))
        visitor.visit(tree)
        visitors.append(visitor)
        result.entities.extend(visitor.entities)
        result.relations.extend(visitor.relations)

    _resolve_cross_file(result, visitors)
    _dedupe_relations(result)
    return result


def _resolve_cross_file(result: ScanResult, visitors: list[ModuleVisitor]) -> None:
    by_qual: dict[str, str] = {}          # qualname -> entity id
    by_short: dict[str, list[str]] = {}   # short name -> [entity ids]
    module_ids: dict[str, str] = {}       # module qualname -> id
    class_qual_by_name: dict[str, str] = {}   # ClassName -> class qualname (unique only)
    _class_name_seen: dict[str, int] = {}
    for ent in result.entities:
        if ent.kind == "module":
            module_ids[ent.qualname] = ent.id
        if ent.kind in ("function", "api", "class"):
            by_qual[ent.qualname] = ent.id
            by_short.setdefault(ent.name, []).append(ent.id)
        if ent.kind == "class":
            _class_name_seen[ent.name] = _class_name_seen.get(ent.name, 0) + 1
            class_qual_by_name[ent.name] = ent.qualname
    # drop ambiguous class names (same name in two modules) to stay conservative
    for cname, count in _class_name_seen.items():
        if count > 1:
            class_qual_by_name.pop(cname, None)

    def resolve(visitor: ModuleVisitor, name: str) -> Optional[str]:
        parts = name.split(".")
        head, rest = parts[0], parts[1:]

        # self.method / cls.method inside a class
        if head in ("self", "cls") and rest:
            cls_qual = visitor_class_lookup(visitor, name)
            if cls_qual:
                return by_qual.get(cls_qual)

        # typed local/instance variable: var.method() or self.attr.method()
        # where we recorded  var = SomeClass(...) / self.attr = SomeClass(...)
        if rest:
            for depth in (2, 1):                       # try self.attr then var
                if len(parts) > depth:
                    recv = ".".join(parts[:depth])
                    method = parts[depth]
                    cls = visitor.var_types.get(recv)
                    if cls:
                        target = class_qual_by_name.get(cls)
                        if target:
                            cand = f"{target}.{method}"
                            if cand in by_qual:
                                return by_qual[cand]

        # imported symbol or module
        if head in visitor.imports:
            target = visitor.imports[head]
            if target.startswith("."):
                target = _resolve_relative(visitor.mod_qual, target)
            candidate = ".".join([target] + rest) if rest else target
            if candidate in by_qual:
                return by_qual[candidate]
            if candidate in module_ids:
                return module_ids[candidate]
            # from pkg import func  ->  pkg.func recorded directly
            if target in by_qual and not rest:
                return by_qual[target]
            # Head is an import that resolved to nothing internal: this is a
            # call into an external library. Do not guess.
            return None

        # same-module symbol
        local = f"{visitor.mod_qual}.{name}"
        if local in by_qual:
            return by_qual[local]
        if head != name:
            local_head = f"{visitor.mod_qual}.{head}"
            if local_head in by_qual and rest:
                nested = f"{local_head}.{'.'.join(rest)}"
                if nested in by_qual:
                    return by_qual[nested]

        # unique project-wide short name — only for bare names, so external
        # attribute calls (lib.run) never bind to internal symbols by accident
        if len(parts) == 1:
            matches = by_short.get(name, [])
            if len(matches) == 1:
                return matches[0]
        return None

    def visitor_class_lookup(visitor: ModuleVisitor, name: str) -> Optional[str]:
        # map self.foo -> <EnclosingClass>.foo using recorded scopes is not
        # available post-hoc, so approximate: try every class in this module.
        method = name.split(".", 1)[1]
        for ent in result.entities:
            if ent.kind == "class" and ent.file == visitor.rel_path:
                qual = f"{ent.qualname}.{method.split('.')[0]}"
                if qual in by_qual:
                    return qual
        return None

    for visitor in visitors:
        # import edges (only for modules inside the repo)
        for target in visitor.imported_modules:
            resolved = _resolve_relative(visitor.mod_qual, target) if target.startswith(".") else target
            if resolved in module_ids and module_ids[resolved] != visitor.mod_id:
                result.relations.append(Relation(visitor.mod_id, module_ids[resolved], "imports"))
            else:  # pkg.sub -> match by prefix
                for mq, mid in module_ids.items():
                    if mq == resolved or mq.endswith("." + resolved):
                        if mid != visitor.mod_id:
                            result.relations.append(Relation(visitor.mod_id, mid, "imports"))
                        break

        for caller_id, name, _line in visitor.pending_calls:
            target_id = resolve(visitor, name)
            if target_id and target_id != caller_id:
                result.relations.append(Relation(caller_id, target_id, "calls"))

        for cls_id, base_name in visitor.pending_bases:
            target_id = resolve(visitor, base_name)
            if target_id and target_id != cls_id:
                result.relations.append(Relation(cls_id, target_id, "inherits"))


def _resolve_relative(mod_qual: str, target: str) -> str:
    level = len(target) - len(target.lstrip("."))
    base_parts = mod_qual.split(".")[: -level] if level else mod_qual.split(".")
    tail = target.lstrip(".")
    return ".".join([p for p in base_parts if p] + ([tail] if tail else []))


def _dedupe_relations(result: ScanResult) -> None:
    seen: set[tuple[str, str, str]] = set()
    unique: list[Relation] = []
    ids = {e.id for e in result.entities}
    for rel in result.relations:
        key = (rel.src, rel.dst, rel.kind)
        if key in seen or rel.src not in ids or rel.dst not in ids:
            continue
        seen.add(key)
        unique.append(rel)
    result.relations = unique
