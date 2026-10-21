"""
Local LLM proxy (Phase 5, exploratory).

An OpenAI-compatible endpoint that scrubs PII-shaped spans out of outgoing
prompts, forwards the scrubbed request upstream, then restores originals
into the response. Runs on 127.0.0.1 only.

    pii-faker proxy --upstream https://api.openai.com --port 11434
    # then point any OpenAI client at http://127.0.0.1:11434/v1

Flow per request:
  1. Extract text from chat messages (string or content-part lists).
  2. scrub_text each message; same value → same token within a session
     (session mapping kept in memory, never written to disk).
  3. Forward the scrubbed body upstream with the client's Authorization
     header passed through untouched (the proxy never sees/stores keys —
     it just relays whatever the client sent).
  4. Restore tokens in every choice message / delta, streaming or not.

Limitations (documented, not hidden): deterministic detectors only, no NER
— names/addresses in prose pass through. Upstream must be OpenAI-compatible
(/v1/chat/completions). Session mappings live in memory only.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from pii_faker.core.restorer import _build_text_regex
from pii_faker.core.scrub import scrub_text

_sessions: Dict[str, Dict[str, str]] = {}


def _session_id(request: Request) -> str:
    sid = request.headers.get("X-PII-Session")
    if sid:
        return sid
    return "default"


def _mapping_for(sid: str) -> Dict[str, str]:
    return _sessions.setdefault(sid, {})


def create_proxy_app(upstream: str) -> FastAPI:
    app = FastAPI(title="PII Faker LLM Proxy", version="0.1.0")
    app.state.upstream = upstream.rstrip("/")

    @app.get("/v1/models")
    async def models():
        return JSONResponse({"object": "list", "data": []})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, authorization: Optional[str] = Header(None)):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
        sid = _session_id(request)
        known = _mapping_for(sid)
        start_index = max([1] + [
            int(tok.rsplit("_", 1)[-1].rstrip(">"))
            for tok in known.values()
            if isinstance(tok, str) and tok.rsplit("_", 1)[-1].rstrip(">").isdigit()
        ]) + (0 if not known else 1)

        scrubbed_body = _scrub_messages(body, known, start_index)

        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        stream = bool(body.get("stream"))

        try:
            if stream:
                return await _forward_stream(app.state.upstream, scrubbed_body, headers, known)
            return await _forward_once(app.state.upstream, scrubbed_body, headers, known)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    return app


def _scrub_messages(body: Dict[str, Any], known: Dict[str, str], start_index: int) -> Dict[str, Any]:
    """Return a copy of the request body with message text scrubbed in place."""
    import copy

    out = copy.deepcopy(body)
    counter = [start_index]
    for msg in out.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            res = scrub_text(content, known_originals=known, start_index=counter[0])
            msg["content"] = res.scrubbed
            known.update(res.mapping)
            counter[0] += len(res.mapping)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    res = scrub_text(part["text"], known_originals=known, start_index=counter[0])
                    part["text"] = res.scrubbed
                    known.update(res.mapping)
                    counter[0] += len(res.mapping)
    return out


def _restore_text(text: str, mapping: Dict[str, str]) -> str:
    if not mapping or not isinstance(text, str):
        return text
    reverse = {tok: orig for orig, tok in mapping.items()}
    patterns = [t for t in reverse if isinstance(t, str) and t]
    if not patterns:
        return text
    rx = _build_text_regex(patterns, False)
    return rx.sub(lambda m: str(reverse[m.group(0)]), text)


def _restore_response(payload: Dict[str, Any], mapping: Dict[str, str]) -> Dict[str, Any]:
    for choice in payload.get("choices", []) or []:
        msg = choice.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"] = _restore_text(msg["content"], mapping)
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            delta["content"] = _restore_text(delta["content"], mapping)
    return payload


async def _forward_once(upstream: str, body: Dict[str, Any], headers: Dict[str, str], mapping: Dict[str, str]):
    async with httpx.AsyncClient(timeout=300) as client:
        resp = await client.post(f"{upstream}/v1/chat/completions", json=body, headers=headers)
    try:
        payload = resp.json()
    except Exception:
        return JSONResponse(content={"error": "upstream returned non-JSON"}, status_code=502)
    if resp.status_code >= 400:
        return JSONResponse(content=payload, status_code=resp.status_code)
    return JSONResponse(_restore_response(payload, mapping))


async def _forward_stream(upstream: str, body: Dict[str, Any], headers: Dict[str, str], mapping: Dict[str, str]):
    async with httpx.AsyncClient(timeout=300) as client:
        req = client.build_request("POST", f"{upstream}/v1/chat/completions", json=body, headers=headers)
        resp = await client.send(req, stream=True)
        if resp.status_code >= 400:
            data = await resp.aread()
            try:
                payload = json.loads(data)
            except Exception:
                payload = {"error": data.decode()[:500]}
            return JSONResponse(content=payload, status_code=resp.status_code)

        async def gen():
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    stripped = line.strip()
                    if stripped.startswith("data:") and stripped[5:].strip() not in ("", "[DONE]"):
                        try:
                            payload = json.loads(stripped[5:])
                            payload = _restore_response(payload, mapping)
                            yield "data: " + json.dumps(payload) + "\n\n"
                            continue
                        except Exception:
                            pass
                    yield line + "\n"
            if buf:
                yield buf
            await resp.aclose()

        return StreamingResponse(gen(), media_type="text/event-stream")
