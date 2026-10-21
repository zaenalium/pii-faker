"""Phase 2.6 tests: free-text NER (mocked spaCy so CI has no model)."""

import polars as pl
import pytest

from pii_faker.core.config import PIIConfig
from pii_faker.core.restorer import PIIRestorer


class FakeSpan:
    def __init__(self, text, label, start, end):
        self.text = text
        self.label = label
        self.category = "name"
        self.start = start
        self.end = end


def _fake_extract_spans(text):
    if "Alice" in text:
        s = text.index("Alice")
        return [FakeSpan("Alice Smith", "PERSON", s, s + len("Alice Smith"))]
    return []


@pytest.fixture
def with_fake_ner(monkeypatch):
    import sys, types
    mod = types.ModuleType("pii_faker.core.ner")
    mod.extract_spans = _fake_extract_spans
    mod.column_looks_like_prose = lambda *a, **k: True
    monkeypatch.setitem(sys.modules, "pii_faker.core.ner", mod)
    yield mod


def test_free_text_fakes_and_restores(with_fake_ner):
    from pii_faker.core.pipeline import process_sheets

    cfg = PIIConfig(ner=True, seed=1)
    df = pl.DataFrame({"notes": ["Alice Smith visited the clinic", "No one here"]})
    out, mapper, _ = process_sheets({"default": df}, cfg)
    vals = out["default"]["notes"].to_list()
    assert "Alice Smith" not in vals[0] and "No one here" == vals[1]
    assert any(e.get("kind") == "free_text" for e in mapper.store._entries.values())

    restored = PIIRestorer(mapper).restore_dataframe(out["default"])["notes"].to_list()
    assert restored == ["Alice Smith visited the clinic", "No one here"]


def test_without_ner_leaves_prose_alone():
    from pii_faker.core.pipeline import process_sheets

    cfg = PIIConfig(seed=1)
    df = pl.DataFrame({"notes": ["Alice Smith visited the clinic"]})
    out, mapper, _ = process_sheets({"default": df}, cfg)
    assert out["default"]["notes"].to_list() == ["Alice Smith visited the clinic"]
    assert not any(e.get("kind") == "free_text" for e in mapper.store._entries.values())


def test_ner_skipped_when_not_prose(monkeypatch):
    import sys, types
    mod = types.ModuleType("pii_faker.core.ner")
    mod.extract_spans = _fake_extract_spans
    mod.column_looks_like_prose = lambda *a, **k: False
    monkeypatch.setitem(sys.modules, "pii_faker.core.ner", mod)

    from pii_faker.core.pipeline import process_sheets

    cfg = PIIConfig(ner=True, seed=1)
    df = pl.DataFrame({"notes": ["Alice Smith visited"]})
    out, mapper, _ = process_sheets({"default": df}, cfg)
    assert not any(e.get("kind") == "free_text" for e in mapper.store._entries.values())
