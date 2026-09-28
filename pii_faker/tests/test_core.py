"""Tests for core PII Faker functionality."""

import pytest
import polars as pl

from pii_faker.core.config import PIIConfig, PIIMode, MatchStrategy, RedactionStyle
from pii_faker.core.detector import PIIColumnDetector, detect_pii_columns
from pii_faker.core.generators import PIIGenerator
from pii_faker.core.redactor import PIIRedactor
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.restorer import PIIRestorer
from pii_faker.core.file_io import FileHandler
from pii_faker.core.json_walker import JsonWalker


class TestPIIConfig:
    """Test PII configuration."""

    def test_default_config(self):
        config = PIIConfig()
        assert config.mode == PIIMode.FAKE
        assert config.match_strategy == MatchStrategy.NORMALIZED
        assert config.redact_style == RedactionStyle.FULL
        assert config.seed is None

    def test_custom_config(self):
        config = PIIConfig(
            mode=PIIMode.REDACT,
            match_strategy=MatchStrategy.FUZZY,
            seed=42,
        )
        assert config.mode == PIIMode.REDACT
        assert config.match_strategy == MatchStrategy.FUZZY
        assert config.seed == 42


class TestPIIColumnDetector:
    """Test PII column detection."""

    def test_detect_default_columns(self, sample_df, config):
        detector = PIIColumnDetector(config)
        pii_cols = detector.detect(sample_df)

        detected_names = [c['column'] for c in pii_cols]
        assert 'patient_name' in detected_names
        assert 'email' in detected_names
        assert 'phone' in detected_names
        assert 'card_number' in detected_names
        assert 'non_pii_column' not in detected_names

    def test_detect_normalized_matching(self, config):
        """Test that normalized matching catches case/separator variations."""
        df = pl.DataFrame({
            'Patient Name': ['John'],
            'PATIENT-NAME': ['Jane'],
            'member_code': ['M1'],
        })

        detector = PIIColumnDetector(config)
        pii_cols = detector.detect(df)

        detected_names = [c['column'] for c in pii_cols]
        assert 'Patient Name' in detected_names
        assert 'PATIENT-NAME' in detected_names
        assert 'member_code' in detected_names

    def test_detect_exact_matching(self):
        config = PIIConfig(match_strategy=MatchStrategy.EXACT)
        df = pl.DataFrame({
            'name': ['John'],
            'Name': ['Jane'],
            'NAME': ['Bob'],
        })

        detector = PIIColumnDetector(config)
        pii_cols = detector.detect(df)

        detected_names = [c['column'] for c in pii_cols]
        assert 'name' in detected_names
        assert 'Name' not in detected_names
        assert 'NAME' not in detected_names

    def test_detect_fuzzy_matching(self):
        config = PIIConfig(match_strategy=MatchStrategy.FUZZY)
        df = pl.DataFrame({
            'patients_full_name_2024': ['John'],
            'phone_number': ['555-0100'],
        })

        detector = PIIColumnDetector(config)
        pii_cols = detector.detect(df)

        detected_names = [c['column'] for c in pii_cols]
        assert len(detected_names) > 0

    def test_detect_extra_columns(self):
        config = PIIConfig(extra_pii_columns=['ssn', 'dob'])
        df = pl.DataFrame({
            'ssn': ['123-45-6789'],
            'dob': ['1990-01-01'],
            'name': ['John'],
        })

        detector = PIIColumnDetector(config)
        pii_cols = detector.detect(df)

        detected_names = [c['column'] for c in pii_cols]
        assert 'ssn' in detected_names
        assert 'dob' in detected_names
        assert 'name' in detected_names

    def test_detect_override_defaults(self):
        config = PIIConfig(
            extra_pii_columns=['ssn'],
            override_defaults=True,
        )
        df = pl.DataFrame({
            'ssn': ['123-45-6789'],
            'name': ['John'],
        })

        detector = PIIColumnDetector(config)
        pii_cols = detector.detect(df)

        detected_names = [c['column'] for c in pii_cols]
        assert 'ssn' in detected_names
        assert 'name' not in detected_names

    def test_convenience_function(self, sample_df, config):
        pii_cols = detect_pii_columns(sample_df, config)
        assert len(pii_cols) > 0


class TestPIIGenerator:
    """Test fake data generation."""

    def test_generate_name(self, config):
        generator = PIIGenerator(config)
        fake = generator.generate_fake_value("John Doe", "name", "patient_name")
        assert isinstance(fake, str)
        assert len(fake) > 0
        assert fake != "John Doe"

    def test_generate_identifier(self, config):
        generator = PIIGenerator(config)
        fake = generator.generate_fake_value("4111-1111-1111-1111", "identifier", "card_number")
        assert len(fake) == len("4111-1111-1111-1111")
        assert fake != "4111-1111-1111-1111"

    def test_generate_contact_email(self, config):
        generator = PIIGenerator(config)
        fake = generator.generate_fake_value("john@example.com", "contact", "email")
        assert '@' in fake

    def test_generate_contact_phone(self, config):
        generator = PIIGenerator(config)
        fake = generator.generate_fake_value("555-0100", "contact", "phone")
        assert len(fake) == len("555-0100")

    def test_build_mapping(self, config, sample_df):
        generator = PIIGenerator(config)
        mapping = generator.build_mapping(sample_df['patient_name'], 'name', 'patient_name')

        assert len(mapping) == 3
        assert 'John Doe' in mapping
        assert 'Jane Smith' in mapping
        assert 'Bob Johnson' in mapping

    def test_process_column(self, config, sample_df):
        generator = PIIGenerator(config)
        col_info = {'column': 'patient_name', 'category': 'name'}

        df, mapping = generator.process_column(sample_df, col_info)

        assert len(mapping) > 0
        # Check that values were replaced
        for original, fake in mapping.items():
            assert original != fake

    def test_consistency(self, config, sample_df):
        """Test that same input produces same output with same seed."""
        generator1 = PIIGenerator(PIIConfig(seed=42))
        generator2 = PIIGenerator(PIIConfig(seed=42))

        mapping1 = generator1.build_mapping(sample_df['patient_name'], 'name', 'patient_name')
        mapping2 = generator2.build_mapping(sample_df['patient_name'], 'name', 'patient_name')

        # Same set of originals and same set of fakes (order may differ due to unique())
        assert set(mapping1.keys()) == set(mapping2.keys())
        assert set(mapping1.values()) == set(mapping2.values())
        # Each original maps to the same fake in both runs
        for orig in mapping1:
            assert mapping1[orig] == mapping2[orig]

    def test_different_seeds(self, sample_df):
        """Test that different seeds produce different outputs."""
        gen1 = PIIGenerator(PIIConfig(seed=42))
        gen2 = PIIGenerator(PIIConfig(seed=123))

        mapping1 = gen1.build_mapping(sample_df['patient_name'], 'name', 'patient_name')
        mapping2 = gen2.build_mapping(sample_df['patient_name'], 'name', 'patient_name')

        assert mapping1 != mapping2


class TestPIIRedactor:
    """Test redaction functionality."""

    def test_redact_full(self, redact_config):
        redactor = PIIRedactor(redact_config)
        redacted = redactor._redact_full("John Doe")
        assert redacted == "[REDACTED]"

    def test_redact_partial(self):
        config = PIIConfig(mode=PIIMode.REDACT, redact_style=RedactionStyle.PARTIAL)
        redactor = PIIRedactor(config)
        redacted = redactor._redact_partial("4111-1111-1111-1111")
        assert redacted.endswith("1111")
        assert redacted.startswith("X")

    def test_redact_token(self):
        config = PIIConfig(mode=PIIMode.REDACT, redact_style=RedactionStyle.TOKEN)
        redactor = PIIRedactor(config)
        redacted1 = redactor._redact_token("John Doe")
        redacted2 = redactor._redact_token("Jane Smith")
        assert redacted1 == "REDACTED_0001"
        assert redacted2 == "REDACTED_0002"

    def test_build_mapping(self, redact_config, sample_df):
        redactor = PIIRedactor(redact_config)
        mapping = redactor.build_mapping(sample_df['patient_name'], 'patient_name')

        assert len(mapping) == 3
        for original, redacted in mapping.items():
            assert redacted == "[REDACTED]"

    def test_process_column(self, redact_config, sample_df):
        redactor = PIIRedactor(redact_config)
        col_info = {'column': 'patient_name', 'category': 'name'}

        df, mapping = redactor.process_column(sample_df, col_info)

        assert len(mapping) > 0
        for val in df['patient_name'].to_list():
            assert val == "[REDACTED]"


class TestPIIMapper:
    """Test mapping management."""

    def test_add_mapping(self, config):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1'})

        mapping = mapper.store.get_mapping('name')
        assert mapping == {'John': 'Fake1'}

    def test_merge(self, config):
        mapper1 = PIIMapper(config)
        mapper1.add_column_mapping('name', 'name', {'John': 'Fake1'})

        mapper2 = PIIMapper(config)
        mapper2.add_column_mapping('email', 'contact', {'john@test.com': 'fake@test.com'})

        mapper1.merge(mapper2)

        assert mapper1.store.get_mapping('name') == {'John': 'Fake1'}
        assert mapper1.store.get_mapping('email') == {'john@test.com': 'fake@test.com'}

    def test_get_original_value(self, config):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1'})

        assert mapper.get_original_value('Fake1') == 'John'
        assert mapper.get_original_value('Unknown') is None

    def test_save_load_json(self, config, tmp_path):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1', 'Jane': 'Fake2'})

        path = tmp_path / "mapping.json"
        mapper.save(str(path))

        loaded = PIIMapper.load(str(path))
        assert loaded.store.get_mapping('name') == {'John': 'Fake1', 'Jane': 'Fake2'}

    def test_save_load_sqlite(self, config, tmp_path):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1', 'Jane': 'Fake2'})

        path = tmp_path / "mapping.db"
        mapper.save(str(path))

        loaded = PIIMapper.load(str(path))
        assert loaded.store.get_mapping('name') == {'John': 'Fake1', 'Jane': 'Fake2'}


class TestPIIRestorer:
    """Test restoration functionality."""

    def test_restore_value(self, config):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1'})

        restorer = PIIRestorer(mapper)
        assert restorer.restore_value('Fake1') == 'John'
        assert restorer.restore_value('Unknown') == 'Unknown'

    def test_restore_series(self, config):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1', 'Jane': 'Fake2'})

        restorer = PIIRestorer(mapper)
        series = pl.Series(['Fake1', 'Fake2', 'Fake1'])
        restored = restorer.restore_series(series, 'name')

        assert restored.to_list() == ['John', 'Jane', 'John']

    def test_restore_dataframe(self, config, sample_df):
        # Process with generator
        generator = PIIGenerator(PIIConfig(seed=42))
        col_info = {'column': 'patient_name', 'category': 'name'}
        processed_df, mapping = generator.process_column(sample_df, col_info)

        # Create mapper
        mapper = PIIMapper(config)
        mapper.add_column_mapping('patient_name', 'name', mapping)

        # Restore
        restorer = PIIRestorer(mapper)
        restored_df = restorer.restore_dataframe(processed_df)

        # Check restoration
        assert restored_df['patient_name'].to_list() == sample_df['patient_name'].to_list()

    def test_restore_text(self, config):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {
            'John Doe': 'Michael Smith',
            'Jane Smith': 'Sarah Johnson',
        })

        restorer = PIIRestorer(mapper)
        text = "The patient Michael Smith was seen with Sarah Johnson."
        restored = restorer.restore_text(text)

        assert restored == "The patient John Doe was seen with Jane Smith."

    def test_restore_json(self, config):
        mapper = PIIMapper(config)
        mapper.add_column_mapping('name', 'name', {'John': 'Fake1'})

        restorer = PIIRestorer(mapper)
        data = {'patient': {'name': 'Fake1'}, 'items': [{'name': 'Fake1'}]}
        restored = restorer.restore_json(data)

        assert restored['patient']['name'] == 'John'
        assert restored['items'][0]['name'] == 'John'


class TestFileHandler:
    """Test file I/O."""

    def test_read_csv(self, config, sample_csv):
        handler = FileHandler(config)
        df = handler.read(str(sample_csv))

        assert isinstance(df, pl.DataFrame)
        assert 'patient_name' in df.columns
        assert len(df) == 3

    def test_write_csv(self, config, sample_df, tmp_path):
        handler = FileHandler(config)
        output_path = tmp_path / "output.csv"
        handler.write(sample_df, str(output_path))

        assert output_path.exists()
        df = pl.read_csv(str(output_path))
        assert len(df) == 3

    def test_read_excel(self, config, sample_excel):
        handler = FileHandler(config)
        df = handler.read(str(sample_excel))

        assert isinstance(df, pl.DataFrame)
        assert 'patient_name' in df.columns

    def test_write_excel(self, config, sample_df, tmp_path):
        handler = FileHandler(config)
        output_path = tmp_path / "output.xlsx"
        handler.write(sample_df, str(output_path))

        assert output_path.exists()

    def test_detect_format(self, config):
        handler = FileHandler(config)

        assert handler.detect_format("test.csv") == "csv"
        assert handler.detect_format("test.xlsx") == "excel"
        assert handler.detect_format("test.json") == "json"
        assert handler.detect_format("test.ndjson") == "ndjson"
        assert handler.detect_format("test.parquet") == "parquet"

    def test_unsupported_format(self, config):
        handler = FileHandler(config)

        with pytest.raises(ValueError):
            handler.detect_format("test.txt")


class TestJsonWalker:
    """Test JSON walker functionality."""

    def test_parse_path(self):
        segments = JsonWalker.parse_path("patient.contacts[*].email")
        assert segments == [
            ('patient', False),
            ('contacts', True),
            ('email', False),
        ]

    def test_find_pii_paths_in_schema(self):
        schema = {
            'id': pl.Int64,
            'patient': pl.Struct([
                pl.Field('name', pl.Utf8),
                pl.Field('phone', pl.Utf8),
            ]),
        }

        pii_names = {'name', 'phone'}
        paths = JsonWalker.find_pii_paths_in_schema(schema, pii_names)

        assert 'patient.name' in paths
        assert 'patient.phone' in paths

    def test_collect_unique_string_values(self):
        data = {
            'name': 'John',
            'contacts': [
                {'email': 'john@test.com', 'phone': '555-0100'},
                {'email': 'jane@test.com', 'phone': '555-0101'},
            ],
        }

        result = JsonWalker.collect_unique_string_values(data)

        assert 'John' in result.get('name', set())
        assert 'john@test.com' in result.get('contacts[*].email', set())
