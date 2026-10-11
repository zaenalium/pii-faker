"""Tests for Phase 2 detection: value shapes, confidence, leak check, locale."""

import polars as pl

from pii_faker.core import process_dataframe, restore_dataframe
from pii_faker.core.config import PIIConfig, MatchStrategy
from pii_faker.core.detector import PIIColumnDetector
from pii_faker.core.leak_check import (
    check_processed_frame,
    check_sheets_with_mapper,
)
from pii_faker.core.pipeline import plan_dataframe, process_sheets
from pii_faker.core.value_detectors import (
    detect_value_shapes,
    luhn_ok,
    nik_ok,
    suggest_locale,
)


class TestValueDetectors:
    def test_email_shape(self):
        s = pl.Series(["a@x.com", "b@y.org", "not-an-email"])
        hits = detect_value_shapes(s)
        assert hits and hits[0]["detector"] == "email"

    def test_phone_shape(self):
        s = pl.Series(["0812-3456-7890", "+62 21 555 0100", "nothing"])
        assert any(h["detector"] == "phone" for h in detect_value_shapes(s))

    def test_credit_card_luhn(self):
        s = pl.Series(["4111-1111-1111-1111", "1234-5678-9012-3456", "nope"])
        hits = {h["detector"]: h for h in detect_value_shapes(s)}
        assert "credit_card" in hits
        assert hits["credit_card"]["ratio"] < 1.0

    def test_luhn_vectors(self):
        assert luhn_ok("4111111111111111")
        assert luhn_ok("5500000000000004")
        assert not luhn_ok("4111111111111112")

    def test_nik_shape_and_plausibility(self):
        assert nik_ok("3174051209900001")
        assert not nik_ok("0014051209900001")
        s = pl.Series(["3174051209900001", "3174051209900002", "x"])
        assert detect_value_shapes(s)[0]["detector"] == "nik"

    def test_no_false_positive_on_names(self):
        s = pl.Series(["John", "Jane", "Bob"])
        assert detect_value_shapes(s) == []

    def test_suggest_locale(self):
        assert suggest_locale(columns=["nik", "name"]) == "id_ID"
        assert suggest_locale(columns=["name", "email"]) is None


class TestConfidenceDetection:
    def test_value_only_column_detected(self):
        df = pl.DataFrame({"col_7": ["a@x.com", "b@y.org"], "age": [1, 2]})
        findings = PIIColumnDetector(PIIConfig()).detect_with_confidence(df)
        cols = {f["column"]: f for f in findings}
        assert "col_7" in cols
        assert cols["col_7"]["name_match"] is False
        assert cols["col_7"]["value_detector"] == "email"
        assert "age" not in cols

    def test_planned_target_carries_confidence(self):
        df = pl.DataFrame({"col_7": ["a@x.com", "b@y.org"]})
        plan = plan_dataframe(df, PIIConfig())
        assert len(plan.targets) == 1
        assert plan.targets[0].name_match is False
        assert 0.0 < plan.targets[0].confidence <= 1.0


class TestFuzzyTightening:
    def test_no_fp_on_username_filename(self):
        d = PIIColumnDetector(PIIConfig(match_strategy=MatchStrategy.FUZZY))
        df = pl.DataFrame({"username": ["a"], "filename": ["b"], "company_name": ["c"]})
        assert d.detect(df) == []

    def test_still_matches_compounds(self):
        d = PIIColumnDetector(PIIConfig(match_strategy=MatchStrategy.FUZZY))
        df = pl.DataFrame({
            "patients_full_name_2024": ["a"],
            "phone_number": ["b"],
        })
        assert len(d.detect(df)) == 2


class TestLeakCheck:
    def test_clean_run(self):
        df = pl.DataFrame({"email": ["a@x.com", "b@y.org"]})
        out, mapper = process_dataframe(df, PIIConfig(seed=1))
        reports = check_sheets_with_mapper({"default": out}, mapper, True)
        assert reports["default"].clean

    def test_deselected_column_flagged(self):
        df = pl.DataFrame({"email": ["a@x.com", "b@y.org"]})
        processed, _, run_state = process_sheets(
            {"default": df}, PIIConfig(seed=1), selected=[]
        )
        assert any("leak check" in w for w in run_state.warnings)
        _ = processed

    def test_original_value_found(self):
        df = pl.DataFrame({"email": ["a@x.com"]})
        report = check_processed_frame(df, {"a@x.com"}, set())
        assert not report.clean
        assert report.findings[0].kind == "original_value"


class TestLocale:
    def test_id_locale_produces_local_names(self):
        df = pl.DataFrame({"nama": ["Budi", "Sari"]})
        out, _ = process_dataframe(df, PIIConfig(seed=1, locale="id_ID"))
        out_en, _ = process_dataframe(df, PIIConfig(seed=1))
        assert out["nama"].to_list() != out_en["nama"].to_list()

    def test_locale_round_trip(self):
        df = pl.DataFrame({"nama": ["Budi", "Sari"]})
        out, mapper = process_dataframe(df, PIIConfig(seed=1, locale="id_ID"))
        assert restore_dataframe(out, mapper).to_dicts() == df.to_dicts()
