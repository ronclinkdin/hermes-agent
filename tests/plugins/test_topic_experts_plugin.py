"""Tests for the bundled ``topic-experts`` plugin (``plugins/topic-experts/``).

Covers, with an isolated temp ``HERMES_HOME`` and no network/subprocess:
* hook registration (``gateway_platform_event`` + ``pre_llm_call``)
* telegram session-id → ``(chat_id, thread_id)`` parsing
* the first-message safety net: unknown threads queue a temp job (deduped,
  guarded by chat allowlist and the ``first_message_bootstrap`` switch),
  known threads and other platforms are ignored
* ``_provision`` result shape + route/state writes with a stubbed
  profile-creator and scope-enricher (real SOUL/config/route code runs)
* ``rename_profile`` promoting a temp profile end to end in the temp home
* temp vs full greeting rendering
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from pathlib import Path

import pytest
import yaml

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


PLUGIN_DIR = Path(_repo) / "plugins" / "topic-experts"
CHAT = "-1001"
_counter = itertools.count()


def _load_plugin(monkeypatch, tmp_path, state=None, config_extra=None):
    """Import a fresh plugin module bound to a temp HERMES_HOME."""
    home = tmp_path / "home"
    (home / "profiles").mkdir(parents=True)
    (home / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n", encoding="utf-8")
    cfg = {"enabled": True, "chat_ids": [CHAT], "first_message_bootstrap": True,
           "model": "m", "provider": "p", "base_url": "http://x/v1"}
    cfg.update(config_extra or {})
    (home / "topic-experts.json").write_text(json.dumps(cfg), encoding="utf-8")
    (home / "topic-experts-state.json").write_text(
        json.dumps(state or {"topics": {}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    name = f"topic_experts_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert str(mod.HERMES_HOME) == str(home)
    mod._ensure_worker = lambda: True  # never start the real worker in tests
    return mod, home


def _load_provisioner():
    name = f"provisioner_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "provisioner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _turn(mod, **kw):
    base = dict(user_message="hello?", session_id="s",
                platform="telegram", is_first_turn=True)
    base.update(kw)
    return mod.pre_llm_call(**base)


# ── registration ─────────────────────────────────────────────────────────


def test_registers_both_hooks(monkeypatch, tmp_path):
    mod, _ = _load_plugin(monkeypatch, tmp_path)
    hooked = []
    mod.register(type("Ctx", (), {
        "register_hook": lambda self, n, cb: hooked.append(n)})())
    assert hooked == ["gateway_platform_event", "pre_llm_call"]


# ── session parsing ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("session_id", "want"), [
    ("agent:main:telegram:group:-1001:8259", ("-1001", "8259")),
    ("agent:main:telegram:group:-1001:1", ("", "")),  # General is not a topic
    ("agent:main:telegram:dm:-1001", ("", "")),  # no thread segment
    ("cli-session-abc", ("", "")),
    ("", ("", "")),
])
def test_parse_telegram_thread(monkeypatch, tmp_path, session_id, want):
    mod, _ = _load_plugin(monkeypatch, tmp_path)
    assert mod._parse_telegram_thread(session_id) == want


# ── first-message safety net ─────────────────────────────────────────────


def test_unknown_thread_queues_temp_job(monkeypatch, tmp_path):
    mod, _ = _load_plugin(monkeypatch, tmp_path)
    assert _turn(mod, session_id=f"agent:main:telegram:group:{CHAT}:555") is None
    assert len(mod._queue) == 1
    job = mod._queue[0]
    assert job["temp"] is True and job["thread_id"] == "555"
    assert job["name"] == "topic-555"


def test_duplicate_turns_dedupe(monkeypatch, tmp_path):
    mod, _ = _load_plugin(monkeypatch, tmp_path)
    sid = f"agent:main:telegram:group:{CHAT}:555"
    _turn(mod, session_id=sid)
    _turn(mod, session_id=sid)
    assert len(mod._queue) == 1


def test_known_thread_is_ignored(monkeypatch, tmp_path):
    state = {"topics": {f"{CHAT}:42": {"name": "x", "profile": "exp-x",
                                       "error": None, "temp": False}}}
    mod, _ = _load_plugin(monkeypatch, tmp_path, state=state)
    _turn(mod, session_id=f"agent:main:telegram:group:{CHAT}:42")
    assert mod._queue == []


def test_unmonitored_chat_is_ignored(monkeypatch, tmp_path):
    mod, _ = _load_plugin(monkeypatch, tmp_path)
    _turn(mod, session_id="agent:main:telegram:group:-9999:555")
    assert mod._queue == []


def test_other_platforms_are_ignored(monkeypatch, tmp_path):
    mod, _ = _load_plugin(monkeypatch, tmp_path)
    _turn(mod, session_id="discord-123", platform="discord")
    assert mod._queue == []


def test_switch_off_disables_bootstrap(monkeypatch, tmp_path):
    mod, home = _load_plugin(monkeypatch, tmp_path, config_extra={"first_message_bootstrap": False})
    mod._cfg_cache["at"] = 0  # expire the TTL cache so the flag re-reads
    _turn(mod, session_id=f"agent:main:telegram:group:{CHAT}:555")
    assert mod._queue == []


# ── platform events: temp promotion ──────────────────────────────────────


def test_edit_event_promotes_temp_to_rename_job(monkeypatch, tmp_path):
    state = {"topics": {f"{CHAT}:555": {"name": "topic-555", "profile": "exp-topic-555",
                                        "error": None, "temp": True}}}
    mod, _ = _load_plugin(monkeypatch, tmp_path, state=state)
    mod._on_platform_event(platform="telegram", event_type="forum_topic_edited",
                           payload={"chat_id": CHAT, "thread_id": "555", "name": "CRM"})
    assert len(mod._queue) == 1
    job = mod._queue[0]
    assert job["op"] == "rename" and job["rename_from"] == "exp-topic-555"


def test_edit_event_keeps_real_profile(monkeypatch, tmp_path):
    state = {"topics": {f"{CHAT}:42": {"name": "PR", "profile": "exp-pr",
                                       "error": None, "temp": False}}}
    mod, _ = _load_plugin(monkeypatch, tmp_path, state=state)
    mod._on_platform_event(platform="telegram", event_type="forum_topic_edited",
                           payload={"chat_id": CHAT, "thread_id": "42", "name": "PR v2"})
    assert mod._queue == []


# ── provisioning with stubbed slow steps ─────────────────────────────────


class _StubTP:
    """Stand-in for the provisioner: real route/config/SOUL code runs."""
    real = None

    def create_profile(self, profile, role):
        (self.home / "profiles" / profile).mkdir(parents=True, exist_ok=True)

    def copy_env_allowlist(self, home):
        return []

    def llm_enrich(self, name):
        return f"- Owns {name} end to end\n- Measures everything\n"

    def __getattr__(self, item):
        return getattr(self.real, item)


def _stub_tp(monkeypatch, mod, home):
    tp = _load_provisioner()
    stub = _StubTP()
    stub.real = tp
    stub.home = home
    monkeypatch.setattr(mod, "_load_topic_profile_module", lambda: stub)
    return stub


def test_provision_writes_route_state_and_soul(monkeypatch, tmp_path):
    mod, home = _load_plugin(monkeypatch, tmp_path)
    _stub_tp(monkeypatch, mod, home)
    res = mod._provision({"chat_id": CHAT, "thread_id": "555", "name": "CRM"}, mod._cfg())
    assert res["profile"] == "exp-crm"
    assert "Owns CRM" in res["scope"]
    assert res["temp"] is False
    soul = (home / "profiles" / "exp-crm" / "SOUL.md").read_text(encoding="utf-8")
    assert "## Onboarding" in soul
    routes = yaml.safe_load((home / "config.yaml").read_text())["gateway"]["profile_routes"]
    assert routes[0]["profile"] == "exp-crm"
    mod._record_state({"chat_id": CHAT, "thread_id": "555", "name": "CRM"}, res, None)
    saved = json.loads((home / "topic-experts-state.json").read_text())["topics"][f"{CHAT}:555"]
    assert saved["profile"] == "exp-crm" and saved["temp"] is False


def test_provision_falls_back_when_enrich_fails(monkeypatch, tmp_path):
    mod, home = _load_plugin(monkeypatch, tmp_path)
    stub = _stub_tp(monkeypatch, mod, home)

    def boom(name):
        raise RuntimeError("router down")
    monkeypatch.setattr(stub, "llm_enrich", boom)
    res = mod._provision({"chat_id": CHAT, "thread_id": "555", "name": "CRM"}, mod._cfg())
    assert res["scope"].startswith("Own CRM work")


def test_rename_promotes_temp_profile(monkeypatch, tmp_path):
    mod, home = _load_plugin(monkeypatch, tmp_path)
    stub = _stub_tp(monkeypatch, mod, home)
    mod._provision({"chat_id": CHAT, "thread_id": "555", "name": "topic-555", "temp": True},
                   mod._cfg())
    res = mod._rename({"chat_id": CHAT, "thread_id": "555", "name": "CRM",
                       "rename_from": "exp-topic-555"}, mod._cfg())
    assert res == {**res, "temp": False}
    assert res["profile"] == "exp-crm"
    assert not (home / "profiles" / "exp-topic-555").exists()
    routes = yaml.safe_load((home / "config.yaml").read_text())["gateway"]["profile_routes"]
    assert routes[0]["profile"] == "exp-crm"


# ── greetings ────────────────────────────────────────────────────────────


def test_full_greeting_carries_scope_points():
    tp = _load_provisioner()
    text = tp.greeting_text("CRM", "exp-crm", "- Owns pipelines\n- Measures churn\n")
    assert "What I understand" in text
    assert "Owns pipelines" in text
    assert "1. What is your goal" in text


def test_temp_greeting_asks_for_the_name():
    tp = _load_provisioner()
    text = tp.greeting_text("topic-555", "exp-topic-555", "", temp=True)
    assert "temporary expert" in text
    assert "what is this topic's name" in text
