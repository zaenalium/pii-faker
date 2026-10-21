"""Web UI: authentication, job lifecycle, desktop interplay, and the detect -> process -> restore flow."""

import glob
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pii_faker.web import app as web
from pii_faker.web.auth import SESSION_COOKIE, Authenticator

TOKEN = "test-token-0123456789abcdef"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_server():
    """A known token, and no jobs or temp files carried between tests."""
    web.app.state.auth = Authenticator(TOKEN)
    web.app.state.retention_seconds = 3600
    web.app.state.allow_local_paths = False
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


def upload(client, name, content):
    response = client.post("/api/detect", files={"file": (name, content)})
    assert response.status_code == 200, response.text
    return response.json()


def process(client, job, columns, **extra):
    data = {"job_id": job, "selected_columns": ",".join(columns), "mode": "fake", "seed": "1", **extra}
    response = client.post("/api/process", data=data)
    assert response.status_code == 200, response.text
    return response.json()


def read_output(job):
    return Path(web.jobs[job]["output_path"]).read_text()


NESTED = "\n".join(json.dumps(row) for row in [
    {"patient": {"patient_name": "John Smith", "contacts": [{"email": "a@b.com"}, {"email": "c@d.com"}]}, "id": 1},
    {"patient": {"patient_name": "Jane Doe", "contacts": [{"email": "e@f.com"}]}, "id": 2},
]).encode()
CSV = b"patient_name,member_name,note\nJohn Smith,John Smith,keep\nAnn Lee,Zed Poe,keep\n"


class TestAuthentication:
    @pytest.mark.parametrize("method, path", [
        ("post", "/api/detect"),
        ("post", "/api/process"),
        ("post", "/api/restore"),
        ("post", "/api/restore-text"),
        ("post", "/api/delete-mapping"),
        ("post", "/api/detect-path"),
        ("get", "/api/presets"),
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
        assert anonymous.get("/api/presets", headers={"Authorization": "Bearer wrong"}).status_code == 401

    def test_bearer_token_is_accepted(self, client):
        assert client.get("/api/presets").status_code == 200

    def test_home_shows_sign_in_not_the_app(self, anonymous):
        response = anonymous.get("/")
        assert response.status_code == 401
        assert 'name="token"' in response.text and "pii-session-token" not in response.text

    def test_token_link_signs_in_and_redirects_without_the_token(self, anonymous):
        response = anonymous.get(f"/?token={TOKEN}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        cookie = response.headers["set-cookie"].lower()
        assert SESSION_COOKIE in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert TOKEN not in response.headers["set-cookie"]               # the cookie is a session id

        home = anonymous.get("/")                                          # the client kept the cookie
        assert home.status_code == 200 and 'name="token"' not in home.text
        assert anonymous.get("/api/presets").status_code == 200

    def test_wrong_token_link_does_not_sign_in(self, anonymous):
        response = anonymous.get("/?token=nope", follow_redirects=False)
        assert response.status_code == 401 and "set-cookie" not in response.headers
        assert "not valid" in response.text

    def test_sign_in_form(self, anonymous):
        bad = anonymous.post("/login", data={"token": "nope"}, follow_redirects=False)
        assert bad.status_code == 401 and "set-cookie" not in bad.headers
        assert anonymous.post("/login", data={"token": TOKEN}, follow_redirects=False).status_code == 303
        assert anonymous.get("/api/presets").status_code == 200

    def test_sign_out_ends_the_session(self, anonymous):
        anonymous.post("/login", data={"token": TOKEN})
        assert anonymous.post("/logout").status_code == 200
        assert anonymous.get("/api/presets").status_code == 401

    def test_expired_sessions_are_rejected(self, anonymous):
        anonymous.post("/login", data={"token": TOKEN})
        web.app.state.auth._sessions = {sid: 0 for sid in web.app.state.auth._sessions}
        assert anonymous.get("/api/presets").status_code == 401

    def test_forged_session_cookie_is_rejected(self, anonymous):
        anonymous.cookies.set(SESSION_COOKIE, "forged")
        assert anonymous.get("/api/presets").status_code == 401

    def test_responses_are_not_cached(self, client):
        response = client.get("/api/presets")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_short_tokens_are_refused(self):
        with pytest.raises(ValueError, match="at least"):
            Authenticator("short")

    def test_generated_tokens_are_strong_and_distinct(self):
        first, second = Authenticator(), Authenticator()
        assert first.generated and len(first.token) >= 32 and first.token != second.token

    def test_a_401_sends_the_page_back_to_sign_in(self, anonymous):
        # apiFetch reloads on 401, which lands on the sign-in page.
        text = (Path(web.__file__).parent / "templates" / "index.html").read_text()
        assert "response.status === 401" in text and "window.location.reload()" in text


class TestDesktopInterplay:
    """The desktop launcher keeps its own header token; both must work together.
    Run in a subprocess: create_app() adds middleware to the shared app for good."""

    def test_the_window_signs_in_with_its_session_token(self):
        script = r'''
import json, warnings; warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from pii_faker import desktop
token = "desktop-session-token-0123456789abcdef"
app = desktop.create_app(token)
c = TestClient(app)
out = {}
out["anonymous_api"] = c.get("/api/presets").status_code
home = c.get("/?token=" + token, follow_redirects=True)
out["home"] = home.status_code
out["page_carries_token"] = f'content="{token}"' in home.text
out["cookie_only"] = c.get("/api/presets").status_code                       # header token missing
out["cookie_and_header"] = c.get("/api/presets", headers={desktop.SESSION_HEADER: token}).status_code
out["paths_allowed"] = c.post("/api/detect-path", json={"path": "/no/such/file.csv"},
                              headers={desktop.SESSION_HEADER: token}).status_code
out["no_idle_expiry"] = app.state.retention_seconds
print(json.dumps(out))
'''
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=REPO, timeout=60)
        assert done.returncode == 0, done.stderr
        result = json.loads(done.stdout.strip().splitlines()[-1])
        assert result == {
            "anonymous_api": 401, "home": 200, "page_carries_token": True,
            "cookie_only": 401,                    # the desktop's own header check still applies
            "cookie_and_header": 200,
            "paths_allowed": 400,                  # reached validation: "Path does not exist"
            "no_idle_expiry": 0,
        }


class TestLocalPathsAreDesktopOnly:
    """detect-path / process-path read and write whatever path the client names."""

    def test_refused_for_the_browser_server_even_with_the_token(self, client, tmp_path):
        source = tmp_path / "data.csv"
        source.write_bytes(CSV)
        response = client.post("/api/detect-path", json={"path": str(source)})
        assert response.status_code == 403 and "desktop app" in response.json()["detail"]
        assert client.post("/api/process-path", json={"job_id": "x"}).status_code == 403

    def test_available_once_the_desktop_enables_them(self, client, tmp_path):
        web.app.state.allow_local_paths = True
        source = tmp_path / "data.csv"
        source.write_bytes(CSV)
        assert client.post("/api/detect-path", json={"path": str(source)}).status_code == 200


class TestJobLifecycle:
    def test_expired_jobs_and_their_files_are_deleted(self, client):
        job = upload(client, "data.csv", CSV)["job_id"]
        job_dir = Path(web.jobs[job]["temp_dir"])
        assert job_dir.exists()

        assert web.purge_expired_jobs(3600) == 0                                 # still fresh
        assert web.purge_expired_jobs(3600, now=time.time() + 3601) == 1
        assert not job_dir.exists() and job not in web.jobs
        assert client.get(f"/api/download/{job}/output").status_code == 404

    def test_activity_postpones_expiry(self, client):
        job = upload(client, "data.csv", CSV)["job_id"]
        web.jobs.touched[job] -= 3000
        client.get(f"/api/download/{job}/audit")                                 # any lookup counts
        assert web.purge_expired_jobs(3600, now=time.time() + 1000) == 0

    def test_a_job_can_be_deleted_on_demand(self, client):
        job = upload(client, "data.csv", CSV)["job_id"]
        job_dir = Path(web.jobs[job]["temp_dir"])
        assert client.delete(f"/api/jobs/{job}").status_code == 200
        assert not job_dir.exists()
        assert client.delete(f"/api/jobs/{job}").status_code == 404

    def test_path_based_jobs_forget_but_never_touch_the_users_file(self, client, tmp_path):
        web.app.state.allow_local_paths = True
        source = tmp_path / "data.csv"
        source.write_bytes(CSV)
        job = client.post("/api/detect-path", json={"path": str(source)}).json()["job_id"]
        assert web.purge_expired_jobs(1, now=time.time() + 10) == 1
        assert source.read_bytes() == CSV                                        # temp_dir is None for these

    def test_failed_uploads_leave_no_files_behind(self, client):
        """A failed detect left the uploaded file in the temp dir: the job isn't registered yet."""
        before = set(glob.glob(f"{tempfile.gettempdir()}/pii_faker_*"))
        response = client.post("/api/detect", files={"file": ("data.xyz", b"patient_name\nZELDA-CANARY\n")})
        assert response.status_code == 400 and not web.jobs
        assert set(glob.glob(f"{tempfile.gettempdir()}/pii_faker_*")) == before

    def test_everything_is_removed_when_the_server_stops(self):
        with TestClient(web.app, headers=BEARER) as running:                    # runs startup and shutdown
            job = upload(running, "data.csv", CSV)["job_id"]
            job_dir = Path(web.jobs[job]["temp_dir"])
            assert job_dir.exists()
        assert not job_dir.exists() and not web.jobs

    def test_temp_directories_are_private(self, client):
        job = upload(client, "data.csv", CSV)["job_id"]
        assert Path(web.jobs[job]["temp_dir"]).stat().st_mode & 0o077 == 0

    def test_upload_names_cannot_escape_the_job_directory(self, client):
        for name in ("../../evil.csv", "..\\..\\evil.csv", "/etc/evil.csv"):
            job = upload(client, name, CSV)["job_id"]
            input_path = Path(web.jobs[job]["input_path"])
            assert input_path.parent == Path(web.jobs[job]["temp_dir"])          # never outside the job dir
            assert ".." not in input_path.parent.parts
            if "\\" not in name:                                                # `\` is just a character on POSIX
                assert input_path.name == "evil.csv"

    def test_job_ids_are_unguessable(self, client):
        first, second = upload(client, "a.csv", CSV)["job_id"], upload(client, "b.csv", CSV)["job_id"]
        assert len({first, second}) == 2 and len(first) == 36


class TestProcessing:
    def test_nested_fields_ticked_in_the_ui_are_processed(self, client):
        """The UI offered `patient.contacts.email`; the run left every email in the clear."""
        detected = upload(client, "data.ndjson", NESTED)
        paths = [d["path"] for d in detected["detections"]["default"]]
        assert paths == ["patient.patient_name", "patient.contacts[*].email"]

        job = detected["job_id"]
        result = process(client, job, paths)
        output = read_output(job)

        for real in ("John Smith", "Jane Doe", "a@b.com", "c@d.com", "e@f.com"):
            assert real not in output
        assert not any("leak check" in w for w in result.get("warnings", []))
        assert json.loads(output.splitlines()[0])["id"] == 1

    def test_the_path_form_without_the_marker_works_too(self, client):
        job = upload(client, "data.ndjson", NESTED)["job_id"]
        process(client, job, ["patient.patient_name", "patient.contacts.email"])
        output = read_output(job)
        assert "a@b.com" not in output and "John Smith" not in output

    def test_restore_returns_the_original_rows(self, client):
        job = upload(client, "data.ndjson", NESTED)["job_id"]
        paths = [d["path"] for d in client.post("/api/detect", files={"file": ("d.ndjson", NESTED)})
                 .json()["detections"]["default"]]
        process(client, job, paths)
        record = web.jobs[job]
        mapping, output = Path(record["mapping_path"]).read_bytes(), Path(record["output_path"]).read_bytes()

        restored = client.post("/api/restore", data={"is_text": "false"}, files={
            "mapping_file": ("map.json", mapping), "input_file": ("processed.ndjson", output)})
        assert restored.status_code == 200, restored.text
        path = Path(web.jobs[restored.json()["job_id"]]["output_path"])
        assert [json.loads(l) for l in path.read_text().splitlines()] == [json.loads(l) for l in NESTED.decode().splitlines()]

    def test_the_same_person_gets_one_fake_across_columns(self, client):
        import polars as pl

        job = upload(client, "data.csv", CSV)["job_id"]
        process(client, job, ["patient_name", "member_name"])
        faked = pl.read_csv(web.jobs[job]["output_path"])
        assert faked["patient_name"][0] == faked["member_name"][0]
        assert len(set(faked["patient_name"].to_list() + faked["member_name"].to_list())) == 3
        assert faked["note"].to_list() == ["keep", "keep"]

    def test_mapping_files_are_owner_only(self, client):
        job = upload(client, "data.csv", CSV)["job_id"]
        process(client, job, ["patient_name"])
        assert Path(web.jobs[job]["mapping_path"]).stat().st_mode & 0o077 == 0


class TestRunServer:
    @pytest.fixture(autouse=True)
    def no_real_server(self, monkeypatch):
        import uvicorn
        self.started = {}
        monkeypatch.setattr(uvicorn, "run", lambda app, host, port: self.started.update(host=host, port=port))
        monkeypatch.delenv("PII_FAKER_TOKEN", raising=False)

    def test_prints_a_sign_in_link_with_a_generated_token(self):
        messages = []
        web.run_server("127.0.0.1", 9100, announce=messages.append)
        assert self.started == {"host": "127.0.0.1", "port": 9100}
        assert f"http://127.0.0.1:9100/?token={web.app.state.auth.token}" in messages[0]
        assert not any("Warning" in m for m in messages)

    def test_a_supplied_token_is_used_but_never_printed(self):
        messages = []
        web.run_server(token="my-own-secret-token-123", announce=messages.append)
        assert web.app.state.auth.token == "my-own-secret-token-123"
        assert "my-own-secret-token-123" not in " ".join(messages)

    def test_token_can_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("PII_FAKER_TOKEN", "from-the-environment-123")
        web.run_server(announce=lambda m: None)
        assert web.app.state.auth.token == "from-the-environment-123"

    def test_warns_when_listening_beyond_localhost(self):
        messages = []
        web.run_server(host="0.0.0.0", announce=messages.append)
        assert any("plain HTTP" in m for m in messages)

    def test_retention_is_applied(self):
        web.run_server(retention_minutes=5, announce=lambda m: None)
        assert web.app.state.retention_seconds == 300
