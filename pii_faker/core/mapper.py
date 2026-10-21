"""
PII mapping management module.

Handles mapping storage, persistence, and lookup for reversible PII transformation.
Supports JSON for small mappings and SQLite for large ones.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Union

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle


class AmbiguousMappingError(ValueError):
    """Two different originals share one replacement, so restore cannot be exact."""


# Files `cleanup` may delete: names starting with "mapping" and one of these
# extensions. Deliberately narrow: the directory is user-supplied and may be a
# project folder holding unrelated files such as `mapping.py`.
MAPPING_SUFFIXES = {'.json', '.db', '.sqlite'}


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
    return os.fdopen(fd, 'w')


class MappingStore:
    """Storage backend for PII mappings."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self._json_mappings: Dict[str, Dict[str, str]] = {}
        self._metadata: Dict[str, Any] = {}

    def add_mapping(
        self,
        column: str,
        category: str,
        mapping: Dict[str, str],
    ):
        """Add a column mapping."""
        self._json_mappings[column] = {
            'category': category,
            'mapping': mapping,
        }

    def get_mapping(self, column: str) -> Optional[Dict[str, str]]:
        """Get mapping for a column."""
        entry = self._json_mappings.get(column)
        if entry:
            return entry.get('mapping')
        return None

    def get_all_mappings(self) -> Dict[str, Dict[str, str]]:
        """Get all mappings as {column: {original: replacement}}."""
        result = {}
        for col, entry in self._json_mappings.items():
            result[col] = entry.get('mapping', {})
        return result

    def get_reverse_mapping(self) -> Dict[str, str]:
        """Get reverse mapping: {replacement: original} for all columns."""
        reverse = {}
        for col, entry in self._json_mappings.items():
            mapping = entry.get('mapping', {})
            for original, replacement in mapping.items():
                reverse[replacement] = original
        return reverse

    def get_reverse_mapping_by_column(self) -> Dict[str, Dict[str, str]]:
        """Get reverse mapping organized by column."""
        result = {}
        for col, entry in self._json_mappings.items():
            mapping = entry.get('mapping', {})
            reverse = {v: k for k, v in mapping.items()}
            result[col] = reverse
        return result

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return {
            'version': '1.0',
            'created_at': datetime.now().isoformat(),
            'config': {
                'mode': self.config.mode,
                'redact_style': self.config.redact_style,
                'match_strategy': self.config.match_strategy,
                'seed': self.config.seed,
            },
            'mappings': self._json_mappings,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], config: Optional[PIIConfig] = None) -> 'MappingStore':
        """Deserialize from dict."""
        if config is None:
            config_data = data.get('config', {})
            config = PIIConfig(**config_data)

        store = cls(config)
        store._json_mappings = data.get('mappings', {})
        return store


class PIIMapper:
    """High-level mapper interface for PII transformations."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self.store = MappingStore(config)
        self._session_id = str(uuid.uuid4())

    def add_column_mapping(
        self,
        column: str,
        category: str,
        mapping: Dict[str, str],
    ):
        """Add a mapping for a column."""
        self.store.add_mapping(column, category, mapping)

    def merge(self, other: Union['PIIMapper', Dict[str, str]], column: str = "", category: str = ""):
        """Merge another mapper or mapping dict."""
        if isinstance(other, PIIMapper):
            for col, entry in other.store._json_mappings.items():
                self.store._json_mappings[col] = entry
        elif isinstance(other, dict) and column:
            self.store.add_mapping(column, category, other)

    def get_original_value(self, replacement: str) -> Optional[str]:
        """Look up original value from a replacement."""
        reverse = self.store.get_reverse_mapping()
        return reverse.get(replacement)

    def get_original_value_for_column(
        self,
        replacement: str,
        column: str,
    ) -> Optional[str]:
        """Look up original value for a specific column."""
        mapping = self.store.get_mapping(column)
        if mapping:
            reverse = {v: k for k, v in mapping.items()}
            return reverse.get(replacement)
        return None

    def is_lossless_mode(self) -> bool:
        """True when the configured mode promises an exact restore."""
        return self.config.mode == PIIMode.FAKE or (
            self.config.mode == PIIMode.REDACT
            and self.config.redact_style == RedactionStyle.TOKEN
        )

    def validate_reversible(self):
        """
        Raise AmbiguousMappingError if a column maps two originals to one replacement.

        Only meaningful for fake and token modes; full/partial redaction is
        many-to-one by design.
        """
        if not self.is_lossless_mode():
            return

        problems = []
        for column, entry in self.store._json_mappings.items():
            seen: Dict[str, str] = {}
            clashes = 0
            for original, replacement in entry.get('mapping', {}).items():
                if replacement in seen and seen[replacement] != original:
                    clashes += 1
                seen[replacement] = original
            if clashes:
                problems.append(f"'{column}' ({clashes} clashing value(s))")

        if problems:
            raise AmbiguousMappingError(
                "Mapping is not reversible: different originals share one replacement in "
                + ", ".join(problems)
            )

    def save(self, path: str):
        """Save mapping to file (JSON or SQLite), readable only by the owner."""
        self.validate_reversible()

        path_obj = Path(path)
        data = self.store.to_dict()

        if path_obj.suffix == '.db' or path_obj.suffix == '.sqlite':
            self._save_sqlite(path, data)
        else:
            self._save_json(path, data)

    def _save_json(self, path: str, data: Dict[str, Any]):
        """Save as JSON file."""
        with _open_private(path) as f:
            json.dump(data, f, indent=2)

    def _save_sqlite(self, path: str, data: Dict[str, Any]):
        """Save as SQLite database."""
        # Start from a fresh private file: rows left over from an earlier run
        # would otherwise be merged into this mapping.
        _open_private(path).close()

        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE mappings (
                column_name TEXT,
                category TEXT,
                original TEXT,
                replacement TEXT,
                PRIMARY KEY (column_name, original)
            )
        ''')

        # Store metadata
        metadata = {
            'version': data['version'],
            'created_at': data['created_at'],
            'config': json.dumps(data['config']),
        }
        cursor.executemany(
            'INSERT INTO metadata (key, value) VALUES (?, ?)',
            list(metadata.items()),
        )

        # Store mappings
        for col, entry in data['mappings'].items():
            category = entry['category']
            cursor.executemany(
                'INSERT INTO mappings (column_name, category, original, replacement) VALUES (?, ?, ?, ?)',
                [(col, category, original, replacement)
                 for original, replacement in entry['mapping'].items()],
            )

        conn.commit()
        conn.close()

    @classmethod
    def load(cls, path: str, config: Optional[PIIConfig] = None) -> 'PIIMapper':
        """Load mapping from file."""
        path_obj = Path(path)

        if path_obj.suffix == '.db' or path_obj.suffix == '.sqlite':
            data = cls._load_sqlite(path)
        else:
            with open(path, 'r') as f:
                data = json.load(f)

        store = MappingStore.from_dict(data, config)
        mapper = cls(store.config)
        mapper.store = store
        return mapper

    @classmethod
    def _load_sqlite(cls, path: str) -> Dict[str, Any]:
        """Load from SQLite database."""
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        # Load metadata
        cursor.execute('SELECT key, value FROM metadata')
        metadata = dict(cursor.fetchall())

        config = json.loads(metadata.get('config', '{}'))

        # Load mappings
        cursor.execute('SELECT column_name, category, original, replacement FROM mappings')
        rows = cursor.fetchall()

        mappings: Dict[str, Dict[str, Any]] = {}
        for col, category, original, replacement in rows:
            if col not in mappings:
                mappings[col] = {'category': category, 'mapping': {}}
            mappings[col]['mapping'][original] = replacement

        conn.close()

        return {
            'version': metadata.get('version', '1.0'),
            'created_at': metadata.get('created_at', datetime.now().isoformat()),
            'config': config,
            'mappings': mappings,
        }

    def cleanup_old_mappings(self, directory: str, older_than_days: float = 7) -> List[Path]:
        """
        Delete mapping files older than the given age (fractional days allowed).

        Only files named `mapping*` with a `.json`, `.db` or `.sqlite`
        extension are considered. Returns the paths that were deleted.
        """
        dir_path = Path(directory)
        if not dir_path.is_dir():
            return []
        cutoff = datetime.now() - timedelta(days=older_than_days)

        deleted = []
        for path in sorted(dir_path.iterdir()):
            if not path.is_file() or not path.name.startswith('mapping'):
                continue
            if path.suffix not in MAPPING_SUFFIXES:
                continue
            # Prefer timestamp embedded in filename (mapping_<iso-timestamp>_<uuid>.*),
            # fall back to file mtime so plain names like mapping.json still expire.
            try:
                parts = path.stem.split('_')
                if len(parts) >= 2:
                    file_time = datetime.fromisoformat(parts[1])
                else:
                    raise ValueError("no timestamp in name")
            except (ValueError, IndexError):
                file_time = datetime.fromtimestamp(path.stat().st_mtime)
            if file_time < cutoff:
                path.unlink()
                deleted.append(path)
        return deleted
