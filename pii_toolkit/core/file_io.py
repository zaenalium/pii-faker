"""
File I/O module for PII Toolkit.

Handles reading and writing various file formats using Polars.
Supports: CSV, TSV, Excel, JSON, NDJSON, Parquet.
"""

from pathlib import Path
from typing import Optional, List, Union, Dict, Any

import polars as pl

from pii_toolkit.core.config import PIIConfig


class FileHandler:
    """Handles file I/O for various formats."""

    SUPPORTED_EXTENSIONS = {
        '.csv': 'csv',
        '.tsv': 'csv',
        '.xlsx': 'excel',
        '.xls': 'excel',
        '.json': 'json',
        '.ndjson': 'ndjson',
        '.jsonl': 'ndjson',
        '.parquet': 'parquet',
    }

    def __init__(self, config: PIIConfig):
        self.config = config

    def detect_format(self, path: str) -> str:
        """Detect file format from extension."""
        ext = Path(path).suffix.lower()
        format_type = self.SUPPORTED_EXTENSIONS.get(ext)
        if format_type is None:
            raise ValueError(f"Unsupported file format: {ext}")
        return format_type

    def read(
        self,
        path: str,
        format: Optional[str] = None,
        sheets: Optional[str] = None,
        all_sheets: bool = False,
        infer_schema_length: Optional[int] = None,
    ) -> Union[pl.DataFrame, Dict[str, pl.DataFrame]]:
        """
        Read a file into Polars DataFrame(s).

        Args:
            path: Path to input file
            format: Override format detection
            sheets: Comma-separated sheet names for Excel
            all_sheets: Read all sheets from Excel
            infer_schema_length: Number of rows for JSON schema inference

        Returns:
            DataFrame or dict of {sheet_name: DataFrame} for Excel
        """
        if format is None:
            format = self.detect_format(path)

        if format == 'csv':
            return self._read_csv(path)
        elif format == 'excel':
            return self._read_excel(path, sheets, all_sheets)
        elif format == 'json':
            return self._read_json(path, infer_schema_length)
        elif format == 'ndjson':
            return self._read_ndjson(path, infer_schema_length)
        elif format == 'parquet':
            return self._read_parquet(path)
        else:
            raise ValueError(f"Unsupported format: {format}")

    def _read_csv(self, path: str) -> pl.DataFrame:
        """Read CSV/TSV file."""
        separator = '\t' if path.endswith('.tsv') else ','
        return pl.read_csv(path, separator=separator, infer_schema_length=10000)

    def _read_excel(
        self,
        path: str,
        sheets: Optional[str] = None,
        all_sheets: bool = False,
    ) -> Union[pl.DataFrame, Dict[str, pl.DataFrame]]:
        """Read Excel file. Returns dict if multiple sheets."""
        if all_sheets:
            # Read all sheets
            sheet_names = pl.read_excel(path, sheet_name=None).keys()
            return {
                name: pl.read_excel(path, sheet_name=name, engine='calamine')
                for name in sheet_names
            }
        elif sheets:
            # Read specified sheets
            sheet_list = [s.strip() for s in sheets.split(',')]
            if len(sheet_list) == 1:
                return pl.read_excel(path, sheet_name=sheet_list[0], engine='calamine')
            return {
                name: pl.read_excel(path, sheet_name=name, engine='calamine')
                for name in sheet_list
            }
        else:
            # Read first sheet
            return pl.read_excel(path, engine='calamine')

    def _read_json(
        self,
        path: str,
        infer_schema_length: Optional[int] = None,
    ) -> pl.DataFrame:
        """Read JSON file."""
        if infer_schema_length is None:
            infer_schema_length = self.config.infer_schema_length or 100

        try:
            return pl.read_json(path, infer_schema_length=infer_schema_length)
        except Exception as e:
            raise ValueError(f"Failed to read JSON file: {e}")

    def _read_ndjson(
        self,
        path: str,
        infer_schema_length: Optional[int] = None,
    ) -> pl.DataFrame:
        """Read NDJSON/JSONL file."""
        if infer_schema_length is None:
            infer_schema_length = self.config.infer_schema_length or 100

        try:
            return pl.read_ndjson(path, infer_schema_length=infer_schema_length)
        except Exception as e:
            raise ValueError(f"Failed to read NDJSON file: {e}")

    def _read_parquet(self, path: str) -> pl.DataFrame:
        """Read Parquet file."""
        return pl.read_parquet(path)

    def write(
        self,
        df: pl.DataFrame,
        path: str,
        format: Optional[str] = None,
    ):
        """
        Write DataFrame to file.

        Args:
            df: DataFrame to write
            path: Output file path
            format: Override format detection
        """
        if format is None:
            format = self.detect_format(path)

        if format == 'csv':
            self._write_csv(df, path)
        elif format == 'excel':
            self._write_excel(df, path)
        elif format == 'json':
            self._write_json(df, path)
        elif format == 'ndjson':
            self._write_ndjson(df, path)
        elif format == 'parquet':
            self._write_parquet(df, path)
        else:
            raise ValueError(f"Unsupported format: {format}")

    def _write_csv(self, df: pl.DataFrame, path: str):
        """Write CSV/TSV file."""
        separator = '\t' if path.endswith('.tsv') else ','
        df.write_csv(path, separator=separator)

    def _write_excel(self, df: pl.DataFrame, path: str):
        """Write Excel file using xlsxwriter."""
        df.write_excel(path)

    def _write_json(self, df: pl.DataFrame, path: str):
        """Write JSON file."""
        df.write_json(path)

    def _write_ndjson(self, df: pl.DataFrame, path: str):
        """Write NDJSON file."""
        df.write_ndjson(path)

    def _write_parquet(self, df: pl.DataFrame, path: str):
        """Write Parquet file."""
        df.write_parquet(path)

    def read_lazy(
        self,
        path: str,
        format: Optional[str] = None,
    ) -> pl.LazyFrame:
        """
        Read file as lazy frame for query optimization.

        Only supported for CSV, NDJSON, and Parquet.
        """
        if format is None:
            format = self.detect_format(path)

        if format == 'csv':
            separator = '\t' if path.endswith('.tsv') else ','
            return pl.scan_csv(path, separator=separator)
        elif format == 'ndjson':
            return pl.scan_ndjson(path)
        elif format == 'parquet':
            return pl.scan_parquet(path)
        else:
            # Fall back to eager read for unsupported lazy formats
            df = self.read(path, format=format)
            return df.lazy()
