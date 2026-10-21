"""
MCP gateway: let an LLM work with your data without ever seeing the real values.

Everything a tool returns is sent to your LLM provider. This server therefore
fakes the data once, at startup, and every tool only ever reads that faked copy.
The mapping back to the real values stays in this process (and, if you ask, in
a private file on this machine); no tool can return it.

Design rules, each of which has a test:

- **Fail closed.** A column, nested field or JSON key reaches the model only if
  it was faked or you named it in `keep`. Anything the name-based detector
  missed is dropped, not passed through. (The CLI's `process` command does the
  opposite, which suits a file you review; it does not suit a live tool.)
- **No paths from the model.** Datasets are registered by you under an alias.
- **No tool returns real values.** Restoring writes to a local folder and
  returns only a count.
- **Errors never echo data.** Library errors quote cell values (Polars does),
  so every tool turns unexpected failures into one generic message and logs
  just the exception type.
- **Everything is read-only and capped.**
"""

import functools
import hmac
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence, Set

import polars as pl
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle
from pii_faker.core.file_io import FileHandler
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.processor import PIIProcessor
from pii_faker.core.restorer import PIIRestorer
from pii_faker.web.auth import MIN_TOKEN_LENGTH, generate_token

logger = logging.getLogger("pii_faker.mcp")

TOKEN_ENV = "PII_FAKER_MCP_TOKEN"
DEFAULT_MAX_ROWS = 100
DEFAULT_MAX_CELL_CHARS = 500
MAX_RESTORE_CHARS = 1_000_000

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DROP = object()          # sentinel: nothing of this JSON node may be shown

GENERIC_ERROR = "The request could not be completed."

INSTRUCTIONS = (
    "Personal details in these datasets (names, contacts, ids, dates of birth, addresses) have been "
    "replaced with realistic but synthetic stand-ins. Columns marked faked=true hold synthetic values; "
    "faked=false columns hold the original values. Do not try to work out who anyone really is. "
    "Quote the synthetic values exactly as they appear when you report findings, so the data owner can "
    "map them back to the real ones afterwards."
)


class GatewayError(Exception):
    """A failure whose message is safe to show the model (it never contains data)."""


# ---------------------------------------------------------------------------
# What may be exposed
# ---------------------------------------------------------------------------

def _norm(path: str, columns: Optional[Iterable[str]] = None) -> str:
    """Canonical form of a path: lists are transparent, `[*]` is dropped."""
    return ".".join(JsonWalker.segments(path, columns))


def _norm_key(key: str, columns: Iterable[str]) -> str:
    """Canonical form of a mapping key or --keep item (`column:json.path` is kept as such)."""
    if ":" in key and key not in columns:
        column, path = key.split(":", 1)
        return f"{column}:{_norm(path)}"
    return _norm(key, columns)


def _prefixes(path: str) -> List[str]:
    """`a.b.c` -> a, a.b, a.b.c. For `col:x.y` -> col, col:x, col:x.y."""
    head, sep, rest = path.partition(":")
    if sep:
        parts = rest.split(".")
        return [head] + [f"{head}:{'.'.join(parts[:i + 1])}" for i in range(len(parts))]
    parts = path.split(".")
    return [".".join(parts[:i + 1]) for i in range(len(parts))]


@dataclass
class Policy:
    """Decides, per leaf path, whether the model may see it."""

    processed: Set[str] = field(default_factory=set)   # faked: safe to show
    explicit: Set[str] = field(default_factory=set)    # named in --keep (a column or any prefix of the path)
    blocked: Set[str] = field(default_factory=set)     # matched as PII but could not be faked: never shown
    keep_unmatched: bool = False

    def allows(self, path: str) -> bool:
        if any(prefix in self.explicit for prefix in _prefixes(path)):
            return True
        if path in self.processed:
            return True
        return self.keep_unmatched and path not in self.blocked


def _prune_native(expr: pl.Expr, dtype: pl.DataType, path: str, policy: Policy) -> Optional[pl.Expr]:
    """Rebuild a (possibly nested) column keeping only allowed leaves, or None if nothing survives."""
    if isinstance(dtype, pl.List):
        inner = _prune_native(pl.element(), dtype.inner, path, policy)
        return None if inner is None else expr.list.eval(inner)

    if isinstance(dtype, pl.Struct):
        parts = []
        for f in dtype.fields:
            child = _prune_native(expr.struct.field(f.name), f.dtype, f"{path}.{f.name}", policy)
            if child is not None:
                parts.append(child.alias(f.name))
        return pl.struct(parts) if parts else None

    return expr if policy.allows(path) else None


def _prune_document(node: Any, path: str, column: str, policy: Policy) -> Any:
    """Keep only the allowed keys of a parsed JSON document (_DROP if none survive)."""
    if isinstance(node, dict):
        kept = {}
        for key, value in node.items():
            sub = f"{path}.{key}" if path else key
            result = _prune_document(value, sub, column, policy)
            if result is not _DROP:
                kept[key] = result
        return kept if kept else _DROP
    if isinstance(node, list):
        items = [r for r in (_prune_document(i, path, column, policy) for i in node) if r is not _DROP]
        return items if items else _DROP
    return node if policy.allows(f"{column}:{path}") else _DROP


def _prune_json_column(df: pl.DataFrame, column: str, policy: Policy) -> Optional[pl.Expr]:
    """Prune the JSON documents in a text column. Unparseable documents become null."""
    rewritten: Dict[str, Optional[str]] = {}
    for text, doc in JsonWalker.parse_json_documents(df, column).items():
        pruned = _prune_document(doc, "", column, policy)
        rewritten[text] = None if pruned is _DROP else _dumps(pruned)
    if not any(v is not None for v in rewritten.values()):
        return None
    return pl.col(column).replace_strict(rewritten, default=None, return_dtype=pl.String)


def _dumps(value: Any) -> str:
    import json
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _leaf_paths(schema: Dict[str, pl.DataType]) -> Set[str]:
    """Canonical path of every leaf in a (possibly nested) schema."""
    leaves: Set[str] = set()

    def walk(dtype: pl.DataType, path: str) -> None:
        while isinstance(dtype, pl.List):
            dtype = dtype.inner
        if isinstance(dtype, pl.Struct):
            for f in dtype.fields:
                walk(f.dtype, f"{path}.{f.name}")
        else:
            leaves.add(path)

    for name, dtype in schema.items():
        walk(dtype, name)
    return leaves


# ---------------------------------------------------------------------------
# The gateway
# ---------------------------------------------------------------------------

@dataclass
class DatasetReport:
    """What a dataset exposes. Column names only, for the data owner: never shown to the model."""

    name: str
    rows: int
    faked: List[str]
    kept: List[str]
    dropped: List[str]
    pruned: List[str]
    blocked: List[str]


@dataclass
class ExposedDataset:
    name: str
    frame: pl.DataFrame
    faked_columns: Set[str]
    report: DatasetReport


class Aggregation(BaseModel):
    """One aggregate to compute."""

    op: Literal["count", "n_unique", "sum", "mean", "min", "max"]
    column: Optional[str] = None


def parse_dataset_specs(specs: Sequence[str]) -> Dict[str, Path]:
    """Parse `name=path` items. The model never chooses these; the data owner does."""
    datasets: Dict[str, Path] = {}
    for spec in specs:
        name, sep, raw_path = spec.partition("=")
        if not sep or not raw_path:
            raise ValueError(f"Dataset '{spec}' must look like name=path/to/file.csv")
        if not _NAME.match(name):
            raise ValueError(f"Dataset name '{name}' may only use letters, digits, '_' and '-' (max 64).")
        if name in datasets:
            raise ValueError(f"Dataset name '{name}' is used twice.")
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ValueError(f"Dataset file not found: {path}")
        datasets[name] = path
    if not datasets:
        raise ValueError("Give at least one --dataset name=path.")
    return datasets


class Gateway:
    """Fakes datasets once and answers read-only queries about the faked copy."""

    def __init__(
        self,
        config: PIIConfig,
        keep: Sequence[str] = (),
        keep_unmatched: bool = False,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_cell_chars: int = DEFAULT_MAX_CELL_CHARS,
        restore_dir: Optional[Path] = None,
    ):
        if config.mode == PIIMode.REDACT and config.redact_style == RedactionStyle.PARTIAL:
            raise ValueError(
                "Partial redaction keeps the last 4 characters of every value, which is not safe to "
                "send to an untrusted provider. Use fake mode, or --redact-style full/token."
            )
        self.config = config
        self.keep = [k.strip() for k in keep if k.strip()]
        self.keep_unmatched = keep_unmatched
        self.max_rows = max(1, int(max_rows))
        self.max_cell_chars = max_cell_chars
        self.processor = PIIProcessor(config)
        self.datasets: Dict[str, ExposedDataset] = {}
        self.restore_dir: Optional[Path] = None
        self._restorer: Optional[PIIRestorer] = None

        if restore_dir is not None:
            if not self.processor.mapper.is_lossless_mode():
                raise ValueError("--restore-dir needs fake mode or --redact-style token: full redaction can't be reversed.")
            self.restore_dir = Path(restore_dir).expanduser()
            self.restore_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.restore_dir, 0o700)

    # -- loading ----------------------------------------------------------

    def add_dataset(self, name: str, source: Any) -> DatasetReport:
        """Load, fake and prune a dataset. `source` is a file path or a DataFrame."""
        if not _NAME.match(name):
            raise ValueError(f"Dataset name '{name}' may only use letters, digits, '_' and '-' (max 64).")
        if name in self.datasets:
            raise ValueError(f"Dataset name '{name}' is already registered.")

        df = source if isinstance(source, pl.DataFrame) else FileHandler(self.config).read(str(source))
        if isinstance(df, dict):                       # a workbook: first sheet only
            df = next(iter(df.values()))

        skipped_before = len(self.processor.skipped)
        entries = self.processor.detect(df)
        faked = self.processor.process(df, entries)
        skipped = self.processor.skipped[skipped_before:]

        columns = list(df.columns)
        policy = Policy(
            processed={_norm_key(e["column"], columns) for e in entries if e["column"] not in skipped},
            explicit={_norm_key(k, columns) for k in self.keep},
            blocked={_norm_key(k, columns) for k in skipped},
            keep_unmatched=self.keep_unmatched,
        )

        exposed = self._expose(faked, policy)
        if exposed.width == 0:
            raise ValueError(
                f"Nothing in '{name}' would be exposed: no column was recognised as PII and none is "
                "named in --keep. Run with --dry-run to review."
            )

        faked_columns = {JsonWalker.segments(e["column"], columns)[0] if ":" not in e["column"]
                         else e["column"].split(":", 1)[0]
                         for e in entries if e["column"] not in skipped} & set(exposed.columns)

        report = DatasetReport(
            name=name,
            rows=exposed.height,
            faked=sorted(e["column"] for e in entries if e["column"] not in skipped),
            kept=[c for c in exposed.columns if c not in faked_columns],
            dropped=[c for c in df.columns if c not in exposed.columns],
            pruned=sorted(_leaf_paths({c: df.schema[c] for c in exposed.columns})
                          - _leaf_paths(dict(exposed.schema))),
            blocked=sorted(skipped),
        )
        self.datasets[name] = ExposedDataset(name, exposed, faked_columns, report)
        self._restorer = None
        return report

    def _expose(self, df: pl.DataFrame, policy: Policy) -> pl.DataFrame:
        json_columns = set(self.config.json_columns)
        selected = []
        for name, dtype in df.schema.items():
            if name in json_columns and dtype == pl.String:
                expr = _prune_json_column(df, name, policy)
            else:
                expr = _prune_native(pl.col(name), dtype, name, policy)
            if expr is not None:
                selected.append(expr.alias(name))
        return df.select(selected) if selected else df.select([])

    def reports(self) -> List[DatasetReport]:
        return [d.report for d in self.datasets.values()]

    def save_mapping(self, path: str) -> None:
        """Write the real<->fake mapping to a private (0600) file on this machine."""
        self.processor.mapper.save(path)

    # -- queries (all on the faked copy) -----------------------------------

    def _dataset(self, name: str) -> ExposedDataset:
        dataset = self.datasets.get(name)
        if dataset is None:
            raise GatewayError(f"Unknown dataset {name!r}. Available: {', '.join(sorted(self.datasets)) or 'none'}.")
        return dataset

    @staticmethod
    def _need_columns(dataset: ExposedDataset, names: Iterable[str], scalar: bool = False) -> None:
        available = dataset.frame.schema
        unknown = [n for n in names if n not in available]
        if unknown:
            raise GatewayError(
                f"Unknown column(s): {', '.join(map(repr, unknown))}. Available: {', '.join(available)}."
            )
        if scalar:
            nested = [n for n in names if available[n].is_nested()]
            if nested:
                raise GatewayError(f"Column(s) {', '.join(map(repr, nested))} hold nested data; use read_rows instead.")

    def _clamp(self, limit: int) -> int:
        return max(1, min(int(limit), self.max_rows))

    def _cell(self, value: Any) -> Any:
        if isinstance(value, str):
            return value if len(value) <= self.max_cell_chars else value[: self.max_cell_chars] + "…"
        if isinstance(value, dict):
            return {k: self._cell(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._cell(v) for v in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)                               # dates, decimals, bytes...

    def _rows(self, frame: pl.DataFrame) -> List[Dict[str, Any]]:
        return [{k: self._cell(v) for k, v in row.items()} for row in frame.to_dicts()]

    def list_datasets(self) -> Dict[str, Any]:
        return {
            "datasets": [
                {"name": d.name, "rows": d.frame.height, "columns": d.frame.width}
                for d in self.datasets.values()
            ]
        }

    def describe(self, name: str) -> Dict[str, Any]:
        dataset = self._dataset(name)
        frame = dataset.frame
        columns = []
        for column, dtype in frame.schema.items():
            columns.append({
                "name": column,
                "type": str(dtype),
                "faked": column in dataset.faked_columns,
                "nulls": frame[column].null_count(),
                "unique": None if dtype.is_nested() else frame[column].n_unique(),
            })
        return {
            "dataset": name,
            "rows": frame.height,
            "columns": columns,
            "sample": self._rows(frame.head(5)),
        }

    def read_rows(
        self, name: str, offset: int = 0, limit: int = 20, columns: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        dataset = self._dataset(name)
        if columns:
            self._need_columns(dataset, columns)
        offset = max(0, int(offset))
        limit = self._clamp(limit)
        window = dataset.frame.select(columns) if columns else dataset.frame
        total = window.height
        return self._page(name, window.slice(offset, limit), offset, total)

    def search(
        self,
        name: str,
        column: str,
        value: str,
        match: str = "equals",
        case_sensitive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        dataset = self._dataset(name)
        self._need_columns(dataset, [column], scalar=True)
        if match not in ("equals", "contains"):
            raise GatewayError("match must be 'equals' or 'contains'.")

        text = pl.col(column).cast(pl.String)
        needle = value
        if not case_sensitive:
            text, needle = text.str.to_lowercase(), value.lower()
        condition = text == needle if match == "equals" else text.str.contains(needle, literal=True)

        found = dataset.frame.filter(condition)
        offset = max(0, int(offset))
        return self._page(name, found.slice(offset, self._clamp(limit)), offset, found.height)

    def aggregate(
        self,
        name: str,
        group_by: Sequence[str] = (),
        aggregations: Sequence[Aggregation] = (),
        limit: int = 50,
    ) -> Dict[str, Any]:
        dataset = self._dataset(name)
        group_by = list(group_by)
        self._need_columns(dataset, group_by, scalar=True)

        exprs, names = [], set(group_by)
        for agg in aggregations or [Aggregation(op="count")]:
            if agg.op == "count":
                alias, expr = "count", pl.len()
            else:
                if not agg.column:
                    raise GatewayError(f"'{agg.op}' needs a column.")
                self._need_columns(dataset, [agg.column], scalar=True)
                dtype = dataset.frame.schema[agg.column]
                if agg.op in ("sum", "mean") and not dtype.is_numeric():
                    raise GatewayError(f"'{agg.op}' needs a numeric column; {agg.column!r} is {dtype}.")
                alias, expr = f"{agg.op}_{agg.column}", getattr(pl.col(agg.column), agg.op)()
            if alias in names:
                raise GatewayError(f"Result column {alias!r} would appear twice.")
            names.add(alias)
            exprs.append(expr.alias(alias))

        result = dataset.frame.group_by(group_by).agg(exprs).sort(group_by) if group_by \
            else dataset.frame.select(exprs)
        return self._page(name, result.head(self._clamp(limit)), 0, result.height)

    def _page(self, name: str, frame: pl.DataFrame, offset: int, total: int) -> Dict[str, Any]:
        return {
            "dataset": name,
            "columns": frame.columns,
            "rows": self._rows(frame),
            "offset": offset,
            "returned": frame.height,
            "total": total,
            "has_more": offset + frame.height < total,
        }

    # -- restoring (never returns real values) -------------------------------

    @property
    def can_restore(self) -> bool:
        return self.restore_dir is not None

    def restore_to_file(self, text: str, filename: str = "restored.txt") -> Dict[str, Any]:
        """Put the real values back into `text` and save it locally. Returns only a count."""
        if self.restore_dir is None:
            raise GatewayError("Restoring is not enabled on this server.")
        if len(text) > MAX_RESTORE_CHARS:
            raise GatewayError(f"Text is too long (limit {MAX_RESTORE_CHARS:,} characters).")

        if self._restorer is None:
            self._restorer = PIIRestorer(self.processor.mapper)
        restored, replacements = self._restorer.restore_text_counted(text)

        target = self._free_path(_safe_filename(filename))
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(restored)
        return {"saved_as": target.name, "replacements": replacements}

    def _free_path(self, name: str) -> Path:
        path = self.restore_dir / name
        stem, suffix = path.stem, path.suffix
        for n in range(1, 1000):
            if not path.exists():
                return path
            path = self.restore_dir / f"{stem}-{n}{suffix}"
        raise GatewayError("Too many saved files with that name.")


def _safe_filename(filename: str) -> str:
    """A plain `.txt` file name: no directories, no dot-files, no odd characters."""
    name = Path((filename or "").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).lstrip(".")[:80]
    if not name:
        name = "restored"
    return name if name.endswith(".txt") else f"{name}.txt"


# ---------------------------------------------------------------------------
# The MCP server
# ---------------------------------------------------------------------------

def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Make a tool leak-proof: only our own messages reach the model."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except GatewayError as error:
            raise ToolError(str(error)) from None
        except Exception as error:
            # Never log str(error) or a traceback: Polars quotes the offending cell.
            logger.error("Tool %s failed (%s)", fn.__name__, type(error).__name__)
            raise ToolError(GENERIC_ERROR) from None

    return wrapper


def build_server(gateway: Gateway) -> MCPServer:
    """Register the read-only tools for an already-loaded gateway."""
    server = MCPServer("pii-faker", instructions=INSTRUCTIONS)
    read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=False)

    @server.tool(annotations=read_only)
    @_safe
    def list_datasets() -> Dict[str, Any]:
        """List the available datasets with their row and column counts."""
        return gateway.list_datasets()

    @server.tool(annotations=read_only)
    @_safe
    def describe_dataset(dataset: str) -> Dict[str, Any]:
        """Show a dataset's columns (type, whether the values are synthetic, null and distinct counts) and a 5-row sample."""
        return gateway.describe(dataset)

    @server.tool(annotations=read_only)
    @_safe
    def read_rows(dataset: str, offset: int = 0, limit: int = 20, columns: Optional[List[str]] = None) -> Dict[str, Any]:
        """Read a page of rows, optionally only some columns. `limit` is capped by the server."""
        return gateway.read_rows(dataset, offset, limit, columns)

    @server.tool(annotations=read_only)
    @_safe
    def search_rows(
        dataset: str,
        column: str,
        value: str,
        match: Literal["equals", "contains"] = "equals",
        case_sensitive: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Find rows where a (non-nested) column equals or contains a value. Searches the synthetic data."""
        return gateway.search(dataset, column, value, match, case_sensitive, offset, limit)

    @server.tool(annotations=read_only)
    @_safe
    def aggregate(
        dataset: str,
        group_by: List[str] = [],
        aggregations: List[Aggregation] = [],
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Group rows and compute count / n_unique / sum / mean / min / max. With no aggregations it counts rows."""
        return gateway.aggregate(dataset, group_by, aggregations, limit)

    if gateway.can_restore:
        @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                                 idempotent_hint=False, open_world_hint=False))
        @_safe
        def restore_to_file(text: str, filename: str = "restored.txt") -> Dict[str, Any]:
            """Put the real values back into text that quotes synthetic values, and save it on the data owner's
            machine. Returns only the file name and a count: the real text is never sent back to you."""
            return gateway.restore_to_file(text, filename)

    return server


# ---------------------------------------------------------------------------
# HTTP transport (remote / tunnel)
# ---------------------------------------------------------------------------

class BearerAuthMiddleware:
    """Reject every HTTP request that doesn't carry the access token. Wraps the whole app."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._token = token.encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scheme, _, credentials = Headers(scope=scope).get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(credentials.strip().encode(), self._token):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            {"error": "unauthorized"}, status_code=401,
            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        )
        await response(scope, receive, send)


def build_http_app(server: MCPServer, token: str, host: str, port: int, allowed_hosts: Sequence[str] = ()) -> Any:
    """The streamable-HTTP app: token required, Host/Origin checked (DNS-rebinding protection)."""
    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(f"The access token must be at least {MIN_TOKEN_LENGTH} characters long.")

    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        hosts.add(f"{host}:{port}")
    for allowed in allowed_hosts:
        allowed = allowed.strip().lower()
        if allowed:
            hosts.update({allowed, f"{allowed}:*"})

    origins = set()
    for h in hosts:
        origins.update({f"http://{h}", f"https://{h}"})

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )
    return BearerAuthMiddleware(server.streamable_http_app(host=host, transport_security=security), token)


def run_gateway(
    gateway: Gateway,
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8765,
    token: Optional[str] = None,
    allowed_hosts: Sequence[str] = (),
    announce: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
) -> None:
    """Serve the gateway. `announce` must not write to stdout: stdio carries the protocol."""
    server = build_server(gateway)

    if transport == "stdio":
        server.run("stdio")
        return

    if transport != "http":
        raise ValueError("transport must be 'stdio' or 'http'.")

    import uvicorn

    token = token or os.environ.get(TOKEN_ENV) or None
    generated = token is None
    token = token or generate_token()
    app = build_http_app(server, token, host, port, allowed_hosts)

    shown = "localhost" if host in ("0.0.0.0", "::") else host
    announce(f"MCP endpoint: http://{shown}:{port}/mcp")
    announce(f"Send the header  Authorization: Bearer {token}" if generated
             else "Send your access token as  Authorization: Bearer <token>")
    if host not in ("127.0.0.1", "localhost", "::1"):
        announce(f"Warning: listening on {host} over plain HTTP, so the token and the (faked) data are "
                 "unencrypted on the network. Use a TLS tunnel or proxy in front.")
    if allowed_hosts:
        announce(f"Accepting requests addressed to: {', '.join(allowed_hosts)}")

    uvicorn.run(app, host=host, port=port, log_level="warning")
