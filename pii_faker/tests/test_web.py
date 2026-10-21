"""Tests for the web UI: authentication, job lifecycle, and the detect -> process -> restore flow."""

import io
import json
import time
from pathlib import Path

import polars as pl
import pytest
from fastapi.testclient import TestClient

from pii_faker.web import app as web
from pii_faker.web.auth import SESSION_COOKIE, Authenticator

TOKEN = "test-token-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def clean_server():
    """A known token, and no jobs or temp files carried between tests."""
    web.app.state.auth = Authenticator(TOKEN)
    web.app.state.retention_seconds = 3600
    web.purge_all_jobs()
    yield
    web.purge_all_jobs()


@pytest.fixture
def client():
    """A client that sends the access token."""
    return TestClient(web.app, headers=BEARER)


@pytest.fixture
def anonymous():
    return TestClient(web.app)


def upload(client, name, content, mime="text/csv"):
    response = client.post("/api/detect", files={"file": (name, content, mime)})
    assert response.status_code == 200, response.text
    return response.json()


def process(client, job, columns, **extra):
    data = {"job_id": job, "selected_columns": ",".join(columns), "mode": "fake", "seed": "1", **extra}
    response = client.post("/api/process", data=data)
    assert response.status_code == 200, response.text
    return response.json()


def restore(client, mapping, content, name="processed.csv"):
    response = client.post("/api/restore", data={"is_text": "false"}, files={
        "mapping_file": ("map.json", mapping, "application/json"),
        "input_file": (name, content, "text/csv"),
    })
    assert response.status_code == 200, response.text
    return client.get(f"/download/restore/{response.json()['job_id']}").content


NESTED = "\n".join(json.dumps(row) for row in [
    {"patient": {"patient_name": "John Smith", "contacts": [{"email": "a@b.com"}]}, "id": 1},
    {"patient": {"patient_name": "Jane Doe", "contacts": [{"email": "c@d.com"}]}, "id": 2},
]).encode()


class TestAuthentication:
    @pytest.mark.parametrize("method, path", [
        ("post", "/api/detect"),
        ("post", "/api/process"),
        ("post", "/api/restore"),
        ("get", "/api/download/x/output"),
        ("get", "/download/restore/x"),
        ("delete", "/api/jobs/x"),
        ("post", "/logout"),
        ("get", "/static/.gitkeep"),
        ("get", "/docs"),
        ("get", "/some/future/route"),
    ])
    def test_everything_but_sign_in_requires_a_token(self, anonymous, method, path):
        response = getattr(anonymous, method)(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    def test_wrong_bearer_token_is_rejected(self, anonymous):
        response = anonymous.get("/api/download/x/output", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    def test_bearer_token_is_accepted(self, client):
        assert client.get("/api/download/missing/output").status_code == 404     # past auth

    def test_home_shows_sign_in_not_the_app(self, anonymous):
        response = anonymous.get("/")
        assert response.status_code == 401
        assert 'name="token"' in response.text
        assert "file-input" not in response.text

    def test_token_link_signs_in_and_redirects_without_the_token(self, anonymous):
        response = anonymous.get(f"/?token={TOKEN}", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        cookie = response.headers["set-cookie"].lower()
        assert SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert TOKEN not in response.headers["set-cookie"]          # the cookie is a session id

        home = anonymous.get("/")                                    # the client kept the cookie
        assert home.status_code == 200
        assert 'name="token"' not in home.text
        assert anonymous.get("/api/download/missing/output").status_code == 404

    def test_wrong_token_link_does_not_sign_in(self, anonymous):
        response = anonymous.get("/?token=nope", follow_redirects=False)
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert "not valid" in response.text

    def test_sign_in_form(self, anonymous):
        bad = anonymous.post("/login", data={"token": "nope"}, follow_redirects=False)
        assert bad.status_code == 401 and "set-cookie" not in bad.headers

        good = anonymous.post("/login", data={"token": TOKEN}, follow_redirects=False)
        assert good.status_code == 303
        assert anonymous.get("/api/download/missing/output").status_code == 404

    def test_sign_out_ends_the_session(self, anonymous):
        anonymous.post("/login", data={"token": TOKEN})
        assert anonymous.post("/logout").status_code == 200
        assert anonymous.get("/api/download/x/output").status_code == 401

    def test_expired_sessions_are_rejected(self, anonymous):
        anonymous.post("/login", data={"token": TOKEN})
        web.app.state.auth._sessions = {sid: 0 for sid in web.app.state.auth._sessions}
        assert anonymous.get("/api/download/x/output").status_code == 401

    def test_forged_session_cookie_is_rejected(self, anonymous):
        anonymous.cookies.set(SESSION_COOKIE, "forged")
        assert anonymous.get("/api/download/x/output").status_code == 401

    def test_responses_are_not_cached(self, client):
        response = client.get("/api/download/missing/output")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_short_tokens_are_refused(self):
        with pytest.raises(ValueError, match="at least"):
            Authenticator("short")

    def test_generated_tokens_are_strong_and_distinct(self):
        first, second = Authenticator(), Authenticator()
        assert first.generated and len(first.token) >= 32
        assert first.token != second.token


class TestJobLifecycle:
    def test_expired_jobs_and_their_files_are_deleted(self, client):
        job = upload(client, "data.csv", b"patient_name\nAl Bo\n")["job_id"]
        job_dir = Path(web.jobs[job]["temp_dir"])
        assert job_dir.exists()

        assert web.purge_expired_jobs(3600) == 0                                  # still fresh
        assert web.purge_expired_jobs(3600, now=time.time() + 3601) == 1
        assert not job_dir.exists() and job not in web.jobs
        assert client.get(f"/api/download/{job}/output").status_code == 404

    def test_activity_postpones_expiry(self, client):
        job = upload(client, "data.csv", b"patient_name\nAl Bo\n")["job_id"]
        web.jobs[job]["touched"] -= 3000
        client.get(f"/api/download/{job}/output")                                 # any access counts
        assert web.purge_expired_jobs(3600, now=time.time() + 1000) == 0

    def test_a_job_can_be_deleted_on_demand(self, client):
        job = upload(client, "data.csv", b"patient_name\nAl Bo\n")["job_id"]
        job_dir = Path(web.jobs[job]["temp_dir"])
        assert client.delete(f"/api/jobs/{job}").status_code == 200
        assert not job_dir.exists()
        assert client.delete(f"/api/jobs/{job}").status_code == 404

    def test_failed_uploads_leave_no_files_behind(self, client):
        response = client.post("/api/detect", files={"file": ("data.xyz", b"whatever", "text/plain")})
        assert response.status_code == 400
        assert not web.jobs
        assert list(web._job_root.iterdir()) == []

    def test_failed_restores_leave_no_files_behind(self, client):
        response = client.post("/api/restore", data={"is_text": "false"}, files={
            "mapping_file": ("map.json", b"not json", "application/json"),
            "input_file": ("x.csv", b"a\n1\n", "text/csv"),
        })
        assert response.status_code == 500
        assert list(web._job_root.iterdir()) == []

    def test_everything_is_removed_when_the_server_stops(self):
        with TestClient(web.app, headers=BEARER) as running:           # runs startup and shutdown
            upload(running, "data.csv", b"patient_name\nAl Bo\n")
            root = web._job_root
            assert root.exists() and any(root.iterdir())
        assert not root.exists() and not web.jobs

    def test_temp_directories_are_private(self, client):
        job = upload(client, "data.csv", b"patient_name\nAl Bo\n")["job_id"]
        assert Path(web.jobs[job]["temp_dir"]).stat().st_mode & 0o077 == 0

    def test_upload_names_cannot_escape_the_job_directory(self, client):
        for name in ("../../evil.csv", "..\\..\\evil.csv", "/etc/evil.csv"):
            job = upload(client, name, b"patient_name\nAl Bo\n")["job_id"]
            input_path = Path(web.jobs[job]["input_path"])
            assert input_path.parent == Path(web.jobs[job]["temp_dir"])
            assert input_path.name == "evil.csv"

    def test_mapping_and_input_with_the_same_name_do_not_clash(self, client):
        csv = b"patient_name\nAl Bo\nCy Di\n"
        job = upload(client, "data.csv", csv)["job_id"]
        process(client, job, ["patient_name"])
        mapping = client.get(f"/api/download/{job}/mapping").content
        output = client.get(f"/api/download/{job}/output").content

        restored = restore_with_names(client, mapping, output, mapping_name="same.json", input_name="same.json.csv")
        assert restored == csv

    def test_mapping_files_are_owner_only(self, client):
        job = upload(client, "data.csv", b"patient_name\nAl Bo\n")["job_id"]
        process(client, job, ["patient_name"])
        assert Path(web.jobs[job]["mapping_path"]).stat().st_mode & 0o077 == 0


def restore_with_names(client, mapping, content, mapping_name, input_name):
    response = client.post("/api/restore", data={"is_text": "false"}, files={
        "mapping_file": (mapping_name, mapping, "application/json"),
        "input_file": (input_name, content, "text/csv"),
    })
    assert response.status_code == 200, response.text
    return client.get(f"/download/restore/{response.json()['job_id']}").content


class TestProcessing:
    def test_fake_and_restore_round_trip_with_json_column(self, client):
        n = 1500
        df = pl.DataFrame({
            "id": list(range(n)),
            "patient_name": [f"Person {i}" for i in range(n)],
            "payload": [json.dumps({"name": f"Member {i}", "n": i}) for i in range(n)],
        })
        job = upload(client, "data.csv", df.write_csv().encode())["job_id"]
        process(client, job, ["patient_name", "payload:name"])

        output = client.get(f"/api/download/{job}/output").content
        mapping = client.get(f"/api/download/{job}/mapping").content

        faked = pl.read_csv(io.BytesIO(output))
        assert faked["patient_name"].n_unique() == n
        assert len({json.loads(doc)["name"] for doc in faked["payload"]}) == n

        back = pl.read_csv(io.BytesIO(restore(client, mapping, output)))
        assert back["patient_name"].to_list() == df["patient_name"].to_list()
        assert [json.loads(doc) for doc in back["payload"]] == [json.loads(doc) for doc in df["payload"]]

    def test_nested_fields_ticked_in_the_ui_are_actually_processed(self, client):
        """They used to be detected and selectable, then silently left in the clear."""
        detected = upload(client, "data.ndjson", NESTED, "application/x-ndjson")
        paths = [d["path"] for d in detected["detections"]["default"]]
        assert paths == ["patient.patient_name", "patient.contacts[*].email"]

        process(client, detected["job_id"], paths)
        output = client.get(f"/api/download/{detected['job_id']}/output").content

        for original in (b"John Smith", b"Jane Doe", b"a@b.com", b"c@d.com"):
            assert original not in output
        assert json.loads(output.splitlines()[0])["id"] == 1

        mapping = client.get(f"/api/download/{detected['job_id']}/mapping").content
        restored = restore(client, mapping, output, "processed.ndjson")
        assert [json.loads(l) for l in restored.splitlines()] == [json.loads(l) for l in NESTED.splitlines()]

    def test_the_same_person_gets_one_fake_across_columns(self, client):
        csv = b"patient_name,member_name\nJohn Smith,John Smith\nAnn Lee,Zed Poe\n"
        job = upload(client, "data.csv", csv)["job_id"]
        process(client, job, ["patient_name", "member_name"])
        faked = pl.read_csv(io.BytesIO(client.get(f"/api/download/{job}/output").content))
        assert faked["patient_name"][0] == faked["member_name"][0]
        assert len(set(faked["patient_name"].to_list() + faked["member_name"].to_list())) == 3

    def test_selected_fields_without_text_are_reported(self, client):
        job = upload(client, "data.csv", b"phone,patient_name\n5551234,Al Bo\n5559876,Cy Di\n")["job_id"]
        result = process(client, job, ["phone", "patient_name"])
        assert result["skipped"] == ["phone"]

    def test_job_ids_are_unguessable(self, client):
        first = upload(client, "a.csv", b"patient_name\nAl\n")["job_id"]
        second = upload(client, "b.csv", b"patient_name\nAl\n")["job_id"]
        assert len({first, second}) == 2 and len(first) == 36


class TestRunServer:
    def test_prints_a_sign_in_link_with_a_generated_token(self, monkeypatch):
        import uvicorn

        started = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, host, port: started.update(host=host, port=port))
        monkeypatch.delenv("PII_FAKER_TOKEN", raising=False)
        messages = []

        web.run_server("127.0.0.1", 9100, announce=messages.append)

        assert started == {"host": "127.0.0.1", "port": 9100}
        assert f"http://127.0.0.1:9100/?token={web.app.state.auth.token}" in messages[0]
        assert not any("Warning" in m for m in messages)

    def test_a_supplied_token_is_used_but_never_printed(self, monkeypatch):
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        messages = []
        web.run_server(token="my-own-secret-token-123", announce=messages.append)

        assert web.app.state.auth.token == "my-own-secret-token-123"
        assert "my-own-secret-token-123" not in " ".join(messages)

    def test_token_can_come_from_the_environment(self, monkeypatch):
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        monkeypatch.setenv("PII_FAKER_TOKEN", "from-the-environment-123")
        web.run_server(announce=lambda m: None)
        assert web.app.state.auth.token == "from-the-environment-123"

    def test_warns_when_listening_beyond_localhost(self, monkeypatch):
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        monkeypatch.delenv("PII_FAKER_TOKEN", raising=False)
        messages = []
        web.run_server(host="0.0.0.0", announce=messages.append)
        assert any("plain HTTP" in m for m in messages)

    def test_retention_is_applied(self, monkeypatch):
        import uvicorn

        monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
        monkeypatch.delenv("PII_FAKER_TOKEN", raising=False)
        web.run_server(retention_minutes=5, announce=lambda m: None)
        assert web.app.state.retention_seconds == 300
