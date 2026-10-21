"""
Value-based PII detectors (roadmap 2.1).

Vectorised `str.contains` checks run over a sample of each column's values
so detection doesn't depend on column names. Each detector returns a
(value_match_ratio, category) pair used for confidence scoring (2.2) and
the leak check (2.3).
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

import polars as pl

SAMPLE_SIZE = 500
MIN_RATIO = 0.3


@dataclass(frozen=True)
class ValueDetector:
    """One value-shape detector: regex + PII category + description."""

    name: str
    pattern: str
    category: str
    description: str = ""

    def ratio(self, sample: pl.Series) -> float:
        """Fraction of non-null sampled values matching the pattern."""
        try:
            matched = sample.str.contains(self.pattern).fill_null(False)
        except Exception:
            return 0.0
        if len(matched) == 0:
            return 0.0
        return float(matched.mean())


EMAIL = ValueDetector(
    "email",
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "contact",
    "Email address shape",
)
PHONE = ValueDetector(
    "phone",
    r"(\+?\d[\d\s\-().]{6,}\d)",
    "contact",
    "Phone number shape",
)
CREDIT_CARD = ValueDetector(
    "credit_card",
    r"\b(?:\d[ \-]?){13,19}\b",
    "identifier",
    "Credit card digit shape (Luhn-verified)",
)
IBAN = ValueDetector(
    "iban",
    r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
    "identifier",
    "IBAN shape",
)
IPV4 = ValueDetector(
    "ipv4",
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "contact",
    "IPv4 address shape",
)
URL_CREDENTIALS = ValueDetector(
    "url_credentials",
    r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:]+:[^/\s@]+@",
    "identifier",
    "URL with embedded credentials",
)
NIK = ValueDetector(
    "nik",
    r"\b\d{16}\b",
    "identifier",
    "Indonesian NIK shape (16 digits)",
)
NPWP = ValueDetector(
    "npwp",
    r"\b\d{2}\.\d{3}\.\d{3}\.\d-\d{3}\.\d{3}\b",
    "identifier",
    "Indonesian NPWP shape",
)
BPJS = ValueDetector(
    "bpjs",
    r"\b\d{13}\b",
    "identifier",
    "Indonesian BPJS shape (13 digits)",
)

DETECTORS: List[ValueDetector] = [
    EMAIL,
    PHONE,
    CREDIT_CARD,
    IBAN,
    IPV4,
    URL_CREDENTIALS,
    NIK,
    NPWP,
    BPJS,
]

BY_NAME: Dict[str, ValueDetector] = {d.name: d for d in DETECTORS}


def _sample_strings(series: pl.Series, n: int = SAMPLE_SIZE) -> Optional[pl.Series]:
    """First-n non-null values cast to string; None if nothing usable."""
    try:
        sample = series.drop_nulls().head(n)
        if len(sample) == 0:
            return None
        return sample.cast(pl.Utf8)
    except Exception:
        return None


def luhn_ok(digits: str) -> bool:
    """Luhn checksum for a digit string."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _credit_card_ratio(sample: pl.Series) -> float:
    """Ratio of values containing a Luhn-valid 13–19 digit run."""
    hits = 0
    count = 0
    for val in sample.to_list():
        if not isinstance(val, str):
            continue
        count += 1
        for run in re.findall(r"\d(?:[ \-]?\d){12,18}", val):
            digits = re.sub(r"\D", "", run)
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                hits += 1
                break
    return hits / count if count else 0.0


def nik_ok(value: str) -> bool:
    """Plausibility check for an Indonesian NIK: region + date parts."""
    digits = re.sub(r"\D", "", value)
    if len(digits) != 16 or not digits.isdigit():
        return False
    if digits.startswith("00"):
        return False
    day = int(digits[6:8])
    month = int(digits[8:10])
    if not 1 <= month <= 12:
        return False
    if not (1 <= day <= 31 or 41 <= day <= 71):
        return False
    return True


def _nik_ratio(sample: pl.Series) -> float:
    hits = 0
    count = 0
    for val in sample.to_list():
        if not isinstance(val, str):
            continue
        for run in re.findall(r"\b\d{16}\b", val):
            count += 1
            if nik_ok(run):
                hits += 1
                break
        else:
            if re.search(r"\d", val):
                count += 1
    return hits / count if count else 0.0


def detect_value_shapes(
    series: pl.Series,
    min_ratio: float = MIN_RATIO,
    sample_size: int = SAMPLE_SIZE,
) -> List[Dict[str, object]]:
    """
    Run all value detectors over a sample of a Series.

    Returns a list of {detector, category, ratio, description} for detectors
    meeting min_ratio, sorted by ratio descending.
    """
    sample = _sample_strings(series, sample_size)
    if sample is None:
        return []
    results = []
    for detector in DETECTORS:
        if detector.name == "credit_card":
            ratio = _credit_card_ratio(sample)
        elif detector.name == "nik":
            ratio = _nik_ratio(sample)
        else:
            ratio = detector.ratio(sample)
        if ratio >= min_ratio:
            results.append({
                "detector": detector.name,
                "category": detector.category,
                "ratio": ratio,
                "description": detector.description,
            })
    results.sort(key=lambda r: r["ratio"], reverse=True)
    return results


def scan_frame_values(
    df: pl.DataFrame,
    min_ratio: float = MIN_RATIO,
    sample_size: int = SAMPLE_SIZE,
) -> Dict[str, List[Dict[str, object]]]:
    """Run value-shape detection over every top-level column of a frame."""
    findings: Dict[str, List[Dict[str, object]]] = {}
    for col_name in df.columns:
        matches = detect_value_shapes(df[col_name], min_ratio, sample_size)
        if matches:
            findings[col_name] = matches
    return findings


ID_LOCALE_HINTS = frozenset({"nik", "npwp", "bpjs"})

ID_COLUMN_HINTS = frozenset({
    "nik", "noktp", "no_ktp", "ktp", "npwp", "bpjs", "no_bpjs", "nobpjs",
    "nama", "nama_lengkap", "alamat", "tanggal_lahir", "no_hp", "nohp",
    "no_telepon", "notelepon",
})


def suggest_locale(
    value_findings: Optional[Dict[str, List[Dict[str, object]]]] = None,
    columns: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Suggest a Faker locale from detected identifiers (roadmap 2.5).

    Returns "id_ID" when Indonesian identifiers (NIK/NPWP/BPJS shapes or
    Indonesian column names) are present, else None (keep en_US default).
    """
    if value_findings:
        for matches in value_findings.values():
            for match in matches:
                if match.get("detector") in ID_LOCALE_HINTS:
                    return "id_ID"
    if columns:
        import re

        for col in columns:
            normalized = re.sub(r"[\s_\-]+", "", col.lower())
            if normalized in ID_COLUMN_HINTS:
                return "id_ID"
    return None
