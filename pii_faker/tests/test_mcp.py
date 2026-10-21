"""Tests for the MCP gateway: what the model can see, and that it can never see real values."""

import asyncio
import json
import logging
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import polars as pl
import pytest
from typer.testing import CliRunner

pytest.importorskip("mcp")

import httpx2                                                                     # noqa: E402
import uvicorn                                                                    # noqa: E402
from mcp import Client, ClientSession                                             # noqa: E402
from mcp.client.stdio import StdioServerParameters                                # noqa: E402
from mcp.client.streamable_http import streamable_http_client                     # noqa: E402

from pii_faker.cli.main import app                                                # noqa: E402
from pii_faker.core import PIIConfig                                              # noqa: E402
from pii_faker.mcp_server import (                                                # noqa: E402
    BearerAuthMiddleware,
    Gateway,
    build_http_app,
    build_server,
    parse_dataset_specs,
)

# Recognisable strings planted in every kind of place. None may ever reach the model.
NAMES = ["Zelda Quill", "Orson Pike"]
EMAILS = ["zelda.quill@canary-mail.test", "orson.pike@canary-mail.test"]
PHONES = ["5550142", "5550177"]
SECRETS = ["rare-condition-xyzzy", "second-secret-plugh", "free text mentioning Zelda Quill"]
CANARIES = NAMES + EMAILS + PHONES + SECRETS + ["1984-03-12", "1991-11-02", "Zelda", "Quill", "Pike"]


def patients():
    return pl.DataFrame({
        "patient_name": [NAMES[0], NAMES[1], NAMES[0]],
        "email": [EMAILS[0], EMAILS[1], EMAILS[0]],
        "phone": [int(PHONES[0]), int(PHONES[1]), int(PHONES[0])],     # numeric PII: faked as numbers
        "date_of_birth": ["1984-03-12", "1991-11-02", "1984-03-12"],
        "diagnosis": [SECRETS[0], SECRETS[1], SECRETS[0]],             # not recognised as PII
        "note": [SECRETS[2]] * 3,                                       # free text containing a name
        "age": [34, 51, 34],
        "visits": [3, 7, 3],
    })


def unfakeable():
    """PII by name, but a type the pipeline cannot process: it must be dropped, never shown."""
    return pl.DataFrame({
        "patient_name": [NAMES[0], NAMES[1]],
        "phone": pl.Series("phone", [PHONES[0].encode(), PHONES[1].encode()]),
        "age": [34, 51],
    })


def nested():
    return pl.DataFrame({
        "patient": [
            {"patient_name": NAMES[0], "diagnosis": SECRETS[0],
             "contacts": [{"email": EMAILS[0], "label": "home-label"}]},
            {"patient_name": NAMES[1], "diagnosis": SECRETS[1],
             "contacts": [{"email": EMAILS[1], "label": "work-label"}, {"email": None, "label": "spare"}]},
        ],
        "internal_id": [101, 102],
    })


def documents():
    return pl.DataFrame({
        "id": [1, 2, 3],
        "payload": [
            json.dumps({"email": EMAILS[0], "diagnosis": SECRETS[0], "meta": {"phone": PHONES[0], "ward": "w1"}}),
            json.dumps({"email": EMAILS[1], "diagnosis": SECRETS[1]}),
            "not valid json " + SECRETS[2],
        ],
    })


def gateway(**kwargs):
    config = kwargs.pop("config", None) or PIIConfig(seed=1)
    return Gateway(config, **kwargs)


def use(gw, fn):
    """Run `fn(client)` against the server in-process."""
    async def go():
        async with Client(build_server(gw)) as client:
            return await fn(client)
    return asyncio.run(go())


def call(gw, tool, **arguments):
    async def go(client):
        return await client.call_tool(tool, arguments)
    return use(gw, go)


def data(result):
    assert not result.is_error, result.content[0].text
    return result.structured_content["result"]


def everything(result):
    """All text a result carries to the model."""
    return json.dumps(result.structured_content) + "".join(getattr(c, "text", "") for c in result.content)


def assert_no_canaries(text):
    leaked = [c for c in CANARIES if c in text]
    assert not leaked, f"real values reached the model: {leaked}"


# ---------------------------------------------------------------------------
# What is exposed
# ---------------------------------------------------------------------------

class TestExposure:
    def test_only_faked_columns_pass_by_default(self):
        report = gateway().add_dataset("p", patients())
        assert report.faked == ["date_of_birth", "email", "patient_name", "phone"]
        assert report.kept == []
        assert set(report.dropped) == {"diagnosis", "note", "age", "visits"}

    def test_numeric_pii_is_faked_as_numbers_not_dropped(self):
        gw = gateway()
        gw.add_dataset("p", patients())
        phones = gw.datasets["p"].frame["phone"]
        assert phones.dtype == pl.Int64 and phones.to_list()[0] == phones.to_list()[2]
        assert int(PHONES[0]) not in phones.to_list() and int(PHONES[1]) not in phones.to_list()

    def test_pii_that_cannot_be_processed_is_dropped_and_reported(self):
        gw = gateway()
        report = gw.add_dataset("p", unfakeable())
        assert report.blocked == ["phone"]
        assert "phone" in report.dropped and "phone" not in gw.datasets["p"].frame.columns
        assert gw.skipped == ["phone"]

    def test_keep_exposes_named_columns_with_original_values(self):
        gw = gateway(keep=["age", "visits"])
        report = gw.add_dataset("p", patients())
        assert report.kept == ["age", "visits"]
        assert gw.datasets["p"].frame["age"].to_list() == [34, 51, 34]
        assert "diagnosis" in report.dropped

    def test_keep_unmatched_exposes_the_rest_but_never_blocked_pii(self):
        gw = gateway(keep_unmatched=True)
        report = gw.add_dataset("p", patients())
        assert "diagnosis" in report.kept and "note" in report.kept

        report = gateway(keep_unmatched=True).add_dataset("u", unfakeable())
        assert "age" in report.kept and "phone" in report.dropped               # matched by name: still blocked

    def test_explicitly_keeping_a_blocked_column_is_allowed(self):
        gw = gateway(keep=["phone"])
        gw.add_dataset("u", unfakeable())
        assert gw.datasets["u"].frame["phone"].to_list() == [PHONES[0].encode(), PHONES[1].encode()]

    def test_nested_columns_keep_only_allowed_leaves(self):
        gw = gateway()
        report = gw.add_dataset("n", nested())
        frame = gw.datasets["n"].frame

        assert frame.columns == ["patient"]                                      # internal_id dropped
        row = frame["patient"][1]
        assert set(row) == {"patient_name", "contacts"}                          # diagnosis pruned
        assert set(row["contacts"][0]) == {"email"}                              # label pruned
        assert row["patient_name"] != NAMES[1] and row["contacts"][0]["email"] != EMAILS[1]
        assert row["contacts"][1]["email"] is None
        assert sorted(report.pruned) == ["patient.contacts.label", "patient.diagnosis"]
        assert "patient" in gw.datasets["n"].faked_columns

    def test_keeping_a_nested_path(self):
        gw = gateway(keep=["patient.diagnosis", "patient.contacts.label"])
        gw.add_dataset("n", nested())
        row = gw.datasets["n"].frame["patient"][0]
        assert row["diagnosis"] == SECRETS[0] and row["contacts"][0]["label"] == "home-label"

    def test_structures_the_pipeline_cannot_see_into_are_dropped_not_leaked(self):
        # The detector looks one list level deep; PII in a list of lists is never faked, so the
        # allowlist must keep that column out rather than show it as-is.
        deep = [[[{"email": EMAILS[0], "tag": "t1"}], [{"email": EMAILS[1], "tag": "t2"}]]]
        gw = gateway()
        report = gw.add_dataset("g", pl.DataFrame({"groups": deep, "patient_name": [NAMES[0]]}))
        assert gw.datasets["g"].frame.columns == ["patient_name"]
        assert "groups" in report.dropped
        assert EMAILS[0] not in json.dumps(gw.datasets["g"].frame.to_dicts())

        with pytest.raises(ValueError, match="Nothing in 'h' would be exposed"):
            gateway().add_dataset("h", pl.DataFrame({"groups": deep}))

    def test_json_documents_keep_only_allowed_keys(self):
        gw = gateway(config=PIIConfig(seed=1, json_columns=["payload"]))
        gw.add_dataset("d", documents())
        frame = gw.datasets["d"].frame

        assert frame.columns == ["payload"]                                      # id dropped
        first = json.loads(frame["payload"][0])
        assert set(first) == {"email", "meta"} and set(first["meta"]) == {"phone"}
        assert first["email"] != EMAILS[0] and first["meta"]["phone"] != PHONES[0]
        assert "diagnosis" not in first
        assert frame["payload"][2] is None                                       # unparseable: nothing shown

    def test_keeping_a_json_key(self):
        gw = gateway(config=PIIConfig(seed=1, json_columns=["payload"]), keep=["payload:diagnosis"])
        gw.add_dataset("d", documents())
        assert json.loads(gw.datasets["d"].frame["payload"][1])["diagnosis"] == SECRETS[1]

    def test_refuses_a_dataset_with_nothing_to_show(self):
        with pytest.raises(ValueError, match="Nothing in 'x' would be exposed"):
            gateway().add_dataset("x", pl.DataFrame({"diagnosis": ["a"], "age": [1]}))

    def test_the_same_person_looks_the_same_across_datasets(self):
        gw = gateway()
        gw.add_dataset("a", pl.DataFrame({"patient_name": [NAMES[0]]}))
        gw.add_dataset("b", pl.DataFrame({"member_name": [NAMES[0], NAMES[1]]}))
        assert gw.datasets["a"].frame["patient_name"][0] == gw.datasets["b"].frame["member_name"][0]

    def test_workbooks_use_the_first_sheet(self, tmp_path):
        path = tmp_path / "book.xlsx"
        patients().write_excel(path)
        gw = gateway()
        gw.add_dataset("book", path)
        assert gw.datasets["book"].frame.height == 3

    def test_partial_redaction_is_refused(self):
        with pytest.raises(ValueError, match="Partial redaction"):
            Gateway(PIIConfig(mode="redact", redact_style="partial"))

    def test_token_redaction_works(self):
        gw = Gateway(PIIConfig(mode="redact", redact_style="token"))
        gw.add_dataset("p", patients())
        assert gw.datasets["p"].frame["patient_name"][0].startswith("REDACTED_")

    @pytest.mark.parametrize("spec, message", [
        ("noequals", "must look like"),
        ("bad name=x.csv", "may only use"),
        ("../up=x.csv", "may only use"),
        ("ok=/no/such/file.csv", "not found"),
    ])
    def test_dataset_specs_are_validated(self, spec, message):
        with pytest.raises(ValueError, match=message):
            parse_dataset_specs([spec])

    def test_dataset_specs(self, tmp_path):
        path = tmp_path / "a.csv"
        path.write_text("x\n1\n")
        assert parse_dataset_specs([f"people={path}"]) == {"people": path}
        with pytest.raises(ValueError, match="used twice"):
            parse_dataset_specs([f"a={path}", f"a={path}"])
        with pytest.raises(ValueError, match="at least one"):
            parse_dataset_specs([])


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

@pytest.fixture
def loaded():
    gw = gateway(keep=["age", "visits"], max_rows=5)
    gw.add_dataset("patients", patients())
    gw.add_dataset("nested", nested())
    return gw


class TestTools:
    def test_only_read_only_tools_exist_by_default(self, loaded):
        async def go(client):
            return (await client.list_tools()).tools
        tools = use(loaded, go)
        assert sorted(t.name for t in tools) == [
            "aggregate", "describe_dataset", "list_datasets", "read_rows", "search_rows",
        ]
        assert all(t.annotations.read_only_hint for t in tools)

    def test_instructions_tell_the_model_the_data_is_synthetic(self, loaded):
        async def go(client):
            return client.instructions
        text = use(loaded, go)
        assert "synthetic" in text and "exactly" in text

    def test_list_and_describe(self, loaded):
        listing = data(call(loaded, "list_datasets"))
        assert {d["name"] for d in listing["datasets"]} == {"patients", "nested"}

        info = data(call(loaded, "describe_dataset", dataset="patients"))
        columns = {c["name"]: c for c in info["columns"]}
        assert set(columns) == {"patient_name", "email", "phone", "date_of_birth", "age", "visits"}
        assert columns["patient_name"]["faked"] is True and columns["age"]["faked"] is False
        assert columns["patient_name"]["unique"] == 2
        assert len(info["sample"]) == 3

    def test_read_rows_pages_and_selects_columns(self, loaded):
        page = data(call(loaded, "read_rows", dataset="patients", limit=2, columns=["patient_name", "age"]))
        assert page["columns"] == ["patient_name", "age"] and page["returned"] == 2
        assert page["total"] == 3 and page["has_more"] is True

        rest = data(call(loaded, "read_rows", dataset="patients", offset=2, limit=2))
        assert rest["returned"] == 1 and rest["has_more"] is False
        assert data(call(loaded, "read_rows", dataset="patients", offset=99))["rows"] == []

    def test_row_limit_is_capped_by_the_server(self):
        gw = gateway(keep=["n"], max_rows=5)
        gw.add_dataset("big", pl.DataFrame({"patient_name": [f"Person {i}" for i in range(50)], "n": range(50)}))
        page = data(call(gw, "read_rows", dataset="big", limit=10_000))
        assert page["returned"] == 5 and page["has_more"] is True

    def test_long_cells_are_truncated(self):
        gw = gateway(keep=["bio"])
        gw.max_cell_chars = 20
        gw.add_dataset("t", pl.DataFrame({"patient_name": ["A B"], "bio": ["x" * 500]}))
        assert data(call(gw, "read_rows", dataset="t"))["rows"][0]["bio"] == "x" * 20 + "…"

    def test_search(self, loaded):
        fake = loaded.datasets["patients"].frame["patient_name"][0]
        found = data(call(loaded, "search_rows", dataset="patients", column="patient_name", value=fake))
        assert found["total"] == 2 and found["rows"][0]["patient_name"] == fake

        part = data(call(loaded, "search_rows", dataset="patients", column="patient_name",
                         value=fake[:3].upper(), match="contains"))
        assert part["total"] >= 2
        assert data(call(loaded, "search_rows", dataset="patients", column="patient_name",
                         value=fake.upper(), case_sensitive=True))["total"] == 0
        assert data(call(loaded, "search_rows", dataset="patients", column="age", value="51"))["total"] == 1

    def test_the_real_values_cannot_be_searched_for(self, loaded):
        # A search runs on the faked copy, so it can't confirm that a guessed real name exists.
        for name in NAMES:
            assert data(call(loaded, "search_rows", dataset="patients", column="patient_name",
                             value=name, match="contains"))["total"] == 0

    def test_aggregate(self, loaded):
        by_person = data(call(loaded, "aggregate", dataset="patients", group_by=["patient_name"],
                              aggregations=[{"op": "count"}, {"op": "sum", "column": "visits"},
                                            {"op": "mean", "column": "age"}]))
        assert by_person["columns"] == ["patient_name", "count", "sum_visits", "mean_age"]
        assert sorted((r["count"], r["sum_visits"]) for r in by_person["rows"]) == [(1, 7), (2, 6)]

        assert data(call(loaded, "aggregate", dataset="patients"))["rows"] == [{"count": 3}]
        unique = data(call(loaded, "aggregate", dataset="patients",
                           aggregations=[{"op": "n_unique", "column": "patient_name"}]))
        assert unique["rows"] == [{"n_unique_patient_name": 2}]

    @pytest.mark.parametrize("tool, args, message", [
        ("describe_dataset", {"dataset": "nope"}, "Unknown dataset"),
        ("read_rows", {"dataset": "patients", "columns": ["nope"]}, "Unknown column"),
        ("search_rows", {"dataset": "patients", "column": "nope", "value": "x"}, "Unknown column"),
        ("search_rows", {"dataset": "nested", "column": "patient", "value": "x"}, "nested"),
        ("aggregate", {"dataset": "patients", "group_by": ["nope"]}, "Unknown column"),
        ("aggregate", {"dataset": "patients", "aggregations": [{"op": "sum", "column": "patient_name"}]}, "numeric"),
        ("aggregate", {"dataset": "patients", "aggregations": [{"op": "sum"}]}, "needs a column"),
        ("aggregate", {"dataset": "patients", "aggregations": [{"op": "count"}, {"op": "count"}]}, "twice"),
    ])
    def test_bad_requests_get_clear_errors(self, loaded, tool, args, message):
        result = call(loaded, tool, **args)
        assert result.is_error and message in result.content[0].text

    def test_a_model_cannot_name_a_path(self, loaded):
        result = call(loaded, "read_rows", dataset="/etc/passwd")
        assert result.is_error and "Unknown dataset" in result.content[0].text

    def test_asking_for_a_dropped_column_looks_like_asking_for_a_missing_one(self, loaded):
        result = call(loaded, "read_rows", dataset="patients", columns=["diagnosis"])
        text = result.content[0].text
        assert result.is_error and "Unknown column" in text
        assert "diagnosis" not in text.split("Available:")[1]            # dropped columns aren't listed
        assert "note" not in text.split("Available:")[1]

    def test_canaries_never_reach_the_model(self, loaded):
        """Plant real values everywhere, call every tool as broadly as possible, scan every byte returned."""
        everywhere = []

        async def sweep(client):
            for dataset in ("patients", "nested"):
                everywhere.append(await client.call_tool("describe_dataset", {"dataset": dataset}))
                everywhere.append(await client.call_tool("read_rows", {"dataset": dataset, "limit": 1000}))
                everywhere.append(await client.call_tool("aggregate", {"dataset": dataset}))
            everywhere.append(await client.call_tool("list_datasets", {}))
            # requests for things that were dropped, and probes with the real values
            for column in ("diagnosis", "note", "phone", "internal_id"):
                everywhere.append(await client.call_tool("read_rows", {"dataset": "patients", "columns": [column]}))
                everywhere.append(await client.call_tool("search_rows",
                                  {"dataset": "patients", "column": column, "value": "x", "match": "contains"}))
            for probe in CANARIES:
                everywhere.append(await client.call_tool("search_rows", {
                    "dataset": "patients", "column": "patient_name", "value": probe, "match": "contains"}))
            everywhere.append(await client.call_tool("describe_dataset", {"dataset": "../../etc/passwd"}))

        use(loaded, sweep)
        assert len(everywhere) > 25
        assert_no_canaries("\n".join(everything(r) for r in everywhere))

    def test_json_columns_never_leak_either(self):
        gw = gateway(config=PIIConfig(seed=1, json_columns=["payload"]))
        gw.add_dataset("docs", documents())
        page = call(gw, "read_rows", dataset="docs", limit=100)
        assert_no_canaries(everything(page))


class TestErrorsNeverEchoData:
    def test_unexpected_failures_become_one_generic_message(self, loaded, monkeypatch, caplog):
        def boom(*_args, **_kwargs):
            pl.Series([NAMES[0]]).cast(pl.Int64)             # Polars quotes the cell in its error message

        monkeypatch.setattr(Gateway, "read_rows", boom)
        with caplog.at_level(logging.DEBUG):
            result = call(loaded, "read_rows", dataset="patients")

        assert result.is_error and result.content[0].text.endswith("The request could not be completed.")
        assert_no_canaries(everything(result))
        assert_no_canaries(caplog.text)                      # nor does the server log carry the value
        assert "InvalidOperationError" in caplog.text        # the exception type is logged

    def test_the_original_error_text_is_not_chained_to_the_client(self, loaded, monkeypatch):
        monkeypatch.setattr(Gateway, "list_datasets", lambda self: (_ for _ in ()).throw(ValueError(NAMES[1])))
        assert_no_canaries(everything(call(loaded, "list_datasets")))


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------

class TestRestoreTool:
    @pytest.fixture
    def restoring(self, tmp_path):
        gw = gateway(restore_dir=tmp_path / "restored")
        gw.add_dataset("patients", patients())
        return gw

    def fake_names(self, gw):
        return gw.datasets["patients"].frame["patient_name"].unique().to_list()

    def test_not_offered_unless_a_restore_folder_is_configured(self, loaded):
        async def go(client):
            return [t.name for t in (await client.list_tools()).tools]
        assert "restore_to_file" not in use(loaded, go)

    def test_restores_into_a_local_file_and_returns_no_text(self, restoring, tmp_path):
        fake = self.fake_names(restoring)[0]
        result = call(restoring, "restore_to_file", text=f"Please review {fake} again, {fake}.", filename="note.txt")
        payload = data(result)

        assert payload == {"saved_as": "note.txt", "replacements": 2}
        assert_no_canaries(everything(result))
        assert str(tmp_path) not in everything(result)                          # not even the local path
        saved = (tmp_path / "restored" / "note.txt").read_text()
        assert saved in (f"Please review {NAMES[0]} again, {NAMES[0]}.", f"Please review {NAMES[1]} again, {NAMES[1]}.")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_files_and_folder_are_private(self, restoring, tmp_path):
        call(restoring, "restore_to_file", text="hello", filename="a.txt")
        folder = tmp_path / "restored"
        assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(folder / "a.txt").st_mode) == 0o600

    @pytest.mark.parametrize("name, expected", [
        ("../../escape.txt", "escape.txt"),
        ("..\\..\\win.txt", "win.txt"),
        ("/etc/passwd", "passwd.txt"),
        (".bashrc", "bashrc.txt"),
        ("we ird$name!.txt", "we_ird_name_.txt"),
        ("", "restored.txt"),
        ("report", "report.txt"),
    ])
    def test_file_names_cannot_escape_the_folder(self, restoring, tmp_path, name, expected):
        payload = data(call(restoring, "restore_to_file", text="x", filename=name))
        assert payload["saved_as"] == expected
        assert sorted(p.name for p in (tmp_path / "restored").iterdir()) == [expected]
        assert not (tmp_path / "escape.txt").exists()

    def test_never_overwrites(self, restoring, tmp_path):
        first = data(call(restoring, "restore_to_file", text="one", filename="same.txt"))
        second = data(call(restoring, "restore_to_file", text="two", filename="same.txt"))
        assert (first["saved_as"], second["saved_as"]) == ("same.txt", "same-1.txt")
        assert (tmp_path / "restored" / "same.txt").read_text() == "one"

    def test_oversized_text_is_refused(self, restoring):
        result = call(restoring, "restore_to_file", text="x" * 1_000_001)
        assert result.is_error and "too long" in result.content[0].text

    def test_full_redaction_cannot_be_restored(self, tmp_path):
        with pytest.raises(ValueError, match="can't be reversed"):
            Gateway(PIIConfig(mode="redact", redact_style="full"), restore_dir=tmp_path / "r")

    def test_mapping_can_be_saved_privately_for_later(self, restoring, tmp_path):
        path = tmp_path / "mapping.json"
        restoring.save_mapping(str(path))
        if sys.platform != "win32":
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert NAMES[0] in path.read_text()                  # the real values live here, never in a tool


# ---------------------------------------------------------------------------
# Remote use: HTTP transport
# ---------------------------------------------------------------------------

TOKEN = "gateway-token-0123456789abcdef"


class TestBearerAuth:
    @staticmethod
    def run(auth_header=None):
        calls = []

        async def inner(scope, receive, send):
            calls.append(scope["type"])
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        from starlette.testclient import TestClient
        client = TestClient(BearerAuthMiddleware(inner, TOKEN))
        headers = {"Authorization": auth_header} if auth_header is not None else {}
        return client.post("/mcp", headers=headers), calls

    def test_valid_token_passes(self):
        response, calls = self.run(f"Bearer {TOKEN}")
        assert response.status_code == 200 and calls == ["http"]

    @pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer wrong", f"Basic {TOKEN}", TOKEN, f"Bearer {TOKEN}x"])
    def test_everything_else_is_rejected_before_reaching_the_app(self, header):
        response, calls = self.run(header)
        assert response.status_code == 401 and calls == []
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"

    def test_scheme_is_case_insensitive(self):
        assert self.run(f"bearer {TOKEN}")[0].status_code == 200

    def test_short_tokens_are_refused(self, loaded):
        with pytest.raises(ValueError, match="at least"):
            build_http_app(build_server(loaded), "short", "127.0.0.1", 8765)


@pytest.fixture
def live(loaded):
    """The real HTTP app, served by uvicorn on a free port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    http_app = build_http_app(build_server(loaded), TOKEN, "127.0.0.1", port, allowed_hosts=["tunnel.example.test"])
    server = uvicorn.Server(uvicorn.Config(http_app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(10)


class TestHttpTransport:
    HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json, text/event-stream"}

    def post(self, url, headers):
        async def go():
            async with httpx2.AsyncClient() as http:
                return await http.post(url, json={}, headers=headers)
        return asyncio.run(go())

    def test_a_real_client_with_the_token_can_use_the_tools(self, live):
        async def go():
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
                async with streamable_http_client(live, http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        tools = [t.name for t in (await session.list_tools()).tools]
                        rows = await session.call_tool("read_rows", {"dataset": "patients"})
                        return tools, rows
        tools, rows = asyncio.run(go())
        assert "read_rows" in tools
        assert_no_canaries(everything(rows))
        assert rows.structured_content["result"]["rows"][0]["patient_name"] not in NAMES

    def test_requests_without_the_token_are_rejected(self, live):
        assert self.post(live, {}).status_code == 401
        assert self.post(live, {"Authorization": "Bearer wrong"}).status_code == 401

    def test_unknown_hosts_are_rejected_even_with_the_token(self, live):
        # DNS-rebinding protection: the Host header must be one we expect.
        assert self.post(live, {**self.HEADERS, "Host": "evil.example"}).status_code == 421

    def test_the_tunnel_hostname_is_accepted_when_allowed(self, live):
        # Auth and host checks pass (the junk body then fails as a bad MCP request, not a 401/421).
        status = self.post(live, {**self.HEADERS, "Host": "tunnel.example.test"}).status_code
        assert status not in (401, 421)

    def test_a_tunnel_hostname_that_was_not_allowed_is_rejected(self, live):
        assert self.post(live, {**self.HEADERS, "Host": "other-tunnel.example.test"}).status_code == 421

    def test_browser_origins_must_match(self, live):
        status = self.post(live, {**self.HEADERS, "Origin": "https://evil.example"}).status_code
        assert status == 403


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def patients_csv(tmp_path):
    path = tmp_path / "patients.csv"
    patients().write_csv(path)
    return path


class TestMcpCommand:
    def invoke(self, *args):
        return runner.invoke(app, ["mcp", *map(str, args)])

    def test_dry_run_lists_what_would_be_exposed_and_serves_nothing(self, patients_csv):
        result = self.invoke("--dataset", f"patients={patients_csv}", "--keep", "age", "--dry-run")

        assert result.exit_code == 0, result.output
        assert result.stdout == ""                                      # stdout is reserved for the protocol
        report = result.stderr
        for column in ("patient_name", "email", "date_of_birth", "age", "diagnosis", "note", "phone", "visits"):
            assert column in report
        assert "original values" in report and "dropped" in report
        assert "Dry run" in report
        assert_no_canaries(result.stderr)                               # column names only, never values

    def test_nothing_exposed_is_an_error(self, tmp_path):
        path = tmp_path / "x.csv"
        path.write_text("diagnosis,age\na,1\n")
        result = self.invoke("--dataset", f"x={path}", "--dry-run")
        assert result.exit_code == 1 and "Nothing in 'x'" in result.stderr

    @pytest.mark.parametrize("args, message", [
        (["--dataset", "nonsense"], "must look like"),
        (["--dataset", "a=/no/such.csv"], "not found"),
        (["--dataset", "{csv}", "--mode", "redact", "--redact-style", "partial"], "Partial redaction"),
        (["--dataset", "{csv}", "--transport", "carrier-pigeon"], "stdio or http"),
        (["--dataset", "{csv}", "--mode", "redact", "--redact-style", "full", "--restore-dir", "{dir}"], "can't be reversed"),
    ])
    def test_bad_options(self, patients_csv, tmp_path, args, message):
        args = [a.format(csv=f"p={patients_csv}", dir=tmp_path / "r") for a in args]
        result = self.invoke(*args)
        assert result.exit_code == 1 and message in result.stderr

    def test_stdio_server_speaks_the_protocol_and_never_leaks(self, patients_csv, tmp_path):
        """The real command, over a real stdio pipe: also proves nothing else is printed to stdout."""
        mapping = tmp_path / "mapping.json"
        params = StdioServerParameters(command=sys.executable, args=[
            "-m", "pii_faker.cli.main", "mcp", "--dataset", f"patients={patients_csv}",
            "--keep", "age", "--seed", "1", "--mapping-out", str(mapping),
            "--restore-dir", str(tmp_path / "restored"),
        ])

        async def go():
            async with Client(params) as client:
                tools = [t.name for t in (await client.list_tools()).tools]
                rows = await client.call_tool("read_rows", {"dataset": "patients", "limit": 10})
                saved = await client.call_tool("restore_to_file", {
                    "text": rows.structured_content["result"]["rows"][0]["patient_name"], "filename": "who.txt"})
                return tools, rows, saved

        tools, rows, saved = asyncio.run(go())
        assert "restore_to_file" in tools
        assert_no_canaries(everything(rows) + everything(saved))
        assert (tmp_path / "restored" / "who.txt").read_text() == NAMES[0]
        assert NAMES[0] in mapping.read_text()

    def test_http_mode_announces_the_endpoint_and_token_on_stderr(self, patients_csv, monkeypatch):
        started = {}
        monkeypatch.setattr(uvicorn, "run", lambda app_, host, port, log_level: started.update(host=host, port=port))
        monkeypatch.delenv("PII_FAKER_MCP_TOKEN", raising=False)

        result = self.invoke("--dataset", f"p={patients_csv}", "--transport", "http", "--port", "9123",
                             "--allowed-host", "abc.trycloudflare.com")

        flat = result.stderr.replace("\n", "")
        assert result.exit_code == 0 and started == {"host": "127.0.0.1", "port": 9123}
        assert "http://127.0.0.1:9123/mcp" in flat and "Authorization: Bearer " in flat
        assert "abc.trycloudflare.com" in flat and result.stdout == ""

    def test_http_mode_warns_off_localhost_and_hides_a_supplied_token(self, patients_csv, monkeypatch):
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        result = self.invoke("--dataset", f"p={patients_csv}", "--transport", "http", "--host", "0.0.0.0",
                             "--token", "my-own-secret-token-123")
        assert "plain HTTP" in result.stderr.replace("\n", "")
        assert "my-own-secret-token-123" not in result.stderr

    def test_saved_custom_columns_are_faked_here_too(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        runner.invoke(app, ["columns", "add", "ticket_ref"])
        path = tmp_path / "t.csv"
        path.write_text("patient_name,ticket_ref\nAl Bo,TKT-1\n")

        result = self.invoke("--dataset", f"t={path}", "--dry-run")
        report = result.stderr
        assert result.exit_code == 0 and "ticket_ref" in report and "dropped" not in report

    def test_weak_token_is_refused(self, patients_csv, monkeypatch):
        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        result = self.invoke("--dataset", f"p={patients_csv}", "--transport", "http", "--token", "short")
        assert result.exit_code == 1 and "at least 16" in result.stderr.replace("\n", "")

    def test_warns_when_the_real_values_would_be_unrecoverable(self, patients_csv, monkeypatch):
        import pii_faker.mcp_server as module
        monkeypatch.setattr(module, "run_gateway", lambda *a, **k: None)
        result = self.invoke("--dataset", f"p={patients_csv}")
        assert "can't be" in result.stderr.replace("\n", "") and "recovered" in result.stderr.replace("\n", "")
