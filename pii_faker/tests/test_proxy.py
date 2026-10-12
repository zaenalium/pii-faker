"""Phase 5 proxy tests: scrubber + passthrough restore (mocked upstream)."""

import httpx
import pytest

from pii_faker.core.scrub import scrub_text
from pii_faker.proxy import _restore_response, _scrub_messages, create_proxy_app


def test_scrub_email_phone_nik_card():
    r = scrub_text("mail budi@x.com tel +62-812-3456-7890 nik 3174050101900001 card 4111111111111111")
    assert r.scrubbed == "mail <EMAIL_1> tel <PHONE_2> nik <NIK_3> card <CARD_4>"
    assert r.mapping["budi@x.com"] == "<EMAIL_1>"


def test_scrub_reuses_known_tokens():
    r1 = scrub_text("budi@x.com here")
    r2 = scrub_text("budi@x.com again", known_originals=r1.mapping, start_index=50)
    assert r2.mapping == {} and "<EMAIL_1>" in r2.scrubbed


def test_scrub_leaves_prose_names_alone():
    r = scrub_text("Alice Smith went to Jakarta")
    assert r.scrubbed == "Alice Smith went to Jakarta" and r.mapping == {}


def test_restore_response_choices():
    mapping = {"budi@x.com": "<EMAIL_1>"}
    payload = {"choices": [{"message": {"content": "wrote to <EMAIL_1> ok"}}]}
    out = _restore_response(payload, mapping)
    assert out["choices"][0]["message"]["content"] == "wrote to budi@x.com ok"


def test_scrub_messages_content_parts():
    body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "mail budi@x.com"}]}]}
    known: dict = {}
    out = _scrub_messages(body, known, 1)
    assert out["messages"][0]["content"][0]["text"] == "mail <EMAIL_1>"
    assert known["budi@x.com"] == "<EMAIL_1>"


@pytest.mark.asyncio
async def test_proxy_roundtrip_mocked(monkeypatch):
    import json as json_module
    import pii_faker.proxy as proxy_mod

    proxy_mod._sessions.clear()
    app = proxy_mod.create_proxy_app("https://up.example")

    upstream_body = {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, **k):
            upstream_body.update(json or {})
            assert "budi@x.com" not in json_module.dumps((json or {}).get("messages", []))
            class R:
                status_code = 200

                def json(self):
                    return {"choices": [{"message": {"content": "noted <EMAIL_1>"}}]}

            return R()

    monkeypatch.setattr(proxy_mod.httpx, "AsyncClient", FakeClient)

    body = json_module.dumps({"messages": [{"role": "user", "content": "mail budi@x.com"}]}).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [[b"x-pii-session", b"s1"], [b"content-type", b"application/json"]],
        "query_string": b"",
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    messages: list = []

    async def receive():
        if not messages:
            messages.append(1)
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    resp_status: list = []
    resp_body: list = []

    async def send(msg):
        if msg["type"] == "http.response.start":
            resp_status.append(msg["status"])
        elif msg["type"] == "http.response.body":
            resp_body.append(msg.get("body", b""))

    await app(scope, receive, send)
    assert resp_status == [200]
    payload = json_module.loads(b"".join(resp_body))
    assert payload["choices"][0]["message"]["content"] == "noted budi@x.com"
