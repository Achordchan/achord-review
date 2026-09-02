"""Tests for the dashboard config engine (pr_agent/dashboard/config_engine.py)."""

import os

import pytest

from pr_agent.dashboard.config_engine import ConfigEngine, mask_secret


@pytest.fixture()
def engine(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[config]\nmodel = "openai/gpt-old"\nreasoning_effort = "high"\n'
        'ai_timeout = 600\n\n[openai]\nkey = "sk-secret-key-1234567890"\n'
        'api_base = "https://relay.example/v1"\n\n'
        '[pr_reviewer]\nnum_max_findings = 10\nverdict_blocking_severities = ["P0"]\n\n'
        '[ignore]\nglob = ["dist/**"]\n\n[github]\napp_id = "123"\n',
        encoding="utf-8")
    return ConfigEngine(config_path=str(config_path))


class TestRead:
    def test_masks_key(self, engine):
        data = engine.read()
        assert data["available"] is True
        assert data["values"]["key"] == mask_secret("sk-secret-key-1234567890")
        assert "sk-secret-key" not in data["values"]["key"]
        assert data["values"]["model"] == "openai/gpt-old"

    def test_missing_file(self, tmp_path):
        engine = ConfigEngine(config_path=str(tmp_path / "nope.toml"))
        assert engine.read()["available"] is False

    def test_write_preserves_comments_and_ordering(self, engine):
        comment_line = '# reasoning_effort = "high"  # GPT-5 family knob'
        with open(engine.config_path, "a", encoding="utf-8") as f:
            f.write(f"\n{comment_line}\n")
        ok, _ = engine.write({"model": "openai/changed"})
        assert ok
        text = open(engine.config_path, encoding="utf-8").read()
        assert comment_line in text
        assert 'model = "openai/changed"' in text
        # section ordering preserved: [config] content still precedes [openai]
        assert text.index("[config]") < text.index("[openai]")


class TestWrite:
    def test_write_updates_fields(self, engine):
        ok, errors = engine.write({
            "model": "openai/gpt-new",
            "ai_timeout": 900,
            "verdict_blocking_severities": ["P0", "P1"],
            "ignore_glob": ["dist/**", "node_modules/**"],
        })
        assert ok, errors
        values = engine.read()["values"]
        assert values["model"] == "openai/gpt-new"
        assert values["ai_timeout"] == 900
        assert values["verdict_blocking_severities"] == ["P0", "P1"]
        assert values["ignore_glob"] == ["dist/**", "node_modules/**"]

    def test_empty_key_keeps_secret(self, engine):
        before = engine.read()["values"]["key"]
        ok, _ = engine.write({"key": ""})
        assert ok
        assert engine.read()["values"]["key"] == before

    def test_empty_key_hot_reload_keeps_running_key(self, engine, monkeypatch):
        """An empty secret must not clear the in-process key during hot reload."""
        captured = {}

        class FakeSettings:
            def set(self, dotted, value):
                captured[dotted] = value

        import pr_agent.config_loader as cl
        monkeypatch.setattr(cl, "global_settings", FakeSettings(), raising=False)

        ok, _ = engine.write({"key": ""})
        assert ok
        # _hot_reload ran during write; openai.key must NOT have been set to ""
        assert captured.get("openai.key", "unset") != ""

    def test_validation_rejects_bad_values(self, engine):
        ok, errors = engine.write({"ai_timeout": "not-a-number"})
        assert not ok and errors
        ok, errors = engine.write({"ai_timeout": 5})
        assert not ok and "between" in errors[0]
        ok, errors = engine.write({"verdict_blocking_severities": ["P9"]})
        assert not ok
        ok, errors = engine.write({"unknown_field": 1})
        assert not ok

    def test_backup_created(self, engine):
        import time as _time
        engine.write({"model": "openai/second"})
        # two rapid saves inside one second must produce distinct backups
        engine.write({"model": "openai/third"})
        engine.write({"model": "openai/fourth"})
        _time.sleep(0.002)
        engine.write({"model": "openai/fifth"})
        _time.sleep(0.002)
        engine.write({"model": "openai/sixth"})
        engine.write({"model": "openai/seventh"})
        # MAX_BACKUPS enforced and no collision-collapsed files
        backups = [f for f in os.listdir(os.path.dirname(engine.config_path))
                   if f.startswith("config.toml.bak.")]
        assert len(backups) == len(set(backups))  # all distinct names
        assert len(backups) <= 5

    def test_unrelated_sections_preserved(self, engine):
        engine.write({"model": "openai/changed"})
        import tomllib
        with open(engine.config_path, "rb") as f:
            raw = tomllib.load(f)
        assert raw["github"]["app_id"] == "123"

    def test_missing_file_fails_gracefully(self, tmp_path):
        engine = ConfigEngine(config_path=str(tmp_path / "nope.toml"))
        ok, errors = engine.write({"model": "x"})
        assert not ok and errors


class TestMaskSecret:
    def test_masks(self):
        assert mask_secret("") == ""
        assert mask_secret("short") == "****"
        masked = mask_secret("sk-1234567890abcdef")
        assert masked.startswith("sk-12") and masked.endswith("cdef") and "****" in masked
