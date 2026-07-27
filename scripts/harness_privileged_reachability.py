"""AST inventory for direct privileged Python callsites."""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
PROCESS_CALLS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.popen",
    "os.system",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.run",
}
NETWORK_CONSTRUCTORS = {
    "aiohttp.ClientSession",
    "boto3.client",
    "httpcore.AsyncConnectionPool",
    "httpx.AsyncClient",
    "httpx.Client",
    "requests.Session",
    "socket.create_connection",
    "socket.socket",
}
NETWORK_REQUEST_CALLS = {
    "aiohttp.delete",
    "aiohttp.get",
    "aiohttp.patch",
    "aiohttp.post",
    "aiohttp.put",
    "aiohttp.request",
    "httpx.delete",
    "httpx.get",
    "httpx.patch",
    "httpx.post",
    "httpx.put",
    "httpx.request",
    "requests.delete",
    "requests.get",
    "requests.patch",
    "requests.post",
    "requests.put",
    "requests.request",
    "urllib.request.urlopen",
}


class ReachabilityScanError(ValueError):
    """A configured source root cannot be scanned deterministically."""


@dataclass(frozen=True, slots=True)
class PrivilegedCall:
    path: str
    symbol: str
    sink: str
    ordinal: int

    @property
    def callsite_id(self) -> str:
        return f"{self.path}#{self.symbol}:{self.sink}:{self.ordinal}"


class _SinkVisitor(ast.NodeVisitor):
    def __init__(self, path: str, aliases: dict[str, str]) -> None:
        self.path = path
        self.aliases = aliases
        self.scope: list[str] = []
        self.counts: Counter[tuple[str, str]] = Counter()
        self.calls: list[PrivilegedCall] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _call_name(node.func, self.aliases)
        sink = _sink_for_call(call_name, path=self.path)
        if sink is not None:
            self._record(sink)
        self.generic_visit(node)

    def _visit_scope(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self.scope.append(node.name)
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            call_name = _call_name(decorator.func, self.aliases)
            method = call_name.rsplit(".", maxsplit=1)[-1].lower()
            if method in HTTP_METHODS and self.path.startswith("app/api/"):
                self._record(f"api.route.{method.upper()}")
        self.generic_visit(node)
        self.scope.pop()

    def _record(self, sink: str) -> None:
        symbol = ".".join(self.scope) or "<module>"
        counter_key = (symbol, sink)
        self.counts[counter_key] += 1
        self.calls.append(
            PrivilegedCall(
                path=self.path,
                symbol=symbol,
                sink=sink,
                ordinal=self.counts[counter_key],
            )
        )


def discover_privileged_calls(
    scan_roots: tuple[PurePosixPath, ...],
    *,
    root: Path = ROOT,
) -> tuple[PrivilegedCall, ...]:
    calls: list[PrivilegedCall] = []
    for scan_root in scan_roots:
        directory = root / scan_root
        if not directory.is_dir():
            raise ReachabilityScanError(
                f"scan root does not exist: {scan_root}"
            )
        for source_path in sorted(directory.rglob("*.py")):
            relative_path = source_path.relative_to(root).as_posix()
            tree = _parse_source(source_path, relative_path)
            visitor = _SinkVisitor(relative_path, _import_aliases(tree))
            visitor.visit(tree)
            calls.extend(visitor.calls)
    return tuple(sorted(calls, key=lambda call: call.callsite_id))


def defined_symbols(source_path: Path) -> set[str]:
    tree = _parse_source(source_path, source_path.as_posix())
    symbols: set[str] = set()
    scope: list[str] = []

    class SymbolVisitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._visit_scope(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_scope(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_scope(node)

        def _visit_scope(
            self,
            node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ) -> None:
            scope.append(node.name)
            symbols.add(".".join(scope))
            self.generic_visit(node)
            scope.pop()

    SymbolVisitor().visit(tree)
    return symbols


def _parse_source(source_path: Path, label: str) -> ast.Module:
    try:
        return ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as error:
        raise ReachabilityScanError(
            f"cannot scan privileged path: {label}"
        ) from error


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_root = alias.name.split(".", maxsplit=1)[0]
                local_name = alias.asname or imported_root
                aliases[local_name] = (
                    alias.name if alias.asname else imported_root
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = (
                    f"{node.module}.{alias.name}"
                )
    return aliases


def _call_name(node: ast.expr, aliases: dict[str, str]) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(aliases.get(current.id, current.id))
    return ".".join(reversed(parts))


def _sink_for_call(name: str, *, path: str) -> str | None:
    if name in PROCESS_CALLS or name.startswith("os.exec"):
        return "process.create"
    if name.startswith("os.spawn"):
        return "process.create"
    if name in NETWORK_CONSTRUCTORS:
        return "network.client"
    if name in NETWORK_REQUEST_CALLS:
        return "network.request"
    if name.endswith(".connect_tcp") or name.endswith(".connect_udp"):
        return "network.connect"
    if name.endswith(".getaddrinfo"):
        return "network.dns_lookup"
    if name.endswith(".converse_stream"):
        return "network.bedrock_stream"
    if name == "google.auth.default":
        return "network.credential_discovery"
    if path.endswith("/providers/vertex_credentials.py") and name.endswith(
        ".refresh"
    ):
        return "network.credential_refresh"
    return None
