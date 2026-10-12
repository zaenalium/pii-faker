"""Phase 4 tests: tolerant restore, shift/perturb modes, project reuse, spans."""

import datetime

import polars as pl

from pii_faker.core.config import PIIConfig
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.pipeline import process_sheets
from pii_faker.core.restorer import PIIRestorer


def _mapper():
    m = PIIMapper(PIIConfig())
    m.add_column_mapping("name", "name", {"Alice Smith": "Zara Quinn", "Bob Jones": "Mike Ross"})
    return m


def test_tolerant_case_and_possessive():
    r = PIIRestorer(_mapper())
    out, spans = r.restore_text_with_spans("zara quinn's friend MIKE ROSS", tolerant=True)
    assert out == "Alice Smith's friend Bob Jones"
    assert len(spans) == 2


def test_tolerant_first_name_only():
    r = PIIRestorer(_mapper())
    out, _ = r.restore_text_with_spans("Zara went with Mike", tolerant=True)
    assert out == "Alice Smith went with Bob Jones"


def test_tolerant_ambiguous_left_alone():
    m = PIIMapper(PIIConfig())
    m.add_column_mapping("name", "name", {"Alice Smith": "Zara Quinn", "Alice Wong": "Zara Lin"})
    r = PIIRestorer(m)
    out, spans = r.restore_text_with_spans("Zara arrived", tolerant=True)
    assert out == "Zara arrived" and spans == []
    assert "zara" in r.ambiguous_hits


def test_spans_offsets_exact():
    r = PIIRestorer(_mapper())
    out, spans = r.restore_text_with_spans("Hi Zara Quinn!")
    assert out == "Hi Alice Smith!"
    assert spans == [(3, 14, "Alice Smith")]


def test_shift_preserves_intervals_and_restores():
    cfg = PIIConfig(mode="shift", shift_days=10, seed=1)
    df = pl.DataFrame({"dob": [datetime.date(1990, 1, 1), datetime.date(1990, 1, 15)]})
    out, mapper, _ = process_sheets({"default": df}, cfg)
    shifted = out["default"]["dob"].to_list()
    assert (shifted[1] - shifted[0]).days == 14
    assert shifted[0] == datetime.date(1990, 1, 11)
    restored = PIIRestorer(mapper).restore_dataframe(out["default"])["dob"].to_list()
    assert restored == [datetime.date(1990, 1, 1), datetime.date(1990, 1, 15)]


def test_perturb_scales_and_restores():
    cfg = PIIConfig(mode="perturb", perturb_pct=10, seed=1)
    df = pl.DataFrame({"salary": [1000, 2000]})
    out, mapper, _ = process_sheets({"default": df}, cfg)
    vals = out["default"]["salary"].to_list()
    assert all(900 <= v <= 1100 or 1800 <= v <= 2200 for v in vals)
    restored = PIIRestorer(mapper).restore_dataframe(out["default"])["salary"].to_list()
    assert restored == [1000, 2000]


def test_project_reuse_same_fake(tmp_path):
    proj = tmp_path / "proj.json"
    cfg1 = PIIConfig(seed=1, project_mapping=str(proj))
    a = pl.DataFrame({"nama": ["Budi"]})
    _, _, _ = process_sheets({"default": a}, cfg1, mapping_out=str(tmp_path / "m1.json"))
    cfg2 = PIIConfig(seed=1, project_mapping=str(proj))
    b = pl.DataFrame({"nama": ["Budi", "Sari"]})
    out, _, _ = process_sheets({"default": b}, cfg2, mapping_out=str(tmp_path / "m2.json"))
    first = out["default"]["nama"].to_list()[0]
    m1 = PIIMapper.load(str(tmp_path / "m1.json")).store.get_mapping("nama")
    assert m1["Budi"] == first
