"""
PII mapping management module.

Handles mapping storage, persistence, and lookup for reversible PII transformation.
Supports JSON for small mappings and SQLite for large ones.

Mapping format v2: entries are keyed by (scope, column) so multi-sheet runs
don't overwrite each other, values keep their native types (int, date, ...),
and the session ID plus per-entry dtype metadata are persisted.
Version 1 files are loaded transparently.
"""

import datetime
import json
import os
import sqlite3
import uuid
from datetime import datetime as dt_datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Union, Tuple

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle
from pii_faker import __version__ as TOOL_VERSION

MAPPING_VERSION = "2.0"


def _encode_value(value: Any) -> Dict[str, Any]:
    """Encode a native value into a JSON/SQLite-safe tagged dict."""
    if value is None:
        return {"t": "null", "v": None}
    if isinstance(value, bool):
        return {"t": "bool", "v": value}
    if isinstance(value, int):
        return {"t": "int", "v": value}
    if isinstance(value, float):
        return {"t": "float", "v": value}
    if isinstance(value, str):
        return {"t": "str", "v": value}
    if isinstance(value, dt_datetime):
        return {"t": "datetime", "v": value.isoformat()}
    if isinstance(value, datetime.date):
        return {"t": "date", "v": value.isoformat()}
    if isinstance(value, datetime.time):
        return {"t": "time", "v": value.isoformat()}
    raise TypeError(f"Unsupported mapping value type: {type(value).__name__}")


def _decode_value(data: Dict[str, Any]) -> Any:
    """Decode a tagged dict back to its native value."""
    kind = data["t"]
    val = data["v"]
    if kind == "null":
        return None
    if kind == "bool":
        return bool(val)
    if kind == "int":
        return int(val)
    if kind == "float":
        return float(val)
    if kind == "str":
        return str(val)
    if kind == "datetime":
        return dt_datetime.fromisoformat(val)
    if kind == "date":
        return datetime.date.fromisoformat(val)
    if kind == "time":
        return datetime.time.fromisoformat(val)
    raise ValueError(f"Unknown encoded value type: {kind}")


class AmbiguousMappingError(ValueError):
    """Two different originals share one replacement, so restore cannot be exact."""


def _open_private(path: str):
    """Open `path` for writing as a file only the owner can read.

    Mapping files hold real PII. The mode is applied before any data is
    written, including when the file already exists with looser permissions.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):  # not available on Windows
        pass
    return os.fdopen(fd, "w")


def _make_private(path: str) -> None:
    """Make sure `path` exists and is owner-only, without truncating it."""
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
    try:
        os.chmod(path, 0o600)
    except OSError:  # e.g. Windows
        pass


def _entry_key(scope: str, column: str) -> Tuple[str, str]:
    return (scope or "", column)


class MappingStore:
    """Storage backend for PII mappings."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self._entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._metadata: Dict[str, Any] = {}

    def add_mapping(
        self,
        column: str,
        category: str,
        mapping: Dict[Any, Any],
        scope: str = "",
        kind: str = "flat",
        path: str = "",
        original_dtype: Optional[str] = None,
        processed_dtype: Optional[str] = None,
    ):
        """Add a column mapping, scoped so multi-sheet runs don't collide."""
        reverse = {v: k for k, v in mapping.items()}
        self._entries[_entry_key(scope, column)] = {
            "scope": scope or "",
            "column": column,
            "category": category,
            "kind": kind,
            "path": path or "",
            "original_dtype": original_dtype,
            "processed_dtype": processed_dtype,
            "mapping": dict(mapping),
            "bijective": len(reverse) == len(mapping),
        }

    def get_mapping(self, column: str, scope: str = "") -> Optional[Dict[Any, Any]]:
        """Get mapping for a column within a scope."""
        entry = self._entries.get(_entry_key(scope, column))
        if entry:
            return entry.get("mapping")
        return None

    def get_entry(self, column: str, scope: str = "") -> Optional[Dict[str, Any]]:
        """Get the full mapping entry (metadata + mapping)."""
        return self._entries.get(_entry_key(scope, column))

    def entries_for_scope(self, scope: str = "") -> Dict[str, Dict[str, Any]]:
        """Get all entries for one scope as {column: entry}."""
        scope = scope or ""
        return {
            col: entry
            for (entry_scope, col), entry in self._entries.items()
            if entry_scope == scope
        }

    def scopes(self) -> List[str]:
        """List all scopes present in the store."""
        seen = []
        for entry_scope, _ in self._entries:
            if entry_scope not in seen:
                seen.append(entry_scope)
        return seen

    def get_all_mappings(self) -> Dict[str, Dict[Any, Any]]:
        """Get all mappings as {column: {original: replacement}} (scope '')."""
        result = {}
        for (entry_scope, col), entry in self._entries.items():
            if entry_scope == "":
                result[col] = entry.get("mapping", {})
        return result

    def get_reverse_mapping(self) -> Dict[Any, Any]:
        """Get reverse mapping: {replacement: original} for all entries."""
        reverse = {}
        for entry in self._entries.values():
            mapping = entry.get("mapping", {})
            for original, replacement in mapping.items():
                reverse[replacement] = original
        return reverse

    def get_reverse_mapping_by_column(self) -> Dict[str, Dict[Any, Any]]:
        """Get reverse mapping organized by column (scope '' only)."""
        result = {}
        for (entry_scope, col), entry in self._entries.items():
            if entry_scope != "":
                continue
            mapping = entry.get("mapping", {})
            result[col] = {v: k for k, v in mapping.items()}
        return result

    def to_dict(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Serialize to dict (format v2)."""
        mappings = []
        for entry in self._entries.values():
            mappings.append({
                "scope": entry["scope"],
                "column": entry["column"],
                "category": entry["category"],
                "kind": entry.get("kind", "flat"),
                "path": entry.get("path", ""),
                "original_dtype": entry.get("original_dtype"),
                "processed_dtype": entry.get("processed_dtype"),
                "pairs": [
                    [_encode_value(o), _encode_value(r)]
                    for o, r in entry["mapping"].items()
                ],
            })
        return {
            "version": MAPPING_VERSION,
            "tool": "pii-faker",
            "tool_version": TOOL_VERSION,
            "session_id": session_id,
            "created_at": dt_datetime.now().isoformat(),
            "config": {
                "mode": self.config.mode,
                "redact_style": self.config.redact_style,
                "match_strategy": self.config.match_strategy,
                "seed": self.config.seed,
            },
            "mappings": mappings,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], config: Optional[PIIConfig] = None) -> 'MappingStore':
        """Deserialize from dict (accepts v1 and v2)."""
        if config is None:
            config_data = data.get("config", {})
            config = PIIConfig(**config_data)

        store = cls(config)
        raw = data.get("mappings", {})

        if isinstance(raw, dict):
            store._load_v1_mappings(raw)
        else:
            for item in raw:
                pairs = item.get("pairs", [])
                mapping = {}
                for orig_enc, repl_enc in pairs:
                    try:
                        mapping[_decode_value(orig_enc)] = _decode_value(repl_enc)
                    except (ValueError, KeyError, TypeError):
                        continue
                reverse = {v: k for k, v in mapping.items()}
                store._entries[_entry_key(item.get("scope", ""), item["column"])] = {
                    "scope": item.get("scope", ""),
                    "column": item["column"],
                    "category": item.get("category", "custom"),
                    "kind": item.get("kind", "flat"),
                    "path": item.get("path", ""),
                    "original_dtype": item.get("original_dtype"),
                    "processed_dtype": item.get("processed_dtype"),
                    "mapping": mapping,
                    "bijective": len(reverse) == len(mapping),
                }

        store._metadata = {
            "version": data.get("version", "1.0"),
            "session_id": data.get("session_id"),
            "created_at": data.get("created_at"),
        }
        return store

    def _load_v1_mappings(self, raw: Dict[str, Any]):
        """Load version 1 mappings ({column: {category, mapping}})."""
        for col, entry in raw.items():
            mapping = entry.get("mapping", {}) if isinstance(entry, dict) else {}
            category = entry.get("category", "custom") if isinstance(entry, dict) else "custom"
            if ":" in col:
                parent, _, json_path = col.partition(":")
                scope, column, kind, path = "", parent, "json_string", json_path
            else:
                scope, column, kind, path = "", col, "flat", ""
            reverse = {v: k for k, v in mapping.items()}
            self._entries[_entry_key(scope, column + (":" + path if kind == "json_string" else ""))] = {
                "scope": scope,
                "column": column if kind == "flat" else f"{column}:{path}",
                "category": category,
                "kind": kind,
                "path": path,
                "original_dtype": None,
                "processed_dtype": None,
                "mapping": dict(mapping),
                "bijective": len(reverse) == len(mapping),
            }


class PIIMapper:
    """High-level mapper interface for PII transformations."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self.store = MappingStore(config)
        self._session_id = str(uuid.uuid4())

    @property
    def session_id(self) -> str:
        return self._session_id

    def add_column_mapping(
        self,
        column: str,
        category: str,
        mapping: Dict[Any, Any],
        scope: str = "",
        kind: str = "flat",
        path: str = "",
        original_dtype: Optional[str] = None,
        processed_dtype: Optional[str] = None,
    ):
        """Add a mapping for a column."""
        self.store.add_mapping(
            column, category, mapping,
            scope=scope, kind=kind, path=path,
            original_dtype=original_dtype, processed_dtype=processed_dtype,
        )

    def merge(self, other: Union['PIIMapper', Dict[str, str]], column: str = "", category: str = ""):
        """Merge another mapper or mapping dict."""
        if isinstance(other, PIIMapper):
            for key, entry in other.store._entries.items():
                self.store._entries[key] = entry
        elif isinstance(other, dict) and column:
            self.store.add_mapping(column, category, other)

    def get_original_value(self, replacement: Any) -> Optional[Any]:
        """Look up original value from a replacement."""
        reverse = self.store.get_reverse_mapping()
        return reverse.get(replacement)

    def get_original_value_for_column(
        self,
        replacement: Any,
        column: str,
        scope: str = "",
    ) -> Optional[Any]:
        """Look up original value for a specific column."""
        mapping = self.store.get_mapping(column, scope)
        if mapping:
            reverse = {v: k for k, v in mapping.items()}
            return reverse.get(replacement)
        return None

    def is_lossless_mode(self) -> bool:
        """True when the configured mode promises an exact restore through a lookup."""
        return self.config.mode == PIIMode.FAKE or (
            self.config.mode == PIIMode.REDACT
            and self.config.redact_style in (RedactionStyle.TOKEN, RedactionStyle.TYPED_TOKEN)
        )

    def validate_reversible(self) -> None:
        """
        Raise AmbiguousMappingError if a mapping sends two originals to one replacement.

        Only meaningful for fake and token modes: full/partial redaction is
        many-to-one by design. The store already records `bijective` per entry;
        this refuses to write a file that could not be restored exactly.
        """
        if not self.is_lossless_mode():
            return
        clashing = [
            f"{column!r}" + (f" (sheet {scope!r})" if scope else "")
            for (scope, column), entry in self.store._entries.items()
            if not entry.get("bijective", True)
        ]
        if clashing:
            raise AmbiguousMappingError(
                "Mapping is not reversible: different originals share one replacement in "
                + ", ".join(clashing)
            )

    def save(self, path: str, *, passphrase: Optional[str] = None, encrypt: bool = False, use_keyring: bool = True):
        """Save mapping to file (JSON or SQLite).

        When encrypt is True, the file is wrapped in an AES-GCM envelope
        (roadmap 3.1). Decryption is probed automatically on load, so old
        plaintext v2 files keep loading unchanged.
        """
        self.validate_reversible()

        path_obj = Path(path)
        data = self.store.to_dict(session_id=self._session_id)

        if encrypt:
            from pii_faker.core import crypto as _crypto

            if path_obj.suffix in (".db", ".sqlite"):
                raw = json.dumps(data, ensure_ascii=False).encode()
                env = _crypto.encrypt_bytes(raw, passphrase=passphrase, use_keyring=use_keyring)
                with _open_private(path) as f:
                    json.dump(env, f)
                return
            env = _crypto.encrypt_dict(data, passphrase=passphrase, use_keyring=use_keyring)
            with _open_private(path) as f:
                json.dump(env, f)
            return

        if path_obj.suffix == ".db" or path_obj.suffix == ".sqlite":
            self._save_sqlite(path, data)
        else:
            self._save_json(path, data)

    def _save_json(self, path: str, data: Dict[str, Any]):
        """Save as JSON file."""
        with _open_private(path) as f:
            json.dump(data, f, indent=2)

    def _save_sqlite(self, path: str, data: Dict[str, Any]):
        """Save as SQLite database."""
        _make_private(path)
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        cursor.execute("DROP TABLE IF EXISTS mappings")
        cursor.execute("""
            CREATE TABLE mappings (
                scope TEXT,
                column_name TEXT,
                category TEXT,
                kind TEXT,
                path TEXT,
                original_dtype TEXT,
                processed_dtype TEXT,
                orig_type TEXT,
                original TEXT,
                repl_type TEXT,
                replacement TEXT,
                PRIMARY KEY (scope, column_name, original)
            )
        """)

        metadata = {
            "version": data["version"],
            "tool": data.get("tool", "pii-faker"),
            "tool_version": data.get("tool_version", ""),
            "session_id": data.get("session_id") or "",
            "created_at": data["created_at"],
            "config": json.dumps(data["config"]),
        }
        for key, value in metadata.items():
            cursor.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                (key, value),
            )

        for item in data["mappings"]:
            for orig_enc, repl_enc in item["pairs"]:
                cursor.execute(
                    """INSERT OR REPLACE INTO mappings
                       (scope, column_name, category, kind, path,
                        original_dtype, processed_dtype,
                        orig_type, original, repl_type, replacement)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        item["scope"], item["column"], item["category"],
                        item["kind"], item["path"],
                        item["original_dtype"], item["processed_dtype"],
                        orig_enc["t"], json.dumps(orig_enc["v"]),
                        repl_enc["t"], json.dumps(repl_enc["v"]),
                    ),
                )

        conn.commit()
        conn.close()

    @classmethod
    def load(cls, path: str, config: Optional[PIIConfig] = None, *, passphrase: Optional[str] = None, use_keyring: bool = True) -> 'PIIMapper':
        """Load mapping from file (v1 or v2, JSON or SQLite, plaintext or encrypted)."""
        path_obj = Path(path)

        if path_obj.suffix == ".db" or path_obj.suffix == ".sqlite":
            try:
                with open(path, "r") as f:
                    probe = json.load(f)
                from pii_faker.core import crypto as _crypto

                if _crypto.is_encrypted_envelope(probe):
                    data = _crypto.decrypt_dict(probe, passphrase=passphrase, use_keyring=use_keyring)
                    store = MappingStore.from_dict(data, config)
                    mapper = cls(store.config)
                    mapper.store = store
                    sid = store._metadata.get("session_id")
                    if sid:
                        mapper._session_id = sid
                    return mapper
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            data = cls._load_sqlite(path)
        else:
            with open(path, "r") as f:
                raw = json.load(f)
            try:
                from pii_faker.core import crypto as _crypto

                if _crypto.is_encrypted_envelope(raw):
                    raw = _crypto.decrypt_dict(raw, passphrase=passphrase, use_keyring=use_keyring)
            except Exception:
                from pii_faker.core.crypto import CryptoError

                if isinstance(raw, dict) and raw.get("enc") == "aes-gcm":
                    raise
            data = raw

        store = MappingStore.from_dict(data, config)
        mapper = cls(store.config)
        mapper.store = store
        session_id = store._metadata.get("session_id")
        if session_id:
            mapper._session_id = session_id
        return mapper

    @classmethod
    def _load_sqlite(cls, path: str) -> Dict[str, Any]:
        """Load from SQLite database (v1 or v2 schema)."""
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        cursor.execute("SELECT key, value FROM metadata")
        metadata = dict(cursor.fetchall())

        config = json.loads(metadata.get("config", "{}"))

        cursor.execute("PRAGMA table_info(mappings)")
        columns = {row[1] for row in cursor.fetchall()}

        mappings: List[Dict[str, Any]] = []
        if "scope" in columns:
            cursor.execute(
                """SELECT scope, column_name, category, kind, path,
                          original_dtype, processed_dtype,
                          orig_type, original, repl_type, replacement
                   FROM mappings"""
            )
            grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for row in cursor.fetchall():
                (scope, col, category, kind, path, orig_dtype,
                 proc_dtype, orig_type, original, repl_type, replacement) = row
                key = (scope or "", col)
                if key not in grouped:
                    grouped[key] = {
                        "scope": scope or "",
                        "column": col,
                        "category": category,
                        "kind": kind or "flat",
                        "path": path or "",
                        "original_dtype": orig_dtype,
                        "processed_dtype": proc_dtype,
                        "pairs": [],
                    }
                grouped[key]["pairs"].append([
                    {"t": orig_type, "v": json.loads(original)},
                    {"t": repl_type, "v": json.loads(replacement)},
                ])
            mappings = list(grouped.values())
        else:
            cursor.execute("SELECT column_name, category, original, replacement FROM mappings")
            v1: Dict[str, Dict[str, Any]] = {}
            for col, category, original, replacement in cursor.fetchall():
                if col not in v1:
                    v1[col] = {"category": category, "mapping": {}}
                v1[col]["mapping"][original] = replacement
            conn.close()
            return {
                "version": metadata.get("version", "1.0"),
                "session_id": metadata.get("session_id"),
                "created_at": metadata.get("created_at", dt_datetime.now().isoformat()),
                "config": config,
                "mappings": v1,
            }

        conn.close()

        return {
            "version": metadata.get("version", MAPPING_VERSION),
            "tool": metadata.get("tool", "pii-faker"),
            "tool_version": metadata.get("tool_version", ""),
            "session_id": metadata.get("session_id") or None,
            "created_at": metadata.get("created_at", dt_datetime.now().isoformat()),
            "config": config,
            "mappings": mappings,
        }

    def cleanup_old_mappings(self, directory: str, older_than_days: float = 7, *, pattern: Optional[str] = None):
        """Clean up mapping files older than specified days (roadmap 3.2).

        Detection is by format (tool=="pii-faker" and a version field, or the
        SQLite metadata row) rather than filename, so non-`mapping_*` files
        that contain our format are still found, and random `mapping_*.json`
        from other tools are left alone. Returns the number of files deleted.
        """
        import fnmatch

        dir_path = Path(directory)
        if not dir_path.is_dir():
            return 0
        cutoff = dt_datetime.now() - timedelta(days=older_than_days)
        deleted = 0

        for path in dir_path.iterdir():
            if not path.is_file():
                continue
            if pattern and not fnmatch.fnmatch(path.name, pattern):
                continue
            if not self._is_our_mapping_file(path):
                continue
            file_time = self._mapping_file_time(path)
            if file_time < cutoff:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    pass
        return deleted

    def _is_our_mapping_file(self, path: Path) -> bool:
        """Probe whether a file is recognisably our mapping format."""
        suffix = path.suffix.lower()
        if suffix in (".db", ".sqlite"):
            try:
                conn = sqlite3.connect(str(path))
                cur = conn.cursor()
                cur.execute("SELECT value FROM metadata WHERE key='tool'")
                row = cur.fetchone()
                conn.close()
                return row is not None and row[0] == "pii-faker"
            except Exception:
                return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False
        if isinstance(data, dict) and data.get("enc") == "aes-gcm":
            return True
        return data.get("tool") == "pii-faker" and "version" in data

    def _mapping_file_time(self, path: Path) -> dt_datetime:
        """Best creation time for a mapping file: in-file created_at, else mtime."""
        suffix = path.suffix.lower()
        if suffix in (".db", ".sqlite"):
            try:
                conn = sqlite3.connect(str(path))
                cur = conn.cursor()
                cur.execute("SELECT value FROM metadata WHERE key='created_at'")
                row = cur.fetchone()
                conn.close()
                if row and row[0]:
                    return dt_datetime.fromisoformat(row[0])
            except Exception:
                pass
            return dt_datetime.fromtimestamp(path.stat().st_mtime)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("enc") == "aes-gcm":
                return dt_datetime.fromtimestamp(path.stat().st_mtime)
            created = data.get("created_at")
            if created:
                return dt_datetime.fromisoformat(created)
        except Exception:
            pass
        try:
            parts = path.stem.split("_")
            if len(parts) >= 2:
                return dt_datetime.fromisoformat(parts[1])
        except Exception:
            pass
        return dt_datetime.fromtimestamp(path.stat().st_mtime)
