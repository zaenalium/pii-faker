"""Phase 3 tests: encrypted mappings, format-probe cleanup, PII-free audits, presets."""

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from pii_faker.core.config import PIIConfig
from pii_faker.core.mapper import PIIMapper
from pii_faker.core.pipeline import process_sheets
from pii_faker.core.presets import apply_preset, list_presets

cryptography = pytest.importorskip("cryptography", reason="pii-faker[enc] not installed")
from pii_faker.core import crypto


@pytest.fixture()
def tiny_mapping(tmp_path):
    m = PIIMapper(PIIConfig(seed=1))
    m.add_column_mapping("email", "contact", {"a@x.com": "b@y.net"})
    path = tmp_path / "mapping.json"
    m.save(str(path))
    return m, path


def test_encrypted_roundtrip_passphrase(tmp_path):
    m = PIIMapper(PIIConfig(seed=1))
    m.add_column_mapping("email", "contact", {"a@x.com": "b@y.net"})
    path = tmp_path / "enc.json"
    m.save(str(path), encrypt=True, passphrase="pw-test")
    env = json.loads(path.read_text())
    assert env.get("enc") == "aes-gcm"
    assert "a@x.com" not in path.read_text()
    loaded = PIIMapper.load(str(path), passphrase="pw-test")
    assert loaded.store.get_mapping("email") == {"a@x.com": "b@y.net"}
    with pytest.raises(Exception):
        PIIMapper.load(str(path), passphrase="wrong", use_keyring=False)


def test_plaintext_still_loads(tiny_mapping):
    _, path = tiny_mapping
    assert PIIMapper.load(str(path)).store.get_mapping("email") == {"a@x.com": "b@y.net"}


def test_cleanup_format_probe(tmp_path):
    m = PIIMapper(PIIConfig())
    ours = tmp_path / "run_2020.json"
    data = m.store.to_dict(session_id="s")
    data["created_at"] = (datetime.now() - timedelta(days=30)).isoformat()
    ours.write_text(json.dumps(data))
    os.utime(ours, (0, 0))
    alien = tmp_path / "mapping_other.json"
    alien.write_text(json.dumps({"tool": "other", "version": "9"}))
    os.utime(alien, (0, 0))
    deleted = m.cleanup_old_mappings(str(tmp_path), older_than_days=7)
    assert deleted == 1 and not ours.exists() and alien.exists()


def test_audit_is_pii_free(tmp_path):
    cfg = PIIConfig(seed=1)
    sheets = {"default": pl.DataFrame({"email": ["a@x.com", "b@y.org"]})}
    out = str(tmp_path / "mapping.json")
    _, _, rs = process_sheets(sheets, cfg, mapping_out=out)
    audit_path = tmp_path / "mapping_audit.json"
    assert audit_path.exists()
    blob = audit_path.read_text()
    assert "a@x.com" not in blob and "b@y.org" not in blob
    assert json.loads(blob)["tool"] == "pii-faker"


def test_preset_id_resolves():
    names = [p["name"] for p in list_presets()]
    assert "id" in names
    cfg = apply_preset(PIIConfig(), "id")
    assert cfg.locale == "id_ID" and cfg.preset == "id"
    assert "nik" in cfg.extra_pii_columns


def test_keyring_roundtrip(monkeypatch):
    store = {}

    class FakeKR:
        @staticmethod
        def get_password(s, a):
            return store.get((s, a))

        @staticmethod
        def set_password(s, a, v):
            store[(s, a)] = v

        @staticmethod
        def delete_password(s, a):
            del store[(s, a)]

    monkeypatch.setitem(__import__("sys").modules, "keyring", FakeKR)
    crypto.keyring_set(b"0" * 32)
    assert crypto.keyring_exists()
    assert crypto.keyring_delete()
    assert not crypto.keyring_exists()
