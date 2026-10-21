"""Tests that fake and token output can always be restored exactly."""

import itertools
import json
import os
import stat
import string
import sys
import time
from datetime import date, timedelta

import polars as pl
import pytest

from pii_faker.core import PIIConfig, PIIMapper, PIIProcessor, process_dataframe, restore_dataframe
from pii_faker.core.config import PIIMode, RedactionStyle
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.mapper import AmbiguousMappingError
from pii_faker.core.redactor import PIIRedactor
from pii_faker.core.restorer import PIIRestorer


class TestUniqueFakes:
    """Faker's pools are small: without a uniqueness guarantee, thousands of
    distinct originals collapse onto shared fakes and cannot be told apart."""

    def test_names_stay_distinct_at_scale(self):
        values = [f"Person {i}" for i in range(8000)]
        mapping = PIIGenerator(PIIConfig(seed=1)).build_mapping(pl.Series(values), "name", "name")
        assert len(set(mapping.values())) == len(values)

    def test_emails_stay_distinct_at_scale(self):
        values = [f"user{i}@corp.com" for i in range(8000)]
        mapping = PIIGenerator(PIIConfig(seed=1)).build_mapping(pl.Series(values), "contact", "email")
        assert len(set(mapping.values())) == len(values)
        assert all('@' in fake for fake in mapping.values())

    def test_fixed_width_identifiers_keep_their_format(self):
        values = [f"{i:04d}" for i in range(9000)]
        mapping = PIIGenerator(PIIConfig(seed=1)).build_mapping(pl.Series(values), "identifier", "id")
        assert len(set(mapping.values())) == len(values)
        assert all(len(fake) == 4 and fake.isdigit() for fake in mapping.values())

    def test_dates_stay_distinct_beyond_the_random_range(self):
        # More distinct dates than the ~26k-day range the generator draws from.
        values = [(date(1900, 1, 1) + timedelta(days=i)).isoformat() for i in range(30000)]
        mapping = PIIGenerator(PIIConfig(seed=1)).build_mapping(pl.Series(values), "date", "dob")
        assert len(set(mapping.values())) == len(values)

    def test_date_format_is_preserved(self):
        values = [f"{(i % 12) + 1:02d}/{(i % 27) + 1:02d}/{1950 + i % 40}" for i in range(500)]
        mapping = PIIGenerator(PIIConfig(seed=1)).build_mapping(pl.Series(values), "date", "dob")
        assert all(len(fake) == 10 and fake[2] == '/' and fake[5] == '/' for fake in mapping.values())

    def test_same_seed_gives_same_mapping(self):
        values = [f"Person {i}" for i in range(2000)]
        first = PIIGenerator(PIIConfig(seed=5)).build_mapping(pl.Series(values), "name", "name")
        second = PIIGenerator(PIIConfig(seed=5)).build_mapping(pl.Series(values), "name", "name")
        assert first == second

    def test_columns_do_not_share_fakes_when_there_is_room(self):
        generator = PIIGenerator(PIIConfig(seed=1))
        values = [f"Person {i}" for i in range(3000)]
        first = generator.build_mapping(pl.Series(values), "name", "patient_name")
        second = generator.build_mapping(pl.Series(values), "name", "member_name")
        assert not set(first.values()) & set(second.values())

    def test_small_format_space_only_needs_per_column_uniqueness(self):
        # Two 4-digit columns of 6000 values exceed the 10,000 codes available
        # run-wide, but each column alone fits and must still be unique.
        generator = PIIGenerator(PIIConfig(seed=1))
        values = [f"{i:04d}" for i in range(6000)]
        first = generator.build_mapping(pl.Series(values), "identifier", "a")
        second = generator.build_mapping(pl.Series(values), "identifier", "b")
        assert len(set(first.values())) == 6000
        assert len(set(second.values())) == 6000


class TestRoundTrip:
    def test_large_frame_restores_exactly(self):
        n = 6000
        df = pl.DataFrame({
            'name': [f"Person {i}" for i in range(n)],
            'email': [f"u{i}@corp.com" for i in range(n)],
            'card_number': [f"{i:05d}" for i in range(n)],
            'dob': [(date(1950, 1, 1) + timedelta(days=i % 20000)).isoformat() for i in range(n)],
        })
        processed, mapper = process_dataframe(df, PIIConfig(mode="fake", seed=1))

        for column in df.columns:
            assert processed[column].n_unique() == df[column].n_unique(), column
        assert restore_dataframe(processed, mapper).equals(df)

    def test_restore_is_exact_match_not_substring(self):
        # Fixed-width ids often yield fakes where one is a prefix of another.
        mapper = PIIMapper(PIIConfig())
        mapper.add_column_mapping("id", "identifier", {"AA": "123", "BB": "1234", "CC": "5555"})
        restored = PIIRestorer(mapper).restore_dataframe(pl.DataFrame({"id": ["1234", "123", "5555", None]}))
        assert restored["id"].to_list() == ["BB", "AA", "CC", None]

    def test_restore_series_is_exact_match(self):
        mapper = PIIMapper(PIIConfig())
        mapper.add_column_mapping("id", "identifier", {"AA": "123", "BB": "1234"})
        restored = PIIRestorer(mapper).restore_series(pl.Series(["1234", "123"]), "id")
        assert restored.to_list() == ["BB", "AA"]

    def test_token_redaction_round_trips(self):
        n = 300
        df = pl.DataFrame({'name': [f"Person {i}" for i in range(n)]})
        config = PIIConfig(mode=PIIMode.REDACT, redact_style=RedactionStyle.TOKEN)
        processed, mapper = process_dataframe(df, config)
        assert processed['name'].n_unique() == n
        assert restore_dataframe(processed, mapper).equals(df)

    def test_tokens_are_unique_across_columns_in_one_run(self):
        df = pl.DataFrame({'name': ['A', 'B'], 'email': ['a@x.com', 'b@x.com']})
        config = PIIConfig(mode=PIIMode.REDACT, redact_style=RedactionStyle.TOKEN)
        processed, _ = process_dataframe(df, config)
        tokens = processed['name'].to_list() + processed['email'].to_list()
        assert len(set(tokens)) == 4

    def test_redactor_tokens_are_deterministic(self):
        series = pl.Series(["b", "a", "c"])
        config = PIIConfig(mode=PIIMode.REDACT, redact_style=RedactionStyle.TOKEN)
        mapping = PIIRedactor(config).build_mapping(series)
        assert mapping == {"a": "REDACTED_0001", "b": "REDACTED_0002", "c": "REDACTED_0003"}


class TestSharedMappingAcrossFrames:
    """A workbook's sheets go through one processor and one mapping file."""

    def test_same_column_in_two_sheets_shares_one_mapping(self):
        sheet1 = pl.DataFrame({'email': ['a@x.com', 'b@x.com']})
        sheet2 = pl.DataFrame({'email': ['b@x.com', 'c@x.com']})

        processor = PIIProcessor(PIIConfig(seed=3))
        fake1 = processor.process(sheet1)
        fake2 = processor.process(sheet2)

        # The shared value gets the same fake in both sheets...
        assert fake1['email'][1] == fake2['email'][0]
        # ...and a single mapping restores each sheet.
        restorer = PIIRestorer(processor.mapper)
        assert restorer.restore_dataframe(fake1).equals(sheet1)
        assert restorer.restore_dataframe(fake2).equals(sheet2)


class TestMappingFiles:
    @staticmethod
    def _mapper(mapping, mode="fake", style="full"):
        mapper = PIIMapper(PIIConfig(mode=mode, redact_style=style))
        mapper.add_column_mapping("name", "name", mapping)
        return mapper

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.parametrize("name", ["m.json", "m.db", "m.sqlite"])
    def test_files_are_owner_only(self, tmp_path, name):
        path = tmp_path / name
        self._mapper({"a": "b"}).save(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_existing_world_readable_file_is_tightened(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text("{}")
        os.chmod(path, 0o644)
        self._mapper({"a": "b"}).save(str(path))
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    def test_sqlite_save_replaces_instead_of_merging(self, tmp_path):
        path = str(tmp_path / "m.db")
        self._mapper({"old": "x"}).save(path)
        self._mapper({"new": "y"}).save(path)
        assert PIIMapper.load(path).store.get_mapping("name") == {"new": "y"}

    @pytest.mark.parametrize("name", ["m.json", "m.db"])
    def test_mapping_survives_save_and_load(self, tmp_path, name):
        path = str(tmp_path / name)
        self._mapper({"a": "b", "c": "d"}).save(path)
        assert PIIMapper.load(path).store.get_mapping("name") == {"a": "b", "c": "d"}

    def test_ambiguous_mapping_is_refused_and_nothing_is_written(self, tmp_path):
        path = tmp_path / "m.json"
        with pytest.raises(AmbiguousMappingError):
            self._mapper({"a": "Z", "b": "Z"}).save(str(path))
        assert not path.exists()

    def test_ambiguous_token_mapping_is_refused(self, tmp_path):
        mapper = self._mapper({"a": "T", "b": "T"}, mode="redact", style="token")
        with pytest.raises(AmbiguousMappingError):
            mapper.save(str(tmp_path / "m.json"))

    @pytest.mark.parametrize("style", ["full", "partial"])
    def test_lossy_redaction_may_collapse_values(self, tmp_path, style):
        # Many-to-one is the point of full/partial redaction; it must still save.
        mapper = self._mapper({"a": "[REDACTED]", "b": "[REDACTED]"}, mode="redact", style=style)
        mapper.save(str(tmp_path / "m.json"))


class TestIdentifiers:
    def fake(self, original, seed=1):
        return PIIGenerator(PIIConfig(seed=seed)).generate_fake_value(original, "identifier", "member_code")

    def test_identifiers_without_digits_are_faked(self):
        assert self.fake("ABCDEF") != "ABCDEF"

    def test_letters_are_randomised_along_with_digits(self):
        # "M" used to survive: initials and office codes identify people too.
        assert any(self.fake("M-001", seed=seed)[0] != "M" for seed in range(20))

    @pytest.mark.parametrize("original", ["ABCDEF", "M-001", "ab-Cd 12", "JKT/2024/0042", "x"])
    def test_shape_case_and_punctuation_are_kept(self, original):
        fake = self.fake(original)
        assert len(fake) == len(original)
        for before, after in zip(original, fake):
            assert before.isdigit() == after.isdigit()
            assert before.isupper() == after.isupper()
            assert before.isalpha() == after.isalpha()
            if not before.isalnum():
                assert before == after

    def test_a_full_letter_space_still_maps_one_to_one(self):
        # All 676 two-letter codes need 676 distinct two-letter fakes.
        codes = ["".join(pair) for pair in itertools.product(string.ascii_uppercase, repeat=2)]
        mapping = PIIGenerator(PIIConfig(seed=1)).build_mapping(pl.Series(codes), "identifier", "code")
        assert len(set(mapping.values())) == len(codes)
        assert all(len(fake) == 2 and fake.isalpha() and fake.isupper() for fake in mapping.values())

    def test_letter_identifiers_round_trip(self):
        df = pl.DataFrame({'member_code': ['ABCDEF', 'M-001', 'XY', 'ab-12'] * 50 + [f"K{i:03d}" for i in range(300)]})
        processed, mapper = process_dataframe(df, PIIConfig(seed=1))
        assert processed['member_code'].n_unique() == df['member_code'].n_unique()
        assert restore_dataframe(processed, mapper).equals(df)


class TestCrossColumnConsistency:
    """The same person must look the same wherever they appear."""

    def test_same_value_in_two_columns_of_one_category_gets_one_fake(self):
        df = pl.DataFrame({
            'patient_name': ['John Smith', 'Ann Lee', 'Bob Ray'],
            'member_name': ['John Smith', 'Ann Lee', 'Zed Poe'],
        })
        processed, mapper = process_dataframe(df, PIIConfig(seed=1))

        assert processed['patient_name'][0] == processed['member_name'][0]
        assert processed['patient_name'][1] == processed['member_name'][1]
        distinct = set(processed['patient_name'].to_list() + processed['member_name'].to_list())
        assert len(distinct) == 4                                     # four people, four fakes
        assert restore_dataframe(processed, mapper).equals(df)

    def test_flat_nested_and_json_values_share_fakes(self):
        df = pl.DataFrame({
            'email': ['j@x.com', 'a@x.com'],
            'contacts': [[{'email': 'j@x.com'}], [{'email': 'z@x.com'}]],
            'payload': ['{"email": "j@x.com"}', '{"email": "a@x.com"}'],
        })
        processor = PIIProcessor(PIIConfig(seed=1, json_columns=['payload']))
        processed = processor.process(df)

        flat = processed['email'][0]
        assert processed['contacts'][0][0]['email'] == flat
        assert json.loads(processed['payload'][0])['email'] == flat
        assert json.loads(processed['payload'][1])['email'] == processed['email'][1]
        assert processed['contacts'][1][0]['email'] not in processed['email'].to_list()

        restored = PIIRestorer(processor.mapper).restore_dataframe(processed)
        assert restored['contacts'].to_list() == df['contacts'].to_list()
        assert [json.loads(d) for d in restored['payload']] == [json.loads(d) for d in df['payload']]

    def test_categories_do_not_share(self):
        # "Jordan" as a person and as a street are different kinds of fake.
        df = pl.DataFrame({'patient_name': ['Jordan'], 'address': ['Jordan']})
        processed, mapper = process_dataframe(df, PIIConfig(seed=1))
        assert processed['patient_name'][0] != processed['address'][0]
        assert restore_dataframe(processed, mapper).equals(df)

    def test_distinct_originals_never_share_a_fake_across_columns(self):
        names = [f"Person {i}" for i in range(4000)]
        padding = [None] * 1000
        df = pl.DataFrame({
            'patient_name': names[:3000] + padding,          # 1000 people only here
            'member_name': names[1000:] + padding,           # 1000 people only here, 2000 in both
        })
        processed, mapper = process_dataframe(df, PIIConfig(seed=1))

        owner = {}                                           # fake -> the one original that owns it
        for column in ('patient_name', 'member_name'):
            for original, fake in zip(df[column], processed[column]):
                if original is not None:
                    assert owner.setdefault(fake, original) == original
        assert len(owner) == 4000
        assert restore_dataframe(processed, mapper).equals(df)

    def test_tokens_are_consistent_across_columns(self):
        df = pl.DataFrame({'patient_name': ['John Smith', 'Ann Lee'], 'member_name': ['John Smith', 'Zed Poe']})
        config = PIIConfig(mode=PIIMode.REDACT, redact_style=RedactionStyle.TOKEN)
        processed, mapper = process_dataframe(df, config)

        assert processed['patient_name'][0] == processed['member_name'][0]
        tokens = set(processed['patient_name'].to_list() + processed['member_name'].to_list())
        assert len(tokens) == 3
        assert restore_dataframe(processed, mapper).equals(df)

    def test_free_text_restore_is_unambiguous(self):
        df = pl.DataFrame({'patient_name': ['John Smith', 'Ann Lee'], 'member_name': ['John Smith', 'Zed Poe']})
        processed, mapper = process_dataframe(df, PIIConfig(seed=1))
        fake = processed['patient_name'][0]
        assert PIIRestorer(mapper).restore_text(f"Please review {fake}.") == "Please review John Smith."

    def test_mapping_is_still_saved_per_column(self):
        df = pl.DataFrame({'patient_name': ['John Smith'], 'member_name': ['John Smith']})
        _, mapper = process_dataframe(df, PIIConfig(seed=1))
        assert set(mapper.store.get_all_mappings()) == {'patient_name', 'member_name'}


class TestCleanup:
    @staticmethod
    def make(directory, name, days_old):
        path = directory / name
        path.write_text("{}")
        old = time.time() - days_old * 86400
        os.utime(path, (old, old))
        return path

    def test_deletes_old_mapping_files_of_every_supported_name_and_type(self, tmp_path):
        doomed = [self.make(tmp_path, name, 10) for name in (
            "mapping.json", "mapping_patients.json", "mapping_x.db", "mapping.sqlite", "mappings_2024.json",
        )]
        deleted = PIIMapper(PIIConfig()).cleanup_old_mappings(str(tmp_path), 7)
        assert sorted(deleted) == sorted(doomed)
        assert not any(path.exists() for path in doomed)

    def test_never_touches_unrelated_files(self, tmp_path):
        keep = [self.make(tmp_path, name, 10) for name in (
            "mapping.py", "mapping_notes.txt", "mapping", "other.json", "my_mapping.json", "README.md",
        )]
        (tmp_path / "mapping_dir.json").mkdir()
        PIIMapper(PIIConfig()).cleanup_old_mappings(str(tmp_path), 7)
        assert all(path.exists() for path in keep)
        assert (tmp_path / "mapping_dir.json").is_dir()

    def test_keeps_recent_mapping_files(self, tmp_path):
        recent = self.make(tmp_path, "mapping.json", 1)
        assert PIIMapper(PIIConfig()).cleanup_old_mappings(str(tmp_path), 7) == []
        assert recent.exists()

    def test_fractional_days_and_missing_directory(self, tmp_path):
        two_hours = self.make(tmp_path, "mapping.json", 2 / 24)
        assert PIIMapper(PIIConfig()).cleanup_old_mappings(str(tmp_path), 1 / 24) == [two_hours]
        assert PIIMapper(PIIConfig()).cleanup_old_mappings(str(tmp_path / "nope"), 1) == []
