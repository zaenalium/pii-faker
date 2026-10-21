"""Tests for nested struct/list columns and JSON documents stored in text cells."""

import json

import polars as pl
import pytest

from pii_faker.core import PIIConfig, PIIProcessor, process_dataframe, restore_dataframe
from pii_faker.core.detector import PIIColumnDetector
from pii_faker.core.json_walker import JsonWalker
from pii_faker.core.restorer import PIIRestorer


@pytest.fixture
def patients():
    """Struct, list-of-struct and list-of-string fields, with nulls and empty lists."""
    return pl.DataFrame([
        {"id": 1, "patient": {"patient_name": "John Smith", "tags": ["x", "y"],
                              "contacts": [{"email": "a@b.com"}, {"email": "c@d.com"}]}},
        {"id": 2, "patient": {"patient_name": "Jane Doe", "tags": None, "contacts": []}},
        {"id": 3, "patient": None},
        {"id": 4, "patient": {"patient_name": None, "tags": ["z"], "contacts": None}},
    ])


def columns(config, df):
    return [(e['column'], e['category']) for e in PIIColumnDetector(config).detect_all(df)]


class TestNestedDetection:
    def test_bare_names_match_at_any_depth(self, patients):
        assert columns(PIIConfig(), patients) == [
            ('patient.patient_name', 'name'),
            ('patient.contacts[*].email', 'contact'),
        ]

    def test_explicit_paths(self, patients):
        config = PIIConfig(extra_pii_columns=['patient.tags'], override_defaults=True)
        assert columns(config, patients) == [('patient.tags', 'custom')]

    def test_explicit_path_keeps_the_builtin_category_of_its_leaf(self, patients):
        config = PIIConfig(extra_pii_columns=['patient.patient_name', 'patient.contacts[*].email'])
        assert columns(config, patients) == [
            ('patient.patient_name', 'name'),
            ('patient.contacts[*].email', 'contact'),
        ]

    def test_list_marker_is_optional_in_explicit_paths(self, patients):
        config = PIIConfig(extra_pii_columns=['patient.contacts.email'], override_defaults=True)
        assert columns(config, patients) == [('patient.contacts[*].email', 'custom')]

    def test_strict_paths_ignore_bare_names_below_the_top_level(self, patients):
        assert columns(PIIConfig(strict_paths=True), patients) == []

    def test_strict_paths_still_honour_explicit_paths(self, patients):
        config = PIIConfig(strict_paths=True, extra_pii_columns=['patient.contacts[*].email'])
        assert columns(config, patients) == [('patient.contacts[*].email', 'contact')]

    def test_matching_container_covers_everything_inside_it(self):
        df = pl.DataFrame({'address': [{'street': '1 Main St', 'city': 'Springfield'}], 'id': [1]})
        assert columns(PIIConfig(), df) == [('address.street', 'address'), ('address.city', 'address')]

    def test_legacy_detect_still_reports_only_top_level_columns(self, patients):
        assert PIIColumnDetector(PIIConfig()).detect(patients) == []

    def test_flat_columns_are_detected_as_before(self, sample_df, config):
        detector = PIIColumnDetector(config)
        assert [c['column'] for c in detector.detect_all(sample_df)] == [
            c['column'] for c in detector.detect(sample_df)
        ]


class TestJsonWalkerHelpers:
    def test_leaf_values(self, patients):
        assert sorted(JsonWalker.leaf_values(patients, 'patient.patient_name')) == ['Jane Doe', 'John Smith']
        assert sorted(JsonWalker.leaf_values(patients, 'patient.contacts[*].email')) == ['a@b.com', 'c@d.com']
        assert sorted(JsonWalker.leaf_values(patients, 'patient.tags')) == ['x', 'y', 'z']

    @pytest.mark.parametrize("path", ['id', 'patient.missing', 'nope.x', 'patient.patient_name.deeper'])
    def test_paths_without_text_return_none(self, patients, path):
        assert JsonWalker.leaf_values(patients, path) is None
        assert JsonWalker.rewrite_expr(patients.schema, path, {}) is None

    def test_rewrite_expr_changes_only_the_target(self, patients):
        expr = JsonWalker.rewrite_expr(patients.schema, 'patient.contacts[*].email', {'a@b.com': 'X'})
        rewritten = patients.with_columns(expr)
        assert rewritten.schema == patients.schema
        assert rewritten['patient'][0]['contacts'] == [{'email': 'X'}, {'email': 'c@d.com'}]
        assert rewritten['patient'][0]['patient_name'] == 'John Smith'
        assert rewritten['patient'][2] is None

    def test_column_names_containing_dots_are_flat_columns(self):
        df = pl.DataFrame({'patient.name': ['Ann']})
        assert JsonWalker.leaf_values(df, 'patient.name').to_list() == ['Ann']
        assert df.with_columns(JsonWalker.rewrite_expr(df.schema, 'patient.name', {'Ann': 'Bo'}))['patient.name'].to_list() == ['Bo']


class TestNestedRoundTrip:
    def test_struct_and_list_columns_restore_exactly(self, patients):
        processed, mapper = process_dataframe(patients, PIIConfig(seed=1))

        assert processed['patient'][0]['patient_name'] != 'John Smith'
        assert processed['patient'][0]['contacts'][0]['email'] != 'a@b.com'
        assert processed['patient'][0]['tags'] == ['x', 'y']      # tags weren't PII
        assert processed['id'].to_list() == patients['id'].to_list()

        assert restore_dataframe(processed, mapper).equals(patients)

    def test_nulls_and_empty_lists_survive(self, patients):
        processed, _ = process_dataframe(patients, PIIConfig(seed=1))
        assert processed['patient'][1]['contacts'] == []
        assert processed['patient'][2] is None
        assert processed['patient'][3]['patient_name'] is None

    def test_list_of_strings_at_the_leaf(self, patients):
        config = PIIConfig(seed=1, extra_pii_columns=['patient.tags'])
        processed, mapper = process_dataframe(patients, config)
        assert processed['patient'][0]['tags'] != ['x', 'y']
        assert restore_dataframe(processed, mapper).equals(patients)

    def test_mapping_is_keyed_by_path(self, patients):
        _, mapper = process_dataframe(patients, PIIConfig(seed=1))
        assert set(mapper.store.get_all_mappings()) == {'patient.patient_name', 'patient.contacts[*].email'}

    def test_repeated_values_share_one_fake(self):
        df = pl.DataFrame({'p': [{'email': 'a@b.com'}, {'email': 'a@b.com'}, {'email': 'c@d.com'}]})
        processed, _ = process_dataframe(df, PIIConfig(seed=1))
        emails = [row['email'] for row in processed['p']]
        assert emails[0] == emails[1] != emails[2]

    def test_redaction_of_nested_values(self, patients):
        config = PIIConfig(mode="redact", redact_style="token")
        processed, mapper = process_dataframe(patients, config)
        assert processed['patient'][0]['patient_name'].startswith('REDACTED_')
        assert restore_dataframe(processed, mapper).equals(patients)

    def test_ndjson_file_round_trip(self, tmp_path, sample_ndjson):
        df = pl.read_ndjson(sample_ndjson)
        processed, mapper = process_dataframe(df, PIIConfig(seed=1))
        path = tmp_path / "out.ndjson"
        processed.write_ndjson(str(path))
        assert restore_dataframe(pl.read_ndjson(path), mapper).equals(df)

    def test_restore_ndjson_alias(self, patients):
        processed, mapper = process_dataframe(patients, PIIConfig(seed=1))
        assert PIIRestorer(mapper).restore_ndjson(processed).equals(patients)


class TestUnprocessableMatches:
    def test_numeric_pii_column_is_reported_not_silently_skipped(self):
        df = pl.DataFrame({'phone': [5551234, 5559876], 'name': ['Al Bo', 'Cy Di']})
        processor = PIIProcessor(PIIConfig(seed=1))
        processed = processor.process(df)
        assert processor.skipped == ['phone']
        assert processed['phone'].to_list() == [5551234, 5559876]
        assert processed['name'].to_list() != ['Al Bo', 'Cy Di']

    def test_mistyped_explicit_path_is_not_reported_because_it_never_matches(self, patients):
        processor = PIIProcessor(PIIConfig(extra_pii_columns=['patient.nope'], override_defaults=True))
        processor.process(patients)
        assert processor.skipped == []


@pytest.fixture
def payloads():
    return pl.DataFrame({
        'id': [1, 2, 3, 4, 5],
        'payload': [
            '{"patient_name": "John Smith", "contact": {"phone": "555-0101"}, "n": 1}',
            '{"patient_name": "Jane Doe", "contacts": [{"email": "a@b.com"}, {"email": "c@d.com"}]}',
            'not json',
            None,
            '{"note": "nothing sensitive"}',
        ],
    })


class TestJsonStringColumns:
    def config(self, **kwargs):
        return PIIConfig(seed=1, json_columns=['payload'], **kwargs)

    def test_detection_lists_every_path_inside_the_documents(self, payloads):
        found = [e['column'] for e in PIIColumnDetector(self.config()).detect_all(payloads)]
        assert found == [
            'payload:contact.phone',
            'payload:contacts[*].email',
            'payload:patient_name',
        ]

    def test_documents_are_faked_and_other_content_kept(self, payloads):
        processed, _ = process_dataframe(payloads, self.config())
        first = json.loads(processed['payload'][0])
        assert first['patient_name'] != 'John Smith'
        assert first['contact']['phone'] != '555-0101'
        assert first['n'] == 1
        assert processed['payload'][2] == 'not json'
        assert processed['payload'][3] is None
        assert processed['payload'][4] == payloads['payload'][4]       # unchanged byte for byte

    def test_documents_restore_to_the_same_content(self, payloads):
        processed, mapper = process_dataframe(payloads, self.config())
        restored = restore_dataframe(processed, mapper)

        for before, after in zip(payloads['payload'], restored['payload']):
            if before is None or before == 'not json':
                assert after == before
            else:
                assert json.loads(after) == json.loads(before)

    def test_flat_columns_are_still_processed_alongside(self):
        df = pl.DataFrame({'email': ['a@b.com'], 'payload': ['{"phone": "555-0101"}']})
        processed, mapper = process_dataframe(df, self.config())
        assert processed['email'][0] != 'a@b.com'
        assert json.loads(processed['payload'][0])['phone'] != '555-0101'
        assert restore_dataframe(processed, mapper)['email'][0] == 'a@b.com'

    def test_strict_paths_use_the_document_root_as_top_level(self):
        df = pl.DataFrame({'payload': ['{"email": "a@b.com", "inner": {"email": "c@d.com"}}']})
        config = PIIConfig(json_columns=['payload'], strict_paths=True)
        found = [e['column'] for e in PIIColumnDetector(config).detect_all(df)]
        assert found == ['payload:email']

    def test_unique_count_matches_distinct_values(self, payloads):
        processor = PIIProcessor(self.config())
        counts = {e['column']: processor.unique_count(payloads, e) for e in processor.detect(payloads)}
        assert counts == {
            'payload:contact.phone': 1,
            'payload:contacts[*].email': 2,
            'payload:patient_name': 2,
        }
