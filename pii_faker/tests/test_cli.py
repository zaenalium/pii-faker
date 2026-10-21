"""End-to-end tests that run the README's CLI examples through the real app."""

import json
import os
import stat
import sys
import time

import polars as pl
import pytest
import xlsxwriter
from typer.testing import CliRunner

from pii_faker.cli.main import app

runner = CliRunner()

CSV = """id,patient_name,email,phone,note
1,John Smith,john@x.com,555-1234,hello
2,Jane Doe,jane@y.org,555-9876,hi
3,John Smith,john@x.com,555-1234,again
"""

NDJSON = "\n".join(json.dumps(row) for row in [
    {"patient": {"patient_name": "John Smith",
                 "contacts": [{"email": "a@b.com"}, {"email": "c@d.com"}]}, "id": 1},
    {"patient": {"patient_name": "Jane Doe", "contacts": [{"email": "e@f.com"}]}, "id": 2},
]) + "\n"

NESTED_PATHS = "patient.patient_name,patient.contacts[*].email"


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def run(*args, **kwargs):
    return runner.invoke(app, [str(a) for a in args], **kwargs)


@pytest.fixture
def csv_file(tmp_path):
    path = tmp_path / "data.csv"
    path.write_text(CSV)
    return path


@pytest.fixture
def ndjson_file(tmp_path):
    path = tmp_path / "data.ndjson"
    path.write_text(NDJSON)
    return path


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep ~/.pii_faker out of the real home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


class TestFakeAndRestore:
    def test_csv_round_trip(self, tmp_path, csv_file):
        out, mapping, back = tmp_path / "out.csv", tmp_path / "map.json", tmp_path / "back.csv"

        result = run("process", csv_file, "--output", out, "--mapping-out", mapping, "--seed", 42)
        assert result.exit_code == 0, result.output

        faked = pl.read_csv(out)
        original = pl.read_csv(csv_file)
        for column in ("patient_name", "email", "phone"):
            assert faked[column].to_list() != original[column].to_list(), column
        assert faked["note"].to_list() == original["note"].to_list()
        assert faked["patient_name"][0] == faked["patient_name"][2]     # repeats stay consistent

        result = run("restore", out, "--mapping", mapping, "--output", back)
        assert result.exit_code == 0, result.output
        assert back.read_text() == CSV

    def test_same_seed_reproduces_the_output(self, tmp_path, csv_file):
        for name in ("a", "b"):
            run("process", csv_file, "-o", tmp_path / f"{name}.csv", "-m", tmp_path / f"{name}.json", "--seed", 7)
        assert (tmp_path / "a.csv").read_text() == (tmp_path / "b.csv").read_text()

    def test_sqlite_mapping(self, tmp_path, csv_file):
        out, mapping, back = tmp_path / "out.csv", tmp_path / "map.db", tmp_path / "back.csv"
        assert run("process", csv_file, "-o", out, "-m", mapping).exit_code == 0
        assert run("restore", out, "-m", mapping, "-o", back).exit_code == 0
        assert back.read_text() == CSV

    def test_text_restore(self, tmp_path, csv_file):
        out, mapping = tmp_path / "out.csv", tmp_path / "map.json"
        run("process", csv_file, "-o", out, "-m", mapping, "--seed", 1)
        fake = pl.read_csv(out)["patient_name"][0]

        llm = tmp_path / "llm.txt"
        llm.write_text(f"The record for {fake} looks fine.")
        restored = tmp_path / "restored.txt"

        result = run("restore", llm, "--mapping", mapping, "--output", restored, "--text")
        assert result.exit_code == 0, result.output
        assert restored.read_text() == "The record for John Smith looks fine."

    def test_token_redaction_round_trip(self, tmp_path, csv_file):
        out, mapping, back = tmp_path / "out.csv", tmp_path / "map.json", tmp_path / "back.csv"
        run("process", csv_file, "--mode", "redact", "--redact-style", "token", "-o", out, "-m", mapping)
        assert "REDACTED_" in out.read_text()
        run("restore", out, "-m", mapping, "-o", back)
        assert back.read_text() == CSV


class TestRedaction:
    def test_partial_style(self, tmp_path, csv_file):
        out = tmp_path / "red.xlsx"
        pl.read_csv(csv_file).write_excel(tmp_path / "in.xlsx")
        result = run("process", tmp_path / "in.xlsx", "--mode", "redact", "--redact-style", "partial",
                     "-o", out, "-m", tmp_path / "map.json")
        assert result.exit_code == 0, result.output
        phones = pl.read_excel(out, engine="calamine")["phone"].to_list()
        assert phones == ["XXXX1234", "XXXX9876", "XXXX1234"]

    def test_full_style_needs_no_mapping_warning(self, tmp_path, csv_file):
        result = run("process", csv_file, "--mode", "redact", "-o", tmp_path / "out.csv")
        assert result.exit_code == 0
        assert "--mapping-out" not in result.output
        assert set(pl.read_csv(tmp_path / "out.csv")["patient_name"]) == {"[REDACTED]"}


class TestNestedJson:
    def test_nested_ndjson_with_explicit_paths(self, tmp_path, ndjson_file):
        """README Scenario B."""
        out, mapping, back = tmp_path / "out.ndjson", tmp_path / "map.json", tmp_path / "back.ndjson"

        result = run("process", ndjson_file, "--extra-pii-columns", NESTED_PATHS,
                     "--output", out, "--mapping-out", mapping, "--seed", 7)
        assert result.exit_code == 0, result.output
        assert "No PII columns" not in result.output

        faked = rows(out)
        assert faked[0]["patient"]["patient_name"] != "John Smith"
        assert faked[0]["patient"]["contacts"][0]["email"] != "a@b.com"
        assert faked[0]["id"] == 1

        assert run("restore", out, "-m", mapping, "-o", back).exit_code == 0
        assert rows(back) == rows(ndjson_file)

    def test_bare_names_match_at_any_depth_by_default(self, tmp_path, ndjson_file):
        out = tmp_path / "out.ndjson"
        assert run("process", ndjson_file, "-o", out, "-m", tmp_path / "m.json").exit_code == 0
        assert "John Smith" not in out.read_text()
        assert "a@b.com" not in out.read_text()

    def test_strict_paths_limit_bare_names_to_the_top_level(self, tmp_path, ndjson_file):
        out = tmp_path / "out.ndjson"
        result = run("process", ndjson_file, "--strict-paths", "-o", out, "-m", tmp_path / "m.json",
                     input="n\n")
        assert "No PII columns detected" in result.output
        assert not out.exists()

    def test_strict_paths_still_apply_explicit_paths(self, tmp_path, ndjson_file):
        out = tmp_path / "out.ndjson"
        result = run("process", ndjson_file, "--strict-paths", "-e", NESTED_PATHS,
                     "-o", out, "-m", tmp_path / "m.json")
        assert result.exit_code == 0, result.output
        assert "John Smith" not in out.read_text()

    def test_json_strings_in_cells(self, tmp_path):
        """README Scenario A."""
        source = tmp_path / "data.csv"
        pl.DataFrame({
            "id": [1, 2],
            "payload": ['{"email":"a@b.com","name":"John Smith"}', '{"email":"c@d.com","name":"Jane Doe"}'],
            "other": ["keep", "keep"],
        }).write_csv(source)
        out, mapping, back = tmp_path / "out.csv", tmp_path / "map.json", tmp_path / "back.csv"

        result = run("process", source, "--json-columns", "payload", "-o", out, "-m", mapping, "--seed", 7)
        assert result.exit_code == 0, result.output
        faked = pl.read_csv(out)
        assert json.loads(faked["payload"][0])["email"] != "a@b.com"
        assert faked["other"].to_list() == ["keep", "keep"]

        assert run("restore", out, "-m", mapping, "-o", back).exit_code == 0
        assert back.read_text() == source.read_text()

    def test_infer_schema_length_zero_scans_every_row(self, tmp_path):
        rows = [{"id": i} for i in range(150)] + [{"id": 150, "patient": {"patient_name": "Late Bloomer"}}]
        source = tmp_path / "late.ndjson"
        source.write_text("\n".join(json.dumps(r) for r in rows))

        sampled = run("process", source, "-o", tmp_path / "a.ndjson", input="n\n")
        assert "No PII columns detected" in sampled.output

        out = tmp_path / "b.ndjson"
        full = run("process", source, "--infer-schema-length", 0, "-o", out, "-m", tmp_path / "m.json")
        assert full.exit_code == 0, full.output
        assert "Late Bloomer" not in out.read_text()


class TestDryRun:
    def test_writes_nothing_and_reports_nested_counts(self, tmp_path, ndjson_file):
        out = tmp_path / "out.ndjson"
        result = run("process", ndjson_file, "-e", NESTED_PATHS, "-o", out, "--dry-run")
        assert result.exit_code == 0, result.output
        assert "Total unique PII values to process: 5" in result.output
        assert not out.exists()

    def test_flat_columns(self, tmp_path, csv_file):
        result = run("process", csv_file, "-o", tmp_path / "out.csv", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "Total unique PII values to process: 6" in result.output


class TestMappingSafety:
    def test_warns_when_no_mapping_is_written_for_a_reversible_mode(self, tmp_path, csv_file):
        result = run("process", csv_file, "-o", tmp_path / "out.csv")
        assert result.exit_code == 0
        assert "--mapping-out" in result.output

    def test_no_output_is_written_when_the_mapping_cannot_be_saved(self, tmp_path, csv_file):
        out = tmp_path / "out.csv"
        result = run("process", csv_file, "-o", out, "-m", tmp_path / "missing_dir" / "map.json")
        assert result.exit_code == 1
        assert not out.exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.parametrize("name", ["map.json", "map.db"])
    def test_mapping_files_are_owner_only(self, tmp_path, csv_file, name):
        mapping = tmp_path / name
        run("process", csv_file, "-o", tmp_path / "out.csv", "-m", mapping)
        assert stat.S_IMODE(os.stat(mapping).st_mode) == 0o600

    def test_matched_but_non_text_columns_are_called_out(self, tmp_path):
        source = tmp_path / "num.csv"
        source.write_text("id,phone,name\n1,5551234,Al Bo\n2,5559876,Cy Di\n")
        out = tmp_path / "out.csv"
        result = run("process", source, "-o", out, "-m", tmp_path / "m.json")
        assert result.exit_code == 0
        assert "left unchanged" in result.output
        assert "phone" in result.output
        assert "5551234" in out.read_text()


class TestSheets:
    @pytest.fixture
    def workbook(self, tmp_path):
        path = tmp_path / "book.xlsx"
        book = xlsxwriter.Workbook(str(path))
        pl.DataFrame({"email": ["a@x.com", "b@x.com"], "v": [1, 2]}).write_excel(book, worksheet="Sheet1")
        pl.DataFrame({"email": ["b@x.com", "c@x.com"], "v": [3, 4]}).write_excel(book, worksheet="Sheet3")
        book.close()
        return path

    @pytest.mark.parametrize("flag", [["--all-sheets"], ["--sheets", "Sheet1,Sheet3"]])
    def test_each_sheet_round_trips_through_one_mapping(self, tmp_path, workbook, flag):
        mapping = tmp_path / "map.json"
        result = run("process", workbook, *flag, "-o", tmp_path / "out.xlsx", "-m", mapping, "--seed", 3)
        assert result.exit_code == 0, result.output

        emails = {}
        for sheet, expected in (("Sheet1", ["a@x.com", "b@x.com"]), ("Sheet3", ["b@x.com", "c@x.com"])):
            faked = tmp_path / f"out_{sheet}.xlsx"
            emails[sheet] = pl.read_excel(faked, engine="calamine")["email"].to_list()
            assert emails[sheet] != expected

            back = tmp_path / f"back_{sheet}.xlsx"
            assert run("restore", faked, "-m", mapping, "-o", back).exit_code == 0
            assert pl.read_excel(back, engine="calamine")["email"].to_list() == expected

        # b@x.com appears in both sheets and gets one fake.
        assert emails["Sheet1"][1] == emails["Sheet3"][0]


class TestCustomColumns:
    def test_add_list_remove(self):
        assert "Added" in run("columns", "add", "ssn").output
        assert "ssn" in run("columns", "list").output
        assert "already exists" in run("columns", "add", "ssn").output
        assert "Removed" in run("columns", "remove", "ssn").output
        assert "(none)" in run("columns", "list").output

    def test_saved_columns_apply_to_process(self, tmp_path):
        source = tmp_path / "ssn.csv"
        source.write_text("id,ssn,note\n1,123-45-6789,hi\n")
        run("columns", "add", "ssn")

        out = tmp_path / "out.csv"
        result = run("process", source, "-o", out, "-m", tmp_path / "m.json")
        assert result.exit_code == 0, result.output
        assert "123-45-6789" not in out.read_text()

    def test_saved_columns_combine_with_command_line_columns(self, tmp_path):
        source = tmp_path / "data.csv"
        source.write_text("ssn,dob_x,note\n123-45-6789,born-1990,hi\n")
        run("columns", "add", "ssn")

        out = tmp_path / "out.csv"
        run("process", source, "-e", "dob_x", "-o", out, "-m", tmp_path / "m.json")
        text = out.read_text()
        assert "123-45-6789" not in text
        assert "born-1990" not in text


class TestCleanupCommand:
    @staticmethod
    def make(directory, name, days_old):
        path = directory / name
        path.write_text("{}")
        old = time.time() - days_old * 86400
        os.utime(path, (old, old))
        return path

    def test_removes_the_mapping_names_used_throughout_the_readme(self, tmp_path):
        old = [self.make(tmp_path, name, 10) for name in ("mapping.json", "mapping_2020-01-01_x.json", "mapping.db")]
        keep = [self.make(tmp_path, "mapping.py", 10), self.make(tmp_path, "mapping.json.bak", 10),
                self.make(tmp_path, "mapping_new.json", 1)]

        result = run("cleanup", "--older-than", "7d", "--directory", tmp_path)

        assert result.exit_code == 0, result.output
        assert "Deleted 3 mapping file(s)" in result.output
        assert not any(path.exists() for path in old)
        assert all(path.exists() for path in keep)

    def test_hours(self, tmp_path):
        two_hours = self.make(tmp_path, "mapping.json", 2 / 24)
        run("cleanup", "--older-than", "1h", "-d", tmp_path)
        assert not two_hours.exists()

    def test_rejects_a_bad_age(self, tmp_path):
        result = run("cleanup", "--older-than", "soon", "-d", tmp_path)
        assert result.exit_code == 1
        assert "Invalid format" in result.output

    def test_defaults_to_the_mappings_folder_in_the_home_directory(self, isolated_home):
        folder = isolated_home / ".pii_faker" / "mappings"
        folder.mkdir(parents=True)
        old = self.make(folder, "mapping.json", 30)
        assert "Deleted 1" in run("cleanup").output
        assert not old.exists()


class TestServeCommand:
    @pytest.fixture
    def server(self, monkeypatch):
        """Capture what `serve` would start instead of starting it."""
        import uvicorn
        from pii_faker.web import app as web

        started = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, host, port: started.update(host=host, port=port))
        monkeypatch.delenv("PII_FAKER_TOKEN", raising=False)
        return started, web.app

    @staticmethod
    def flat(result):
        return result.output.replace("\n", "")

    def test_prints_a_complete_sign_in_link(self, server):
        started, web_app = server
        result = run("serve", "--port", 9100)

        assert result.exit_code == 0, result.output
        assert started == {"host": "127.0.0.1", "port": 9100}
        assert f"http://127.0.0.1:9100/?token={web_app.state.auth.token}" in self.flat(result)
        assert "minutes" in result.output

    def test_a_supplied_token_is_not_echoed(self, server):
        _, web_app = server
        result = run("serve", "--token", "my-own-secret-token-123")
        assert web_app.state.auth.token == "my-own-secret-token-123"
        assert "my-own-secret-token-123" not in result.output

    def test_token_from_the_environment(self, server, monkeypatch):
        _, web_app = server
        monkeypatch.setenv("PII_FAKER_TOKEN", "from-the-environment-123")
        assert run("serve").exit_code == 0
        assert web_app.state.auth.token == "from-the-environment-123"

    def test_weak_tokens_are_refused_before_starting(self, server):
        started, _ = server
        result = run("serve", "--token", "short")
        assert result.exit_code == 1
        assert "at least 16 characters" in self.flat(result)
        assert started == {}

    def test_retention_is_configurable_and_bounded(self, server):
        _, web_app = server
        assert run("serve", "--retention-minutes", 5).exit_code == 0
        assert web_app.state.retention_seconds == 300
        assert run("serve", "--retention-minutes", 0).exit_code != 0

    def test_warns_when_exposed_beyond_localhost(self, server):
        result = run("serve", "--host", "0.0.0.0")
        assert "plain HTTP" in self.flat(result)


def test_version():
    result = run("--version")
    assert result.exit_code == 0
    assert "PII Faker" in result.output
