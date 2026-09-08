import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile_seven", MODULE / "profile_seven.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class Connections:
    def __init__(self, settings=None):
        self.apps = ["google-mail", "google-calendar", "google-drive", "fathom"]
        self.duplicate = False
        self.different_email = False

    def close(self):
        pass

    def _get(self, path, params=None):
        if path == "/connections":
            app = params["app"]
            record = {"app": app, "status": "ACTIVE", "connection_id": "bound-" + app}
            return {"connections": [record, record] if self.duplicate else [record]}
        app = path.removeprefix("/connections/bound-")
        return {"app": app, "status": "ACTIVE", "metadata": {"email": "other@example.com" if self.different_email and app == "google-mail" else "owner@example.com"}}


def test_binding_checks_uniqueness_and_identity():
    client = Connections()
    settings = profile.bound_settings(client)
    assert len(settings["connections"]) == 4
    assert "owner@example.com" not in json.dumps(settings)
    assert settings["telegram_archive_enabled"] is False
    client.duplicate = True
    with pytest.raises(ValueError, match="ambiguous"):
        profile.bound_settings(client)
    client.duplicate = False
    client.different_email = True
    with pytest.raises(ValueError, match="do_not_match"):
        profile.bound_settings(client)


def test_replace_preserves_unrelated_profile_blocks():
    text = "AI_FIXER_KEEP\n" + profile.SOUL_START + "\nold\n" + profile.SOUL_END + "\nOTHER_KEEP\n"
    result = profile.replace_block(text, profile.SOUL_START, profile.SOUL_END, profile.SOUL)
    assert result.startswith("AI_FIXER_KEEP\n") and result.endswith("\nOTHER_KEEP\n")
    with pytest.raises(ValueError, match="marker"):
        profile.replace_block("no marker", profile.SOUL_START, profile.SOUL_END, "new")


def sample_jobs():
    return [
        {"id": "morning", "name": "Утренний фокус Павла", "enabled": True, "origin": {"chat_id": "1"}, "schedule": {"expr": "0 9 * * *"}, "model": "preserve-model", "context_from": ["self"]},
        {"id": "evening", "name": "Вечерний обзор и план Павла", "enabled": True, "origin": {"chat_id": "1"}, "schedule": {"expr": "30 20 * * *"}},
        {"id": "watcher", "name": "Fathom", "no_agent": True, "enabled": True, "state": "scheduled"},
    ]


def test_update_only_existing_jobs_preserve_schedule_model_continuity():
    jobs = sample_jobs()
    before = copy.deepcopy(jobs)
    updates = profile.job_updates(jobs, Path("/profile"), "1")
    assert jobs == before and set(updates) == {"morning", "evening"}
    assert not {"schedule", "model", "context_from", "enabled"}.intersection(updates["morning"])
    assert updates["morning"]["enabled_toolsets"] == ["focus_assistant", "skills"]
    jobs[0]["enabled"] = False
    with pytest.raises(ValueError, match="no_implicit_resume"):
        profile.job_updates(jobs, Path("/profile"), "1")


def test_upgrade_backup_and_config_preservation(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    (home / "plugins/focus_assistant").mkdir(parents=True)
    (home / "focus").mkdir()
    (home / "cron").mkdir()
    (home / "runtime").mkdir()
    (home / "runtime/active_sessions.json").write_text('{"entries": []}')
    (home / "config.yaml").write_text("model:\n  context_length: 500000\nplugins:\n  enabled: [focus_assistant, ai_fixer]\nplatforms:\n  telegram:\n    home_channel:\n      chat_id: '1'\n")
    (home / "plugins/focus_assistant/__init__.py").write_text("# previous\n")
    (home / "SOUL.md").write_text("KEEP\n" + profile.SOUL_START + "old" + profile.SOUL_END)
    (home / "focus/AGENTS.md").write_text(profile.AGENTS_START + "old" + profile.AGENTS_END)
    jobs = sample_jobs()
    (home / "cron/jobs.json").write_text(json.dumps(jobs))
    monkeypatch.setitem(sys.modules, "cron.jobs", SimpleNamespace(list_jobs=lambda **_: jobs, update_job=lambda jid, fields: next(j for j in jobs if j["id"] == jid)))
    monkeypatch.setattr(profile, "load_plugin", lambda: SimpleNamespace(workspace=SimpleNamespace(Workspace=Connections)))
    config = profile.digest(home / "config.yaml")
    plugin = profile.digest(home / "plugins/focus_assistant/__init__.py")
    result = profile.upgrade(home, config, plugin, "1", apply=True)
    assert result["ok"] and profile.digest(home / "config.yaml") == config
    assert (home / "SOUL.md").read_text().startswith("KEEP\n")
    assert (home / "skills/productivity/assistant-workflows/SKILL.md").is_file()
    assert (Path(result["backup"]) / "plugins/focus_assistant/__init__.py").read_text() == "# previous\n"
    assert (home / "focus/assistant-settings.json").stat().st_mode & 0o777 == 0o600


def test_skill_manifest_names_match_registration():
    import yaml
    frontmatter = (MODULE / "skill/SKILL.md").read_text().split("---")[1]
    data = yaml.safe_load(frontmatter)
    assert data["platforms"] == ["linux"]
    assert len(data["metadata"]["hermes"]["requires_tools"]) == 8
    assert len(list((MODULE / "skill/references").glob("*.md"))) == 8
    assert "08-telegram-style.md" in (MODULE / "skill/SKILL.md").read_text()
