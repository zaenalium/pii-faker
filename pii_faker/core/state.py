"""
Shared per-run state for PII processing.

A single RunState is shared by the generator and redactor for the whole
run (all targets, all sheets/files). Sharing it is what guarantees:

- fake values are unique across every column and sheet (D5),
- token counters never restart per column (D8),
- fakes never equal any original value seen in the run.
"""

from typing import List, Set


class RunState:
    """Mutable state shared across one processing run."""

    def __init__(self):
        self.used_fakes: Set[str] = set()
        self.originals: Set[str] = set()
        self.token_counter: int = 0
        self.fallback_counter: int = 0
        self.fallbacks: int = 0
        self.warnings: List[str] = []

    def register_originals(self, values) -> None:
        """Record original values so fakes can avoid colliding with them."""
        for v in values:
            if isinstance(v, str) and v:
                self.originals.add(v)

    def next_token(self) -> int:
        """Return the next globally unique token number (1-based)."""
        self.token_counter += 1
        return self.token_counter

    def next_fallback(self) -> int:
        """Return the next fallback suffix number (1-based)."""
        self.fallback_counter += 1
        return self.fallback_counter

    def warn(self, message: str) -> None:
        """Record a run warning (callers dedupe on display)."""
        self.warnings.append(message)
