"""
Free-text PII scrubber for the local LLM proxy (Phase 5).

Finds PII-shaped spans in prompt text with the 2.1 value detectors
(email, phone, credit-card/Luhn, IBAN, IPv4, URL-credentials, NIK, NPWP,
BPJS) plus mapping-known originals, replaces them with typed tokens
(<EMAIL_1>, <PERSON_1>, ...), and records the token→original mapping so
responses restore exactly via restore_text.

No NER model — deterministic shapes only. Anything the detectors miss
passes through unchanged (documented limitation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Dict, List, Optional, Tuple

from pii_faker.core import value_detectors as VD
from pii_faker.core.redactor import make_typed_token


@dataclass
class ScrubResult:
    scrubbed: str
    mapping: Dict[str, str]
    hits: List[Dict[str, str]] = dataclass_field(default_factory=list)


_SPAN_FINDERS: List[Tuple[str, str, str]] = [
    ("email", VD.EMAIL.pattern, "contact"),
    ("url_credentials", VD.URL_CREDENTIALS.pattern, "identifier"),
    ("iban", VD.IBAN.pattern, "identifier"),
    ("ipv4", VD.IPV4.pattern, "contact"),
    ("npwp", VD.NPWP.pattern, "identifier"),
    ("credit_card", VD.CREDIT_CARD.pattern, "identifier"),
    ("nik", VD.NIK.pattern, "identifier"),
    ("bpjs", VD.BPJS.pattern, "identifier"),
    ("phone", VD.PHONE.pattern, "contact"),
]

_TOKEN_LABEL = {
    "email": "EMAIL", "phone": "PHONE", "credit_card": "CARD",
    "iban": "IBAN", "ipv4": "IP", "url_credentials": "URL",
    "nik": "NIK", "npwp": "NPWP", "bpjs": "BPJS",
}


def _spans_for_detector(text: str, name: str, pattern: str) -> List[Tuple[int, int, str]]:
    out: List[Tuple[int, int, str]] = []
    try:
        rx = re.compile(pattern)
    except re.error:
        return out
    for m in rx.finditer(text):
        span = m.group(0)
        digits = re.sub(r"\D", "", span)
        if name == "credit_card":
            if not (13 <= len(digits) <= 19 and VD.luhn_ok(digits)):
                continue
        if name == "nik":
            if len(digits) == 16 and not VD.nik_ok(digits):
                continue
            if len(digits) != 16:
                continue
        if name == "bpjs" and len(digits) != 13:
            continue
        if name == "phone" and len(digits) < 7:
            continue
        out.append((m.start(), m.end(), span))
    return out


def scrub_text(
    text: str,
    known_originals: Optional[Dict[str, str]] = None,
    start_index: int = 1,
) -> ScrubResult:
    """Replace PII-shaped spans with typed tokens.

    known_originals maps original value → existing token so repeated
    prompts reuse the same token for the same value.
    """
    known_originals = known_originals or {}
    reverse_token = {tok: orig for orig, tok in known_originals.items()}

    spans: List[Tuple[int, int, str, str, str]] = []
    for name, pattern, category in _SPAN_FINDERS:
        for s, e, val in _spans_for_detector(text, name, pattern):
            spans.append((s, e, val, name, category))
    spans.sort(key=lambda x: (x[0], -(x[1] - x[0])))

    # Drop overlapping spans, longest-first at the same start.
    kept: List[Tuple[int, int, str, str, str]] = []
    occupied: List[Tuple[int, int]] = []
    for s, e, val, name, category in spans:
        if any(s < oe and e > os for os, oe in occupied):
            continue
        occupied.append((s, e))
        kept.append((s, e, val, name, category))

    mapping: Dict[str, str] = {}
    hits: List[Dict[str, str]] = []
    out: List[str] = []
    cursor = 0
    counter = start_index
    for s, e, val, name, category in kept:
        if val in known_originals:
            token = known_originals[val]
        elif val in mapping:
            token = mapping[val]
        else:
            label = _TOKEN_LABEL.get(name, "PII")
            token = f"<{label}_{counter}>"
            counter += 1
            mapping[val] = token
        if token in reverse_token and reverse_token[token] != val:
            label = _TOKEN_LABEL.get(name, "PII")
            token = f"<{label}_{counter}>"
            counter += 1
            mapping[val] = token
        out.append(text[cursor:s])
        out.append(token)
        cursor = e
        hits.append({"detector": name, "token": token})
    out.append(text[cursor:])
    return ScrubResult(scrubbed="".join(out), mapping=mapping, hits=hits)
