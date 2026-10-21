"""
PII fake data generation module.

Generates realistic fake data based on PII category.
Uses the unique-values-first approach for performance, with per-run
uniqueness enforcement so fake mode stays exactly reversible.
"""

import itertools
import re
import string
from datetime import date, timedelta
from typing import Any, Callable, Dict, Iterator, Optional, Set, Tuple

import polars as pl
from faker import Faker

from pii_faker.core.config import PIIConfig
from pii_faker.core.state import RunState

MAX_FAKE_ATTEMPTS = 50
MAX_FALLBACK_IDS = 4_294_967_296


def _shape(value: str) -> str:
    """A value's format: digits -> 9, letters -> a/A. Values of one shape share a fake space."""
    return re.sub(r"[A-Z]", "A", re.sub(r"[a-z]", "a", re.sub(r"\d", "9", value)))


class PIIGenerator:
    """Generates fake PII data based on category."""

    def __init__(self, config: PIIConfig, run_state: Optional[RunState] = None):
        self.config = config
        self.run_state = run_state or RunState()
        self.fake = Faker(config.locale or "en_US")
        if config.seed is not None:
            self.fake.seed_instance(config.seed)
        # (category, shape) pairs whose random draws have run dry: later values of
        # that shape skip the retries and go straight to counting.
        self._exhausted: Set[Tuple[str, str]] = set()
        # shapes with no fake left that also avoids every original (see find_variant)
        self._dense: Set[Tuple[str, str]] = set()
        self._date_cursor: Optional[date] = None

    def _generate_name(self, original: str) -> str:
        """Generate a fake name."""
        return self.fake.name()

    def _generate_identifier(self, original: str) -> str:
        """
        Generate a fake identifier preserving format.

        Length, case and punctuation are kept; digits and letters are both
        randomised. Letters are as identifying as digits (initials, office
        codes, a purely alphabetic id), and we can't tell which are safe.
        """
        result = []
        for char in original:
            if char.isdigit():
                result.append(str(self.fake.random_digit()))
            elif char.isalpha():
                result.append(
                    self.fake.random_uppercase_letter() if char.isupper()
                    else self.fake.random_lowercase_letter()
                )
            else:
                result.append(char)
        return "".join(result)

    def _generate_contact(self, original: str, field_name: str = "") -> str:
        """Generate fake contact info based on field name hints."""
        field_lower = field_name.lower()

        if "email" in field_lower or "@" in original:
            return self.fake.email()
        elif "phone" in field_lower or any(c.isdigit() for c in original):
            result = []
            for char in original:
                if char.isdigit():
                    result.append(str(self.fake.random_digit()))
                else:
                    result.append(char)
            return "".join(result)
        else:
            return self.fake.phone_number()

    def _generate_address(self, original: str) -> str:
        """Generate a fake address."""
        return self.fake.address().replace("\n", ", ")

    def _random_date(self) -> date:
        """Draw a random adult date of birth."""
        return self.fake.date_of_birth(minimum_age=18, maximum_age=90)

    @staticmethod
    def _format_date(fake_date: date, original: str) -> str:
        """Format a date using the separator and field order of the original."""
        if "/" in original:
            sep = "/"
        elif "-" in original:
            sep = "-"
        elif "." in original:
            sep = "."
        else:
            sep = "-"

        parts = re.split(r"[/\-.]", original)

        if len(parts) == 3:
            if len(parts[0]) == 4:
                return f"{fake_date.year}{sep}{fake_date.month:02d}{sep}{fake_date.day:02d}"
            elif int(parts[0]) > 12:
                return f"{fake_date.day:02d}{sep}{fake_date.month:02d}{sep}{fake_date.year}"
            else:
                return f"{fake_date.month:02d}{sep}{fake_date.day:02d}{sep}{fake_date.year}"

        return fake_date.strftime("%Y-%m-%d")

    def _generate_date(self, original: str) -> str:
        """Generate a fake date preserving format."""
        return self._format_date(self._random_date(), original)

    def _generate_generic(self, original: str) -> str:
        """Generate a generic fake value preserving type."""
        if original.isdigit():
            return "".join(str(self.fake.random_digit()) for _ in original)
        elif any(c.isdigit() for c in original):
            result = []
            for char in original:
                if char.isdigit():
                    result.append(str(self.fake.random_digit()))
                elif char.isalpha():
                    result.append(self.fake.random_letter())
                else:
                    result.append(char)
            return "".join(result)
        else:
            return self.fake.word()

    def generate_fake_value(self, original: str, category: str, field_name: str = "") -> str:
        """
        Generate a fake value based on PII category.

        Args:
            original: Original value to match format
            category: PII category (name, identifier, contact, address)
            field_name: Name of the field for context

        Returns:
            Generated fake value
        """
        generators = {
            "name": lambda: self._generate_name(original),
            "identifier": lambda: self._generate_identifier(original),
            "contact": lambda: self._generate_contact(original, field_name),
            "address": lambda: self._generate_address(original),
            "date": lambda: self._generate_date(original),
        }

        generator = generators.get(category, lambda: self._generate_generic(original))
        return generator()

    def _claim_unique(
        self, candidate: str, originals: set, original: str, allow_originals: bool = False
    ) -> bool:
        """Claim a fake candidate if unused and safe. Returns True on success.

        `allow_originals` lets a fake equal some *other* original: the last stop
        before leaving the original's format, for dense spaces (9,000 of the
        10,000 four-digit codes) where fakes that avoid every original don't fit.
        """
        if not candidate or candidate == original:
            return False
        if candidate in originals and not allow_originals:
            return False
        if candidate in self.run_state.used_fakes:
            return False
        self.run_state.used_fakes.add(candidate)
        return True

    @staticmethod
    def _odometer(text: str, letters: bool, limit: int) -> Iterator[str]:
        """
        Variants of `text` that count upward through its digit (and optionally
        letter) positions, like an odometer. Format-preserving, and derived from
        the fake, never from the original.
        """
        slots = []
        for i, ch in enumerate(text):
            if ch in string.digits:
                slots.append((i, string.digits))
            elif letters and ch in string.ascii_letters:
                slots.append((i, string.ascii_uppercase if ch.isupper() else string.ascii_lowercase))
        if not slots:
            return

        space, number = 1, 0
        for pos, alphabet in slots:
            space *= len(alphabet)
            number = number * len(alphabet) + alphabet.index(text[pos])

        for _ in range(min(space, limit)):
            number = (number + 1) % space
            chars = list(text)
            remainder = number
            for pos, alphabet in reversed(slots):
                remainder, index = divmod(remainder, len(alphabet))
                chars[pos] = alphabet[index]
            yield "".join(chars)

    def next_free_date(self, is_free: Callable[[date], bool]) -> Optional[date]:
        """
        The next date `is_free` accepts, sweeping forward from a random start.

        Only ~26k dates of birth exist in the draw range and real tables fill it, so
        once random draws collide we sweep with a cursor that never moves back: the
        sweep costs O(n) over the run rather than O(n) per value.
        """
        cursor = self._date_cursor or self._random_date()
        limit = len(self.run_state.used_fakes) + len(self.run_state.originals) + 1
        for _ in range(limit):
            candidate, cursor = cursor, cursor + timedelta(days=1)
            if is_free(candidate):
                self._date_cursor = cursor
                return candidate
        self._date_cursor = cursor
        return None

    def find_variant(
        self,
        key: Tuple[str, str],
        text: str,
        letters: bool,
        free_strict: Callable[[str], bool],
        free_relaxed: Callable[[str], bool],
    ) -> Optional[str]:
        """
        The first format-preserving variant of `text` that `free_strict` accepts,
        else (once the format has no such variant left) one `free_relaxed` accepts.

        Remembering that a shape ran dry keeps a dense column linear: otherwise each
        value would rescan the whole space looking for a variant that cannot exist.
        """
        limit = len(self.run_state.used_fakes) + len(self.run_state.originals) + 1
        if key not in self._dense:
            for variant in self._odometer(text, letters, limit):
                if free_strict(variant):
                    return variant
            self._dense.add(key)
        for variant in self._odometer(text, letters, limit):
            if free_relaxed(variant):
                return variant
        return None

    def _unused_variant(
        self, original: str, candidate: str, category: str, field_name: str = ""
    ) -> Optional[str]:
        """An unclaimed, format-preserving fake built from a fake draw, or None."""
        originals = self.run_state.originals
        key = (category, _shape(original))

        if category == "date":
            for allow in (False, True):
                found: Dict[str, str] = {}

                def free(day: date, allow: bool = allow) -> bool:
                    text = self._format_date(day, original)
                    if self._claim_unique(text, originals, original, allow_originals=allow):
                        found["text"] = text
                        return True
                    return False

                if self.next_free_date(free) is not None:
                    return found["text"]
            return None

        # Build only on a draw that says nothing about the original: when the random
        # draw happened to equal it (or contain it), variants of it would too.
        for _ in range(MAX_FAKE_ATTEMPTS):
            if candidate and not (original and original in candidate):
                break
            candidate = self.generate_fake_value(original, category, field_name)
        else:
            return None

        local, at, domain = candidate.partition("@")
        if not at and category not in ("name", "address"):
            variant = self.find_variant(
                key, candidate, category == "identifier",
                lambda v: self._claim_unique(v, originals, original),
                lambda v: self._claim_unique(v, originals, original, allow_originals=True),
            )
            if variant is not None:
                return variant

        # Free-form values (names, emails, addresses): add a counter to the fake.
        for n in itertools.count(2):
            variant = f"{local}{n}@{domain}" if at else f"{candidate} {n}"
            if self._claim_unique(variant, originals, original):
                return variant
        return None  # unreachable: count() never ends

    def _fallback_fake(self, category: str) -> str:
        """Last resort: a unique label that says nothing about the original."""
        while True:
            n = self.run_state.next_fallback()
            if n > MAX_FALLBACK_IDS:
                raise ValueError(
                    f"Could not generate a unique fake for category {category!r}: "
                    "fallback space exhausted"
                )
            candidate = f"{category}#F{n}"
            if self._claim_unique(candidate, self.run_state.originals, ""):
                self.run_state.fallbacks += 1
                return candidate

    def generate_unique_fake(self, original: str, category: str, field_name: str = "") -> str:
        """
        Generate a fake that is unique across the run and differs from any original.

        The result never contains the original value. When random draws run
        out (dense codes, a saturated date range) it counts through the last
        fake's own digits, letters or days instead.
        """
        originals = self.run_state.originals
        key = (category, _shape(original))
        attempts = 3 if key in self._exhausted else MAX_FAKE_ATTEMPTS

        candidate = ""
        for _ in range(attempts):
            candidate = self.generate_fake_value(original, category, field_name)
            if self._claim_unique(candidate, originals, original):
                return candidate

        self._exhausted.add(key)
        variant = self._unused_variant(original, candidate, category, field_name)
        if variant is not None:
            return variant

        self.run_state.warn(f"faker retries exhausted ({category}); using a generic fallback")
        return self._fallback_fake(category)

    def build_mapping(
        self,
        series: pl.Series,
        category: str,
        field_name: str = "",
    ) -> Dict[str, str]:
        """
        Build a mapping from original values to fake values.

        Uses unique-values-first approach for performance:
        1. Get unique non-null values
        2. Sort them for consistent ordering
        3. Generate unique fakes only for unique values
        4. Return mapping dict

        Args:
            series: Polars Series with original values
            category: PII category
            field_name: Name of the field

        Returns:
            Dict mapping original -> fake
        """
        unique_values = sorted(series.drop_nulls().unique().to_list(), key=str)
        self.run_state.register_originals(unique_values)

        mapping = {}
        for original in unique_values:
            if isinstance(original, str):
                mapping[original] = self.generate_unique_fake(original, category, field_name)

        return mapping

    def process_column(
        self,
        df: pl.DataFrame,
        column_info: Dict[str, Any],
    ) -> Tuple[pl.DataFrame, Dict[str, str]]:
        """
        Process a single column: build mapping and apply fakes.

        Args:
            df: Input DataFrame
            column_info: Dict with column, category, etc.

        Returns:
            Tuple of (modified DataFrame, mapping dict)
        """
        col_name = column_info["column"]
        category = column_info["category"]

        mapping = self.build_mapping(df[col_name], category, col_name)

        if not mapping:
            return df, {}

        df = df.with_columns(pl.col(col_name).replace(mapping).alias(col_name))

        return df, mapping


class FakeDataGenerator:
    """Standalone fake data generator for testing/benchmarking."""

    def __init__(self, seed: Optional[int] = None):
        self.fake = Faker()
        if seed is not None:
            Faker.seed(seed)

    def generate_names(self, n: int) -> list:
        """Generate n fake names."""
        return [self.fake.name() for _ in range(n)]

    def generate_emails(self, n: int) -> list:
        """Generate n fake emails."""
        return [self.fake.email() for _ in range(n)]

    def generate_phones(self, n: int) -> list:
        """Generate n fake phone numbers."""
        return [self.fake.phone_number() for _ in range(n)]

    def generate_addresses(self, n: int) -> list:
        """Generate n fake addresses."""
        return [self.fake.address().replace("\n", ", ") for _ in range(n)]

    def generate_identifiers(self, n: int, length: int = 10) -> list:
        """Generate n fake identifiers of given length."""
        return [self.fake.numerify("#" * length) for _ in range(n)]
