"""
PII fake data generation module.

Generates realistic fake data based on PII category.
Uses the unique-values-first approach for performance.
"""

import re
import string
from datetime import date, timedelta
from typing import Dict, Any, Optional, Set, Tuple

import polars as pl
from faker import Faker

from pii_faker.core.config import PIIConfig


class _FormatExhausted(Exception):
    """A format-preserving fake ran out of unused values."""


class _ColumnState:
    """Bookkeeping while building the mapping for one column."""

    __slots__ = ('fakes', 'cursors', 'strict_exhausted')

    def __init__(self):
        self.fakes: Set[str] = set()
        # Date sweep position per scope, so the sweep never rescans used dates.
        self.cursors: Dict[int, date] = {}
        # Once the run-wide scope is out of values for a small format space,
        # stop probing it: every probe would rescan the whole space.
        self.strict_exhausted = False


class PIIGenerator:
    """Generates fake PII data based on category.

    Within a column, every original gets a distinct fake: restoring is a
    reverse lookup, so two originals sharing a fake would silently corrupt the
    round trip. Fakes are also kept distinct across columns whenever the format
    leaves room.
    """

    # Random draws before falling back to deterministic disambiguation.
    MAX_ATTEMPTS = 8

    def __init__(self, config: PIIConfig):
        self.config = config
        self.fake = Faker()
        if config.seed is not None:
            self.fake.seed_instance(config.seed)
        self._used: Set[str] = set()

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
        return ''.join(result)

    def _generate_contact(self, original: str, field_name: str = "") -> str:
        """Generate fake contact info based on field name hints."""
        field_lower = field_name.lower()

        if 'email' in field_lower or '@' in original:
            return self.fake.email()
        elif 'phone' in field_lower or any(c.isdigit() for c in original):
            # Preserve phone-like format
            result = []
            for char in original:
                if char.isdigit():
                    result.append(str(self.fake.random_digit()))
                else:
                    result.append(char)
            return ''.join(result)
        else:
            return self.fake.phone_number()

    def _generate_address(self, original: str) -> str:
        """Generate a fake address."""
        return self.fake.address().replace('\n', ', ')

    def _random_date(self) -> date:
        """Draw a random adult date of birth."""
        return self.fake.date_of_birth(minimum_age=18, maximum_age=90)

    @staticmethod
    def _format_date(fake_date: date, original: str) -> str:
        """Format a date using the separator and field order of the original."""
        if '/' in original:
            sep = '/'
        elif '-' in original:
            sep = '-'
        elif '.' in original:
            sep = '.'
        else:
            sep = '-'

        parts = re.split(r'[/\-.]', original)

        if len(parts) == 3:
            # Determine format (YMD, DMY, MDY)
            if len(parts[0]) == 4:
                # YYYY-MM-DD
                return f"{fake_date.year}{sep}{fake_date.month:02d}{sep}{fake_date.day:02d}"
            elif int(parts[0]) > 12:
                # DD-MM-YYYY
                return f"{fake_date.day:02d}{sep}{fake_date.month:02d}{sep}{fake_date.year}"
            else:
                # MM-DD-YYYY
                return f"{fake_date.month:02d}{sep}{fake_date.day:02d}{sep}{fake_date.year}"

        # Fallback: return in same format
        return fake_date.strftime('%Y-%m-%d')

    def _generate_date(self, original: str) -> str:
        """Generate a fake date preserving format."""
        return self._format_date(self._random_date(), original)

    def _generate_generic(self, original: str) -> str:
        """Generate a generic fake value preserving type."""
        if original.isdigit():
            return ''.join(str(self.fake.random_digit()) for _ in original)
        elif any(c.isdigit() for c in original):
            result = []
            for char in original:
                if char.isdigit():
                    result.append(str(self.fake.random_digit()))
                elif char.isalpha():
                    result.append(self.fake.random_letter())
                else:
                    result.append(char)
            return ''.join(result)
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
            'name': lambda: self._generate_name(original),
            'identifier': lambda: self._generate_identifier(original),
            'contact': lambda: self._generate_contact(original, field_name),
            'address': lambda: self._generate_address(original),
            'date': lambda: self._generate_date(original),
        }

        generator = generators.get(category, lambda: self._generate_generic(original))
        return generator()

    def build_mapping(
        self,
        series: pl.Series,
        category: str,
        field_name: str = "",
        reserved: Optional[Set[str]] = None,
    ) -> Dict[str, str]:
        """
        Build a mapping from original values to fake values.

        Uses unique-values-first approach for performance:
        1. Get unique non-null values
        2. Sort them for consistent ordering
        3. Generate fakes only for unique values
        4. Return mapping dict

        Args:
            series: Polars Series with original values
            category: PII category
            field_name: Name of the field
            reserved: Fakes handed out earlier that must not be reused, even
                when a small format space runs out of fresh values. Pass the
                fakes already shared with other columns.

        Returns:
            Dict mapping original -> fake
        """
        # Get unique non-null values and sort for consistent ordering.
        # key=str keeps mixed-type columns (str + int) from raising TypeError.
        unique_values = sorted(series.drop_nulls().unique().to_list(), key=str)

        # Build mapping for unique values only
        mapping = {}
        column = _ColumnState()
        column.fakes.update(reserved or ())
        for original in unique_values:
            if isinstance(original, str):
                fake_value = self._unique_fake(original, category, field_name, column)
                column.fakes.add(fake_value)
                self._used.add(fake_value)
                mapping[original] = fake_value

        return mapping

    def _unique_fake(
        self,
        original: str,
        category: str,
        field_name: str,
        column: _ColumnState,
    ) -> str:
        """
        Generate a fake that no other value in this column has received.

        Prefers a fake unused anywhere in this run, which keeps free-text
        restore unambiguous. Only per-column uniqueness is required for a
        lossless restore, so that is all we insist on once a small format
        space (e.g. 4-digit codes) runs out of fresh values.
        """
        scopes = [(1, column.fakes)]
        if not column.strict_exhausted:
            scopes.insert(0, (0, self._used))

        for scope, taken in scopes:
            try:
                return self._unique_in(original, category, field_name, taken, column.cursors, scope)
            except _FormatExhausted:
                if scope == 0:
                    column.strict_exhausted = True
        raise ValueError(
            f"Cannot generate enough unique fake values for '{field_name}' while "
            f"preserving its format: the original values use more distinct codes "
            f"than the format allows. Use --mode redact --redact-style token instead."
        )

    def _unique_in(
        self,
        original: str,
        category: str,
        field_name: str,
        taken: Set[str],
        cursors: Dict[int, date],
        scope: int,
    ) -> str:
        """Generate a fake outside `taken`, or raise _FormatExhausted."""
        if category == 'date':
            # Only ~26k distinct dates exist in the draw range, and real tables
            # saturate it. Draw randomly while there is room, then sweep forward
            # with a cursor that never moves back, so the sweep costs O(n) in
            # total rather than O(n) per value.
            for _ in range(self.MAX_ATTEMPTS):
                candidate = self._generate_date(original)
                if candidate not in taken:
                    return candidate
            cursor = cursors.get(scope) or self._random_date()
            for _ in range(len(taken) + 1):
                candidate = self._format_date(cursor, original)
                cursor += timedelta(days=1)
                if candidate not in taken:
                    cursors[scope] = cursor
                    return candidate
            raise _FormatExhausted()

        candidate = self.generate_fake_value(original, category, field_name)
        for _ in range(self.MAX_ATTEMPTS - 1):
            if candidate not in taken:
                return candidate
            candidate = self.generate_fake_value(original, category, field_name)
        if candidate not in taken:
            return candidate

        return self._disambiguate(candidate, category, taken)

    @staticmethod
    def _slots(candidate: str, letters: bool) -> list:
        """(position, alphabet) for each character a format-preserving fake randomises."""
        slots = []
        for i, ch in enumerate(candidate):
            if ch in string.digits:
                slots.append((i, string.digits))
            elif letters and ch in string.ascii_letters:
                slots.append((i, string.ascii_uppercase if ch.isupper() else string.ascii_lowercase))
        return slots

    @staticmethod
    def _disambiguate(candidate: str, category: str, taken: Set[str]) -> str:
        """Derive an unused variant of a colliding fake, deterministically."""
        slots = PIIGenerator._slots(candidate, letters=category == 'identifier')

        if slots and '@' not in candidate and category not in ('name', 'address'):
            # Format-preserving (identifiers, phones, digit-bearing generics):
            # count upward through the randomised slots, like an odometer.
            space = 1
            number = 0
            for pos, alphabet in slots:
                space *= len(alphabet)
                number = number * len(alphabet) + alphabet.index(candidate[pos])

            for _ in range(min(space, len(taken) + 1)):
                number = (number + 1) % space
                chars = list(candidate)
                remainder = number
                for pos, alphabet in reversed(slots):
                    remainder, index = divmod(remainder, len(alphabet))
                    chars[pos] = alphabet[index]
                variant = ''.join(chars)
                if variant not in taken:
                    return variant
            raise _FormatExhausted()

        # Free-form values (names, emails, addresses): add a counter.
        local, at, domain = candidate.partition('@')
        counter = 2
        while True:
            variant = f"{local}{counter}{at}{domain}" if at else f"{candidate} {counter}"
            if variant not in taken:
                return variant
            counter += 1

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
        col_name = column_info['column']
        category = column_info['category']

        # Build mapping from unique values
        mapping = self.build_mapping(df[col_name], category, col_name)

        if not mapping:
            return df, {}

        # Apply mapping using replace (vectorized, no row-wise UDF)
        df = df.with_columns(
            pl.col(col_name).replace(mapping).alias(col_name)
        )

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
        return [self.fake.address().replace('\n', ', ') for _ in range(n)]

    def generate_identifiers(self, n: int, length: int = 10) -> list:
        """Generate n fake identifiers of given length."""
        return [self.fake.numerify('#' * length) for _ in range(n)]
