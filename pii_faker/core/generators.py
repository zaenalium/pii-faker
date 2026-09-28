"""
PII fake data generation module.

Generates realistic fake data based on PII category.
Uses the unique-values-first approach for performance.
"""

from typing import Dict, Any, Optional, Tuple
from functools import lru_cache

import polars as pl
from faker import Faker

from pii_faker.core.config import PIIConfig


class PIIGenerator:
    """Generates fake PII data based on category."""

    def __init__(self, config: PIIConfig):
        self.config = config
        self.fake = Faker()
        if config.seed is not None:
            self.fake.seed_instance(config.seed)

    def _generate_name(self, original: str) -> str:
        """Generate a fake name."""
        return self.fake.name()

    def _generate_identifier(self, original: str) -> str:
        """Generate a fake identifier preserving format."""
        # Preserve length and digit pattern
        result = []
        for char in original:
            if char.isdigit():
                result.append(str(self.fake.random_digit()))
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

    def _generate_date(self, original: str) -> str:
        """Generate a fake date preserving format."""
        # Detect common date formats
        import re

        # Try to detect separator and format
        if '/' in original:
            sep = '/'
        elif '-' in original:
            sep = '-'
        elif '.' in original:
            sep = '.'
        else:
            sep = '-'

        parts = re.split(r'[/\-.]', original)

        # Generate fake date
        fake_date = self.fake.date_of_birth(minimum_age=18, maximum_age=90)

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

        Returns:
            Dict mapping original -> fake
        """
        # Get unique non-null values and sort for consistent ordering.
        # key=str keeps mixed-type columns (str + int) from raising TypeError.
        unique_values = sorted(series.drop_nulls().unique().to_list(), key=str)

        # Build mapping for unique values only
        mapping = {}
        for original in unique_values:
            if isinstance(original, str):
                fake_value = self.generate_fake_value(original, category, field_name)
                mapping[original] = fake_value

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
