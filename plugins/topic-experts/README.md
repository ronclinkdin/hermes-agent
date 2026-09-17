# topic-experts

Each Telegram forum topic becomes its own Hermes expert profile: isolated memory,
skills, sessions, and model config — with zero manual topic → profile mapping.

## How it works

1. You create a forum topic (e.g. `media buying`). Telegram emits a
   `forum_topic_created` service message — the only source of a topic's name,
   since the Bot API has no read method for it.
2. The plugin captures `(chat_id, thread_id, name)`, builds profile
   `exp-media-buying` (SOUL + charter skill + matching skills + lean config),
   writes a `gateway.profile_routes` entry, and — after a quiet window — fires
   **one** graceful gateway restart for the whole batch (in-flight turns drain
   first, nothing is killed).
3. Every message in that thread now runs as the expert. The expert posts a
   topic-aware greeting (what it understood + 3 onboarding questions) as its
   first message.

## Safety nets

- **Missed creation event** (restart overlap, poll gap): the first user message
  in an unknown monitored thread bootstraps a *temporary* expert
  (`exp-topic-<thread>`) and asks for the topic's name. Rename the topic (or
  tell it the name and rename) and the temp expert is automatically promoted
  to the real one. Toggle: `first_message_bootstrap` (default `true`).
- **Rename of a known topic**: ignored (profile kept). **Rename of a temp
  topic**: promotes it. **Idle-gated restarts**: a restart never SIGTERMs a
  live agent turn — it defers until idle (bounded by `max_defer_seconds`).

## Setup

1. Enable the plugin (`plugins.enabled` must include `topic-experts`).
2. Copy `topic-experts.json.example` to `$HERMES_HOME/topic-experts.json` and
   set `chat_ids` to your forum supergroup id(s).
3. Create a topic. Watch `topic-experts-state.json` for the thread → profile
   mapping.

Manual provisioning / inspection (no gateway needed):

```bash
python3 plugins/topic-experts/provisioner.py list
python3 plugins/topic-experts/provisioner.py provision --role "media buying" \
    --thread-id 7225 --chat-id -1001234567890 --apply
```

## Files

- `__init__.py` — gateway plugin (`gateway_platform_event` + `pre_llm_call`).
- `provisioner.py` — profile/route builder (also usable standalone via CLI).
- `topic-experts.json.example` — all knobs with defaults.

## Design notes

- Messaging-bot tokens are never copied into served profiles (multiplexer
  token conflict); only an explicit LLM-key allowlist is inherited.
- New profiles are lean by default: `max_turns: 20`, heavy toolsets
  (`image_gen`, `video_gen`, `computer_use`, `browser`, `kanban`,
  `delegation`, `cronjob`) disabled — closer to a fast bot, with web/file/
  memory/terminal/skills retained.
- Scope enrichment (topic name → expert brief) runs concurrently with profile
  creation and degrades to a template scope on any failure.
