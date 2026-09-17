"""Tests for Telegram forum-topic lifecycle → ``gateway_platform_event``.

Covers ``TelegramAdapter._normalize_forum_topic_event`` and the matching
``_source_for_platform_event_auth`` branch:
* ``forum_topic_created`` / ``forum_topic_edited`` service messages map to a
  stable ``{platform, event_type, payload}`` envelope carrying the topic name
* closed/reopened topics emit their event type with ``name=None``
* plain messages, missing ids, and non-string names yield ``None``
* auth falls back to the chat identity when the service message has no author
  (``from_user`` is absent for some topic-lifecycle updates); updates with no
  usable identity fail closed with ``ValueError``
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform


_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _adapter() -> TelegramAdapter:
    a = object.__new__(TelegramAdapter)
    a.platform = Platform.TELEGRAM
    a.config = SimpleNamespace(extra={"allow_from": ["*"]})
    return a


def _topic_message(thread_id=42, chat_id=-100123, name="media buying",
                   kind="created", from_user_id=7):
    created = SimpleNamespace(name=name) if kind == "created" else None
    edited = SimpleNamespace(name=name) if kind == "edited" else None
    return SimpleNamespace(
        forum_topic_created=created,
        forum_topic_edited=edited,
        forum_topic_closed=SimpleNamespace() if kind == "closed" else None,
        forum_topic_reopened=SimpleNamespace() if kind == "reopened" else None,
        chat=SimpleNamespace(id=chat_id, is_forum=True, title="group"),
        message_thread_id=thread_id,
        from_user=(SimpleNamespace(id=from_user_id, username="ron", is_bot=False)
                   if from_user_id is not None else None),
    )


def _update(message):
    return SimpleNamespace(message_reaction=None, edited_message=None, message=message)


# ── _normalize_forum_topic_event ──────────────────────────────────────────


def test_created_maps_name_thread_and_chat():
    event = _adapter()._normalize_platform_event(
        _update(_topic_message(name="media buying")))
    assert event == {
        "platform": "telegram",
        "event_type": "forum_topic_created",
        "payload": {
            "chat_id": "-100123",
            "thread_id": "42",
            "name": "media buying",
            "icon_custom_emoji_id": None,
            "creator_user_id": "7",
            "is_forum": True,
        },
    }


def test_edited_maps_to_forum_topic_edited():
    event = _adapter()._normalize_platform_event(
        _update(_topic_message(kind="edited", name="new name")))
    assert event["event_type"] == "forum_topic_edited"
    assert event["payload"]["name"] == "new name"
    assert event["payload"]["thread_id"] == "42"


def test_closed_and_reopened_emit_type_with_null_name():
    for kind, want in (("closed", "forum_topic_closed"),
                       ("reopened", "forum_topic_reopened")):
        event = _adapter()._normalize_platform_event(_update(_topic_message(kind=kind)))
        assert event["event_type"] == want
        assert event["payload"]["name"] is None


def test_plain_text_message_yields_none():
    message = SimpleNamespace(
        forum_topic_created=None, forum_topic_edited=None,
        forum_topic_closed=None, forum_topic_reopened=None,
        chat=SimpleNamespace(id=-100123, is_forum=True),
        message_thread_id=42, from_user=None, text="hello")
    assert _adapter()._normalize_platform_event(_update(message)) is None


def test_missing_thread_id_yields_none():
    assert _adapter()._normalize_platform_event(
        _update(_topic_message(thread_id=None))) is None


def test_non_string_name_yields_null_name():
    event = _adapter()._normalize_platform_event(
        _update(_topic_message(name=12345)))
    assert event["event_type"] == "forum_topic_created"
    assert event["payload"]["name"] is None


def test_int_ids_are_coerced_to_strings():
    event = _adapter()._normalize_platform_event(
        _update(_topic_message(thread_id=99, chat_id=-100123)))
    assert event["payload"] == {
        "chat_id": "-100123",
        "thread_id": "99",
        "name": "media buying",
        "icon_custom_emoji_id": None,
        "creator_user_id": "7",
        "is_forum": True,
    }


# ── _source_for_platform_event_auth ───────────────────────────────────────


def test_auth_uses_author_when_present():
    source = _adapter()._source_for_platform_event_auth(
        _update(_topic_message(from_user_id=7)))
    assert source.user_id == "7"
    assert source.chat_id == "-100123"


def test_auth_falls_back_to_chat_identity_without_author():
    source = _adapter()._source_for_platform_event_auth(
        _update(_topic_message(from_user_id=None)))
    assert source.user_id == "-100123"
    assert source.chat_id == "-100123"


def test_auth_fails_closed_without_any_identity():
    message = _topic_message(from_user_id=None)
    message.chat = SimpleNamespace(id="", is_forum=True, title="")
    with pytest.raises(ValueError):
        _adapter()._source_for_platform_event_auth(_update(message))
