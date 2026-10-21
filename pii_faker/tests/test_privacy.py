"""Fakes must never reveal the original, and mapping files must stay private."""

import itertools
import os
import stat
import string
import sys
from datetime import date, timedelta

import polars as pl
import pytest

from pii_faker.core.config import PIIConfig, PIIMode, RedactionStyle
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.mapper import AmbiguousMappingError, PIIMapper
from pii_faker.core.pipeline import _native_fallback, process_dataframe, process_sheets, restore_dataframe
from pii_faker.core.state import RunState


def run(df, **kwargs):
    config = PIIConfig(seed=1, **kwargs)
    processed, mapper = process_dataframe(df, config)
    return processed, mapper


def embedded(original, fake):
    """True if the fake is, or contains, the original value."""
    return str(original) in str(fake)


class TestFakesNeverContainTheOriginal:
    """When random draws run dry (dense codes, a full date range, digit-free ids) the old
    fallback built the fake from the original: `identifier:0006#F1`, `date:1918-05-24#F1`."""

    @pytest.mark.parametrize("width, count", [(2, 90), (3, 900)])
    def test_dense_numeric_codes(self, width, count):
        low = 10 ** (width - 1)
        codes = [str(low + i) for i in range(count)]                     # fills 90% of the format
        processed, mapper = run(pl.DataFrame({"member_code": codes}))

        fakes = processed["member_code"].to_list()
        assert not any(embedded(o, f) for o, f in zip(codes, fakes))
        assert all(len(f) == width and f.isdigit() for f in fakes)       # still the original's format
        assert len(set(fakes)) == count
        assert not any(o == f for o, f in zip(codes, fakes))             # nobody keeps their own code
        assert restore_dataframe(processed, mapper)["member_code"].to_list() == codes

    def test_digit_free_identifiers(self):
        codes = ["ABCDEF", "ZZZZZZ", "KLMNOP"] + [f"CODE{chr(65 + i)}" for i in range(20)]
        processed, mapper = run(pl.DataFrame({"member_code": codes}))
        fakes = processed["member_code"].to_list()
        assert not any(embedded(o, f) for o, f in zip(codes, fakes))
        assert len(set(fakes)) == len(codes)
        assert restore_dataframe(processed, mapper)["member_code"].to_list() == codes

    def test_a_full_letter_space_still_maps_one_to_one(self):
        codes = ["".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)]   # all 676
        processed, mapper = run(pl.DataFrame({"member_code": codes}))
        fakes = processed["member_code"].to_list()
        assert len(set(fakes)) == 676
        assert all(len(f) == 2 and f.isalpha() and f.isupper() for f in fakes)
        assert not any(o == f for o, f in zip(codes, fakes))
        assert restore_dataframe(processed, mapper)["member_code"].to_list() == codes

    def test_dates_beyond_the_random_range(self):
        days = [(date(1900, 1, 1) + timedelta(days=i)).isoformat() for i in range(30000)]
        processed, mapper = run(pl.DataFrame({"dob": days}))
        fakes = processed["dob"].to_list()
        assert not any(embedded(o, f) for o, f in zip(days, fakes))
        assert len(set(fakes)) == len(days) and all(len(f) == 10 and f[4] == "-" for f in fakes)
        assert restore_dataframe(processed, mapper)["dob"].to_list() == days

    def test_native_date_columns(self):
        days = [date(1900, 1, 1) + timedelta(days=i) for i in range(30000)]
        processed, mapper = run(pl.DataFrame({"dob": days}))
        fakes = processed["dob"].to_list()
        assert processed.schema["dob"] == pl.Date
        assert not any(o == f for o, f in zip(days, fakes))              # used to return the real date
        assert len(set(fakes)) == len(days)
        assert restore_dataframe(processed, mapper)["dob"].to_list() == days

    def test_native_integer_ids(self):
        ids = list(range(100, 1000))                                     # 900 of the 1,000 three-digit values
        processed, mapper = run(pl.DataFrame({"member_code": ids}))
        fakes = processed["member_code"].to_list()
        assert processed.schema["member_code"] == pl.Int64
        assert not any(o == f for o, f in zip(ids, fakes))
        assert len(set(fakes)) == len(ids)
        assert restore_dataframe(processed, mapper)["member_code"].to_list() == ids

    @pytest.mark.parametrize("original", [777, 777.5, date(1984, 3, 12)])
    def test_last_resort_native_fallbacks_are_unrelated_to_the_original(self, original):
        generator = PIIGenerator(PIIConfig(seed=1), RunState())
        coerce = {int: lambda t: int("".join(c for c in t if c.isdigit()) or 0),
                  float: lambda t: float("".join(c for c in t if c.isdigit()) or 0),
                  date: lambda t: date.fromisoformat(t[:10])}[type(original)]
        fallback = _native_fallback(original, generator, coerce, "")      # no usable fake text: skip the search
        assert fallback != original
        if not isinstance(original, date):
            assert abs(fallback - original) > 1000                        # used to be original + n

    def test_run_warnings_never_quote_a_value(self):
        codes = ["".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)]
        _, _, state = process_sheets({"s": pl.DataFrame({"member_code": codes})}, PIIConfig(seed=1))
        assert state.warnings, "this volume should exhaust the random draws"
        assert not any(code in warning for warning in state.warnings for code in codes[:50])


class TestIdentifiers:
    def fake(self, original, seed=1):
        return PIIGenerator(PIIConfig(seed=seed)).generate_fake_value(original, "identifier", "member_code")

    def test_identifiers_without_digits_are_faked(self):
        assert self.fake("ABCDEF") != "ABCDEF"

    def test_letters_are_randomised_along_with_digits(self):
        assert any(self.fake("M-001", seed=seed)[0] != "M" for seed in range(20))

    @pytest.mark.parametrize("original", ["ABCDEF", "M-001", "ab-Cd 12", "JKT/2024/0042", "x"])
    def test_shape_case_and_punctuation_are_kept(self, original):
        fake = self.fake(original)
        assert len(fake) == len(original)
        for before, after in zip(original, fake):
            assert before.isdigit() == after.isdigit()
            assert before.isalpha() == after.isalpha() and before.isupper() == after.isupper()
            if not before.isalnum():
                assert before == after


class TestMappingFiles:
    @pytest.fixture
    def mapper(self):
        _, mapper = run(pl.DataFrame({"patient_name": ["Al Bo", "Cy Di"]}))
        return mapper

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.parametrize("name", ["m.json", "m.db", "m.sqlite"])
    def test_files_are_owner_only(self, tmp_path, mapper, name):
        path = tmp_path / name
        mapper.save(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.parametrize("name", ["m.json", "m.db"])
    def test_encrypted_files_are_owner_only_too(self, tmp_path, mapper, name):
        pytest.importorskip("cryptography")
        path = tmp_path / name
        mapper.save(str(path), encrypt=True, passphrase="correct horse battery staple")
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.parametrize("name", ["m.json", "m.db"])
    def test_an_existing_world_readable_file_is_tightened(self, tmp_path, mapper, name):
        path = tmp_path / name
        path.write_bytes(b"" if name.endswith(".db") else b"{}")       # an earlier, looser file
        os.chmod(path, 0o644)
        mapper.save(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.parametrize("name", ["m.json", "m.db"])
    def test_mapping_still_loads(self, tmp_path, mapper, name):
        path = str(tmp_path / name)
        mapper.save(path)
        assert PIIMapper.load(path).store.get_mapping("patient_name") == mapper.store.get_mapping("patient_name")


class TestAmbiguousMappings:
    @staticmethod
    def mapper(clash, **config):
        mapper = PIIMapper(PIIConfig(**config))
        mapper.add_column_mapping("n", "name", {"a": "Z", "b": "Z"} if clash else {"a": "Y", "b": "Z"})
        return mapper

    def test_refused_and_nothing_is_written(self, tmp_path):
        path = tmp_path / "m.json"
        with pytest.raises(AmbiguousMappingError, match="share one replacement"):
            self.mapper(True, mode="fake").save(str(path))
        assert not path.exists()

    @pytest.mark.parametrize("style", ["token", "typed-token"])
    def test_token_modes_are_checked_too(self, tmp_path, style):
        with pytest.raises(AmbiguousMappingError):
            self.mapper(True, mode="redact", redact_style=style).save(str(tmp_path / "m.json"))

    @pytest.mark.parametrize("style", ["full", "partial"])
    def test_lossy_redaction_may_collapse_values(self, tmp_path, style):
        self.mapper(True, mode="redact", redact_style=style).save(str(tmp_path / "m.json"))

    def test_a_clean_mapping_saves(self, tmp_path):
        self.mapper(False, mode="fake").save(str(tmp_path / "m.json"))

    def test_lossless_mode_flag(self):
        assert PIIMapper(PIIConfig(mode="fake")).is_lossless_mode()
        assert PIIMapper(PIIConfig(mode="redact", redact_style="typed-token")).is_lossless_mode()
        assert not PIIMapper(PIIConfig(mode="redact", redact_style="full")).is_lossless_mode()


class TestNestedListsAreProcessed:
    """Auto-detected fields inside a list of structs were mapped, saved and then never applied:
    the output kept the real values, the mapping claimed they were faked, and an unchanged
    column "restores exactly", so the round-trip tests could not see it."""

    @staticmethod
    def frame():
        return pl.DataFrame([
            {"patient": {"patient_name": "John Smith",
                         "contacts": [{"email": "a@b.com"}, {"email": "c@d.com"}]}, "id": 1},
            {"patient": {"patient_name": "Jane Doe", "contacts": [{"email": "e@f.com"}]}, "id": 2},
        ])

    @staticmethod
    def emails(frame):
        return [item["email"] for row in frame["patient"].to_list() for item in row["contacts"]]

    def test_auto_detected_list_fields_are_faked(self):
        original = self.frame()
        processed, mapper = run(original)
        assert not set(self.emails(processed)) & set(self.emails(original))
        assert [r["patient_name"] for r in processed["patient"].to_list()] != ["John Smith", "Jane Doe"]
        assert restore_dataframe(processed, mapper).equals(original)

    @pytest.mark.parametrize("selected", [
        ["patient.patient_name", "patient.contacts[*].email"],
        ["patient.patient_name", "patient.contacts.email"],            # the form the web UI offers
    ])
    def test_selected_list_fields_are_faked_in_either_notation(self, selected):
        original = self.frame()
        processed, mapper = process_dataframe(original, PIIConfig(seed=1), selected=selected)
        assert not set(self.emails(processed)) & set(self.emails(original))
        assert restore_dataframe(processed, mapper).equals(original)

    def test_detected_paths_carry_the_list_marker(self):
        from pii_faker.core.pipeline import plan_dataframe
        paths = [t.path for t in plan_dataframe(self.frame(), PIIConfig(seed=1), None).targets]
        assert paths == ["patient.patient_name", "patient.contacts[*].email"]

    def test_nothing_real_is_left_anywhere_in_the_output(self):
        original = self.frame()
        processed, _ = run(original)
        text = str(processed.to_dicts())
        for real in ("John Smith", "Jane Doe", "a@b.com", "c@d.com", "e@f.com"):
            assert real not in text


class TestLeakCheckSeesInsideLists:
    """The leak check stringified list-of-struct elements: it flagged a correctly faked list, and
    could not find an original inside one except as a vague shape warning."""

    def test_a_correctly_processed_list_is_clean(self):
        _, _, state = process_sheets({"s": TestNestedListsAreProcessed.frame()}, PIIConfig(seed=1))
        assert not [w for w in state.warnings if "leak check" in w]

    def test_an_original_left_inside_a_list_is_a_definite_leak(self):
        from pii_faker.core.leak_check import check_processed_frame

        frame = TestNestedListsAreProcessed.frame()                      # not processed at all
        report = check_processed_frame(
            frame, originals={"a@b.com"}, processed_paths={"patient.contacts[*].email"})
        found = [f for f in report.findings if f.kind == "original_value"]
        assert [f.path for f in found] == ["patient.contacts[*].email"]
        assert not report.clean

    def test_a_processed_path_is_not_flagged_by_shape(self):
        from pii_faker.core.leak_check import check_processed_frame

        processed, _ = run(TestNestedListsAreProcessed.frame())
        report = check_processed_frame(
            processed, originals={"a@b.com"}, processed_paths={"patient.contacts[*].email", "patient.patient_name"})
        assert report.clean
