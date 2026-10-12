"""
Optional free-text NER for PII inside text columns (roadmap 2.6).

Enabled only with `pip install pii-faker[nlp]` — spaCy is a heavy optional
dep so the desktop bundle and default install stay small. When not installed,
this module raises a single ImportError with install instructions and nothing
else touches spaCy. This module is the ONLY place that imports spacy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

# Labels we treat as PII. Subset of spaCy's OntoNotes + Indonesian hints.
CATEGORY_FOR_LABEL: Dict[str, str] = {
    "PERSON": "name",
    "PER": "name",
    "ORG": "name",
    "GPE": "address",
    "LOC": "address",
    "FAC": "address",
    "ADDRESS": "address",
}

PII_LABELS = set(CATEGORY_FOR_LABEL)

_MODEL_NAME = "en_core_web_sm"
_nlp = None


@dataclass(frozen=True)
class NERSpan:
    text: str
    label: str
    category: str
    start: int
    end: int


def require_spacy():
    """Load spaCy lazily, or raise with install hint."""
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy
    except ImportError as e:
        raise ImportError(
            "NER requires `pip install pii-faker[nlp]` (spaCy + en_core_web_sm). "
            "Without it, value-based detectors (email/phone/NIK etc.) still work."
        ) from e
    try:
        _nlp = spacy.load(_MODEL_NAME)
    except OSError as e:
        raise ImportError(
            f"spaCy model {_MODEL_NAME!r} not found. Install it with: "
            f"python -m spacy download {_MODEL_NAME}"
        ) from e
    return _nlp


def extract_spans(
    text: str,
    *,
    labels: Optional[set[str]] = None,
    min_len: int = 2,
) -> List[NERSpan]:
    """Run NER over text, returning PII spans with category.

    Only spans whose label maps to a PII category are kept. The caller is
    responsible for deciding whether a column is prose (e.g. by sampling);
    this function just extracts spans from a single string.
    """
    nlp = require_spacy()
    wanted = labels or PII_LABELS
    doc = nlp(text)
    out: List[NERSpan] = []
    for ent in doc.ents:
        if ent.label_ not in wanted:
            continue
        if len(ent.text.strip()) < min_len:
            continue
        out.append(NERSpan(
            text=ent.text,
            label=ent.label_,
            category=CATEGORY_FOR_LABEL[ent.label_],
            start=ent.start_char,
            end=ent.end_char,
        ))
    return out


def column_looks_like_prose(sample: List[str], *, min_words: int = 4, ratio: float = 0.3) -> bool:
    """Heuristic: column is prose if ≥ ratio of samples have ≥ min_words tokens."""
    if not sample:
        return False
    hits = sum(1 for s in sample if len(s.split()) >= min_words)
    return hits / len(sample) >= ratio
