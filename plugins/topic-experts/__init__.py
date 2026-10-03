"""topic-experts — a Telegram forum topic becomes its own Hermes expert profile.

Trigger: the ``forum_topic_created`` / ``forum_topic_edited`` gateway platform event
(fired by the Telegram adapter from the service message Telegram sends when a topic is
created or renamed — the Bot API has no read method for a topic name, so that service
message is the only source of truth).

What happens, with no human in the loop:

    topic created "media buying"
        -> capture (chat_id, thread_id=8111, name="media buying")
        -> create profile  exp-media-buying  (SOUL + charter skill + seeded skills + lean config)
        -> write gateway.profile_routes entry + multiplex_profile_allowlist
        -> after a quiet window, ONE detached gateway restart for the whole batch
           (graceful SIGUSR1: in-flight turns drain first, no work is killed)
        -> every message in thread 8111 now runs as that expert, own memory, own skills

Everything heavy runs on a worker thread: a ``gateway_platform_event`` observer is on the
adapter's update loop, so the callback must return immediately.

Config: ``$HERMES_HOME/topic-experts.json`` (all optional).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))
# The provisioner ships inside this plugin directory so the plugin is
# self-contained (no dependency on $HERMES_HOME/scripts/).
SCRIPT = Path(__file__).resolve().parent / "provisioner.py"
CONFIG_FILE = HERMES_HOME / "topic-experts.json"
STATE_FILE = HERMES_HOME / "topic-experts-state.json"
GATEWAY_UNIT = "hermes-gateway.service"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    # Groups this plugin acts on. Empty = configure first (see README);
    # the plugin still arms but only logs events until chat_ids is set.
    "chat_ids": [],
    # Seconds of quiet after the last topic creation before the single restart fires.
    "quiet_seconds": 180,
    # False = write profiles and routes but never restart (routes stay inert until a manual restart).
    "restart": True,
    # "graceful" = SIGUSR1 in-band restart (refuse new turns, let in-flight work finish, then exit 75).
    # "service"  = systemctl restart (hard SIGTERM, kills whatever is running).
    "restart_mode": "graceful",
    # Never restart while an agent turn is mid-run: a gateway restart SIGTERMs in-flight work.
    "require_idle": True,
    # Stop waiting for idle after this many seconds and restart anyway. 0 = wait forever.
    "max_defer_seconds": 1800,
    # Seconds to wait for a graceful drain to exit before escalating to a hard service restart.
    "graceful_escalate_seconds": 120,
    # Post the new expert's first message into its own topic so the topic talks without being poked.
    "greet": True,
    # Safety net: first user message in an unknown monitored thread bootstraps
    # a temp expert (covers missed forum_topic_created events). Set false to
    # disable temp provisioning; creation/rename events still work.
    "first_message_bootstrap": True,
    # Topics whose name matches one of these are ignored (Telegram's General topic is thread 1).
    "skip_names": ["general"],
    "profile_prefix": "exp-",
    # Refuse to auto-create beyond this many auto profiles. A backstop, not a policy.
    "max_profiles": 50,
    "model": "glm-5.3-flash",
    "provider": "opencode-go",
    "base_url": "http://localhost:11435/v1",
}

_lock = threading.Lock()
_queue: List[Dict[str, Any]] = []
_last_activity = 0.0
_deferred_since = 0.0
_busy_logged = False
_worker: Optional[threading.Thread] = None


# ------------------------------------------------------------------ helpers
def _load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _save_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _cfg() -> Dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update(_load_json(CONFIG_FILE, {}) or {})
    return merged


_cfg_cache: Dict[str, Any] = {"at": 0.0, "cfg": {}}
_CFG_TTL = 30.0


def _cfg_cached() -> Dict[str, Any]:
    """TTL-cached config for hot paths (pre_llm_call fires on every turn)."""
    now = time.time()
    if now - _cfg_cache["at"] > _CFG_TTL:
        _cfg_cache["cfg"] = _cfg()
        _cfg_cache["at"] = now
    return _cfg_cache["cfg"]


def _load_topic_profile_module():
    """Import the provisioner by path (it lives in scripts/, not on sys.path)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("topic_profile", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _slug(text: str, prefix: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower().strip())
    s = re.sub(r"-+", "-", s).strip("-")[:24].strip("-")
    return f"{prefix}{s or 'expert'}"


def _service_restart(delay: int, unit: str = GATEWAY_UNIT) -> bool:
    """Blunt restart via a transient systemd unit (SIGTERM, kills in-flight work).

    A restart issued from inside the gateway would SIGTERM the gateway's own cgroup. A
    transient ``systemd-run`` unit is owned by the user manager, so it outlives the unit it
    is restarting. Only used as a last resort: ``_sigusr1_restart`` is the graceful path.
    """
    name = f"hermes-topic-experts-restart-{int(time.time())}"
    cmd = ["systemd-run", "--user", "--collect", f"--unit={name}", "--on-active", str(delay),
           "/bin/systemctl", "--user", "restart", unit]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[topic-experts] restart scheduling raised: %s", exc)
        return False
    if res.returncode == 0:
        logger.info("[topic-experts] gateway service restart scheduled in %ss (%s)", delay, name)
        return True
    logger.warning("[topic-experts] restart scheduling failed: %s", (res.stderr or "").strip())
    return False


def _gateway_pid() -> Optional[int]:
    """PID of the running gateway, read from ``$HERMES_HOME/gateway.pid`` (kind-verified)."""
    data = _load_json(HERMES_HOME / "gateway.pid", {}) or {}
    if str(data.get("kind") or "") not in ("hermes-gateway", ""):
        return None
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _sigusr1_restart() -> bool:
    """Ask the gateway to restart in-band (SIGUSR1).

    ``gateway/run.py`` maps SIGUSR1 to ``request_restart(via_service=True)``: refuse new turns,
    let every in-flight agent/cron/api run finish (cap ``agent.restart_after_turn_timeout``,
    default 1800s), then ``stop()`` and exit 75 so systemd relaunches. That is the whole point:
    the restart stops being an event that kills work.
    """
    if not hasattr(signal, "SIGUSR1"):
        return False
    pid = _gateway_pid() or os.getpid()
    if not _pid_alive(pid):
        logger.warning("[topic-experts] gateway pid %s is not alive; nothing to signal", pid)
        return False
    try:
        os.kill(pid, signal.SIGUSR1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[topic-experts] SIGUSR1 to %s raised: %s", pid, exc)
        return False
    logger.info("[topic-experts] graceful restart requested (SIGUSR1 -> pid %s); in-flight work "
                "will drain first", pid)
    return True


def _escalate_if_stuck(pid: Optional[int], cfg: Dict[str, Any]) -> None:
    """Fallback watchdog: if the graceful drain never exits, hard-restart once nothing is live.

    Runs on its own daemon thread. Never escalates while a turn holds a lease (that would
    recreate the bug this gate exists to prevent), and gives up quietly once the PID changes
    (the restart worked) or the deferral cap is spent.
    """
    wait = float(cfg.get("graceful_escalate_seconds", 120))
    max_defer = float(cfg.get("max_defer_seconds", 1800))
    if wait <= 0:
        return
    started = time.time()
    while True:
        time.sleep(wait)
        current = _gateway_pid()
        if pid is None or current != pid:
            return  # graceful restart landed (new process) or no pid to watch
        if not _pid_alive(pid):
            return
        deferred = time.time() - started
        if _turn_busy() and not (max_defer > 0 and deferred >= max_defer):
            logger.info("[topic-experts] graceful restart still draining (%ss); a turn holds the "
                        "lease, leaving it alone", int(deferred))
            continue
        logger.warning("[topic-experts] graceful restart to pid %s has not exited after %ss "
                       "(no live turn); escalating to a hard service restart", pid, int(deferred))
        _service_restart(5)
        return


def _schedule_restart(delay: int, unit: str = GATEWAY_UNIT, mode: str = "graceful") -> bool:
    """Restart the gateway after ``delay`` seconds: graceful (SIGUSR1) by default.

    The graceful path fires from a worker thread so the caller returns at once and the
    escalation watchdog can be armed alongside it.
    """
    if mode != "graceful":
        return _service_restart(delay, unit)
    pid = _gateway_pid()

    def _fire() -> None:
        time.sleep(max(0, int(delay)))
        if _sigusr1_restart():
            _escalate_if_stuck(pid, _cfg())

    threading.Thread(target=_fire, name="topic-experts-restart", daemon=True).start()
    return True


def _holder_pid(holder: str) -> Optional[int]:
    """Pull the PID out of a lease holder string, e.g. ``pid=1234:turn=...``."""
    for part in str(holder or "").split(":"):
        if part.startswith("pid="):
            try:
                return int(part[4:])
            except ValueError:
                return None
    return None


def _pid_alive(pid: int) -> bool:
    """True when a PID is still running. PermissionError counts as alive (foreign process)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return True
    return True


def _turn_busy(db: Optional[Path] = None) -> bool:
    """True while any conversation holds a live agent-turn lease.

    The gateway renews one ``session_turn_leases`` row for the whole length of a turn (300s
    TTL, refreshed on every transcript write). An unexpired row whose holder PID is alive
    means an agent is mid-run, so restarting now kills that work. Anything we cannot read or
    parse counts as busy: the cost of a late restart beats the cost of a SIGTERMed task.
    """
    import sqlite3
    path = db or (HERMES_HOME / "state.db")
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute("SELECT holder, expires_at FROM session_turn_leases").fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[topic-experts] busy check failed (%s); assuming busy", exc)
        return True
    now = time.time()
    for holder, expires_at in rows:
        try:
            if float(expires_at) <= now:
                continue
        except (TypeError, ValueError):
            continue
        pid = _holder_pid(holder)
        if pid is None or _pid_alive(pid):
            return True
    return False


def _restart_action(cfg: Dict[str, Any]) -> str:
    """Decide the next restart step once the queue has drained: 'wait', 'busy' or 'go'.

    'busy' = the quiet window elapsed but a turn is live, so the restart is deferred until
    the gateway goes idle (bounded by ``max_defer_seconds`` so a wedged turn cannot leave the
    new routes inert forever).
    """
    global _deferred_since, _busy_logged
    max_defer = float(cfg.get("max_defer_seconds", 1800))
    with _lock:
        quiet_elapsed = bool(_last_activity) and (time.time() - _last_activity) >= float(
            cfg.get("quiet_seconds", 180))
        if not quiet_elapsed:
            _deferred_since = 0.0
            _busy_logged = False
            return "wait"
        if not _deferred_since:
            _deferred_since = time.time()
        deferred_for = time.time() - _deferred_since

    if not cfg.get("require_idle", True) or not _turn_busy():
        return "go"
    if max_defer > 0 and deferred_for >= max_defer:
        logger.warning("[topic-experts] waited %ss for idle (max_defer_seconds=%s); restarting with "
                       "a turn still live", int(deferred_for), int(max_defer))
        return "go"
    with _lock:
        first = not _busy_logged
        _busy_logged = True
    if first:
        logger.info("[topic-experts] restart deferred: agent turn in progress, will fire when idle")
    return "busy"


# ------------------------------------------------------------------ worker
def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="topic-experts", daemon=True)
            _worker.start()


def _provision(job: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Build one expert profile + route. Returns a result dict for the state file."""
    tp = _load_topic_profile_module()
    name = (job.get("name") or "").strip()
    prefix = cfg.get("profile_prefix", "exp-")
    profile = _slug(name, prefix)

    existing = {p.name for p in (HERMES_HOME / "profiles").glob(f"{prefix}*")}
    auto_created = len(existing)
    if profile not in existing and auto_created >= int(cfg.get("max_profiles", 20)):
        raise RuntimeError(f"max_profiles ({cfg.get('max_profiles')}) reached, refusing to create {profile}")

    started = time.time()
    # Profile creation (CLI subprocess) and scope enrichment (router call) are
    # independent and both slow: run them concurrently so provisioning costs
    # max(create, enrich) instead of create + enrich.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2,
                                               thread_name_prefix="topic-experts-prov") as pool:
        fut_profile = pool.submit(tp.create_profile, profile, name)
        fut_enrich = pool.submit(tp.llm_enrich, name)
        fut_profile.result()
        try:
            enriched_title, enriched_scope = fut_enrich.result()
        except Exception as exc:  # noqa: BLE001
            logger.info("[topic-experts] enrich skipped for %r (%s), using template scope", name, exc)
            enriched_title, enriched_scope = name, ""
    title = enriched_title or name
    scope = enriched_scope or f"Own {name} work for this workspace. Produce the artifact, not a description of it."
    home = HERMES_HOME / "profiles" / profile
    tp.copy_env_allowlist(home)
    shared_mem = tp.seed_shared_memory(home)
    if shared_mem:
        logger.info("[topic-experts] seeded shared owner memory (%s) for %s",
                    ",".join(shared_mem), profile)
    tp.write_soul(home, title, scope)
    seeded = tp.seed_skills(home, name)
    tp.write_role_skill(home, title, scope, seeded)
    tp.write_config(home, name, cfg.get("model", DEFAULTS["model"]),
                    cfg.get("provider", DEFAULTS["provider"]),
                    cfg.get("base_url", DEFAULTS["base_url"]))

    y, data = tp.load_yaml(tp.CONFIG)
    action = tp.upsert_route(data, name=profile, profile=profile,
                             chat_id=job["chat_id"], thread_id=job["thread_id"])
    allowlist = tp.sync_allowlist(data)
    tp.get_gateway(data)["multiplex_profiles"] = True
    backup = tp.write_gateway_keys(
        {k: tp.get_gateway(data)[k] for k in tp.MANAGED_KEYS if k in tp.get_gateway(data)},
        tp.CONFIG)
    return {"profile": profile, "role": name, "scope": scope, "route_action": action,
            "seeded_skills": seeded, "allowlist": allowlist, "backup": backup.name,
            "temp": bool(job.get("temp"))}


def _rename(job: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Promote a temp expert to its real topic name. Returns a result dict for the state file."""
    tp = _load_topic_profile_module()
    name = (job.get("name") or "").strip()
    old = job.get("rename_from") or ""
    if not old:
        raise RuntimeError("rename job without rename_from")
    try:
        enriched_title, enriched_scope = tp.llm_enrich(name)
    except Exception as exc:  # noqa: BLE001
        logger.info("[topic-experts] enrich skipped for %r (%s), using template scope", name, exc)
        enriched_title, enriched_scope = name, ""
    title = enriched_title or name
    scope = enriched_scope or f"Own {name} work for this workspace. Produce the artifact, not a description of it."
    new_profile, backup = tp.rename_profile(old, title, scope, str(job.get("thread_id") or ""))
    return {"profile": new_profile, "role": title, "scope": scope, "route_action": "renamed",
            "seeded_skills": [], "allowlist": [], "backup": backup, "temp": False}


def _worker_loop() -> None:
    global _last_activity
    while True:
        job = None
        with _lock:
            if _queue:
                job = _queue.pop(0)
        if job is not None:
            cfg = _cfg()
            try:
                if job.get("op") == "rename":
                    t0 = time.time()
                    result = _rename(job, cfg)
                    logger.info("[topic-experts] renamed %s -> %s for thread %s in %.1fs",
                                job.get("rename_from"), result["profile"],
                                job.get("thread_id"), time.time() - t0)
                else:
                    # Re-check fresh state: a duplicate event may have been
                    # handled while this job waited in the queue.
                    fresh = ((_load_json(STATE_FILE, {}) or {}).get("topics", {})
                             .get(f"{job.get('chat_id')}:{job.get('thread_id')}") or {})
                    if fresh.get("profile") and not fresh.get("error"):
                        logger.info("[topic-experts] thread %s already provisioned as %s; skipping queued job",
                                    job.get("thread_id"), fresh.get("profile"))
                        continue
                    t0 = time.time()
                    result = _provision(job, cfg)
                    logger.info("[topic-experts] provisioned %s for thread %s (%s) in %.1fs",
                                result["profile"], job["thread_id"], result["route_action"],
                                time.time() - t0)
                _record_state(job, result, error=None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[topic-experts] provisioning failed for thread %s: %s",
                               job.get("thread_id"), exc, exc_info=True)
                _record_state(job, None, error=str(exc))
            else:
                if cfg.get("greet", True):
                    try:
                        tp = _load_topic_profile_module()
                        res = tp.send_topic_greeting(job["chat_id"], job["thread_id"],
                                                     result.get("role") or job.get("name") or "topic",
                                                     result.get("profile") or "",
                                                     result.get("scope") or "",
                                                     result.get("temp", False))
                        if res.get("ok"):
                            logger.info("[topic-experts] greeted topic %s (message_id %s)",
                                        job["thread_id"], res.get("message_id"))
                        else:
                            logger.warning("[topic-experts] greeting failed for thread %s: %s",
                                           job.get("thread_id"), res.get("error"))
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[topic-experts] greeting raised for thread %s: %s",
                                       job.get("thread_id"), exc)
            with _lock:
                _last_activity = time.time()
            continue

        # Queue drained: decide whether the batch's restart window has elapsed.
        cfg = _cfg()
        if _restart_action(cfg) == "go":
            scheduled = False
            if cfg.get("restart", True):
                try:
                    scheduled = _schedule_restart(5, mode=str(cfg.get("restart_mode", "graceful")))
                except Exception as exc:  # noqa: BLE001
                    # Never let a restart dispatch failure kill the worker thread: the next
                    # topic would then provision without ever activating its route.
                    logger.warning("[topic-experts] restart dispatch raised: %s", exc, exc_info=True)
            else:
                logger.info("[topic-experts] restart disabled; run `hermes gateway restart` to "
                            "activate the new routes")
                scheduled = True
            if scheduled:
                with _lock:
                    _last_activity = 0.0
                    _deferred_since = 0.0
                    _busy_logged = False
        time.sleep(5)


def _record_state(job: Dict[str, Any], result: Optional[Dict[str, Any]], error: Optional[str]) -> None:
    state = _load_json(STATE_FILE, {}) or {}
    topics = state.setdefault("topics", {})
    key = f"{job.get('chat_id')}:{job.get('thread_id')}"
    previous = topics.get(key) or {}
    # Never regress an already-onboarded topic back to False (e.g. error retry).
    onboarded = bool(previous.get("onboarded")) if result is None else False
    topics[key] = {
        "name": job.get("name"), "profile": (result or {}).get("profile"),
        "scope": (result or {}).get("scope") or previous.get("scope") or "",
        "temp": bool((result or {}).get("temp", previous.get("temp", False))),
        "onboarded": onboarded,
        "at": time.time(), "error": error,
    }
    state["updated_at"] = time.time()
    _save_json(STATE_FILE, state)


# ------------------------------------------------------------------ hooks
def _parse_telegram_thread(session_id: str) -> tuple[str, str]:
    """Extract (chat_id, thread_id) from a gateway session id.

    Format: ``agent:main:telegram:<chat_type>:<chat_id>:<thread_id>``.
    Returns ("", "") when the shape does not match (DMs, CLI, other platforms).
    """
    parts = str(session_id or "").split(":")
    if "telegram" not in parts:
        return "", ""
    i = parts.index("telegram")
    chat = parts[i + 2] if len(parts) > i + 2 else ""
    thread = parts[i + 3] if len(parts) > i + 3 else ""
    if not chat or not thread or thread == "1":
        return "", ""
    return chat, thread


def pre_llm_call(user_message: str | None = None, session_id: str = "",
                 platform: str = "", is_first_turn: bool = False, **_: Any):
    """Safety net: first message in an unknown monitored thread bootstraps a temp expert.

    Covers the case where Telegram's ``forum_topic_created`` service message never
    reached the gateway (missed poll window, restart overlap): the user never has
    to "start" anything — one message and the topic gets its own expert + greeting.
    Must stay microsecond-cheap and never raise: it runs in every turn's path.
    """
    try:
        if platform != "telegram":
            return None
        cfg = _cfg_cached()
        if not cfg.get("enabled", True):
            return None
        if not cfg.get("first_message_bootstrap", True):
            return None
        chat_id, thread_id = _parse_telegram_thread(session_id)
        if not chat_id or not thread_id:
            return None
        if chat_id not in [str(c) for c in cfg.get("chat_ids") or []]:
            return None
        known = (_load_json(STATE_FILE, {}) or {}).get("topics", {})
        previous = known.get(f"{chat_id}:{thread_id}") or {}
        if previous.get("profile") and not previous.get("error"):
            return None
        job = {"chat_id": chat_id, "thread_id": thread_id,
               "name": f"topic-{thread_id}", "temp": True}
        with _lock:
            if any(j.get("thread_id") == thread_id and j.get("chat_id") == chat_id
                   for j in _queue):
                return None
            _queue.append(job)
        logger.info("[topic-experts] first message in unknown thread %s -> queued temp provisioning",
                    thread_id)
        _ensure_worker()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[topic-experts] pre_llm_call safety net skipped: %s", exc)
    return None


def _on_platform_event(platform: Any = None, event_type: str = "", payload: Optional[Dict] = None, **_: Any):
    """Observer for the adapter's normalized Telegram events (must return fast)."""
    if platform != "telegram" or not isinstance(payload, dict):
        return None
    if event_type not in ("forum_topic_created", "forum_topic_edited"):
        return None

    cfg = _cfg()
    if not cfg.get("enabled", True):
        return None

    chat_id = str(payload.get("chat_id") or "")
    thread_id = str(payload.get("thread_id") or "")
    name = (payload.get("name") or "").strip()
    if not chat_id or not thread_id or not name:
        logger.info("[topic-experts] ignoring %s without name/thread (chat=%s thread=%s)",
                    event_type, chat_id, thread_id)
        return None
    # Telegram's General topic is thread 1 and is not a real topic.
    if thread_id == "1" or name.lower() in {n.lower() for n in cfg.get("skip_names", [])}:
        logger.info("[topic-experts] skipping topic %r (thread %s)", name, thread_id)
        return None

    allowed = [str(c) for c in cfg.get("chat_ids") or []]
    if allowed and chat_id not in allowed:
        return None

    known = (_load_json(STATE_FILE, {}) or {}).get("topics", {})
    previous = known.get(f"{chat_id}:{thread_id}") or {}
    if previous.get("profile") and not previous.get("error"):
        if previous.get("temp") and name.lower() not in (
                (previous.get("name") or "").lower(), ""):
            # The temp expert finally learned the real topic name (created or
            # renamed after the creation event was missed): promote it.
            job = {"op": "rename", "chat_id": chat_id, "thread_id": thread_id,
                   "name": name, "rename_from": previous.get("profile")}
            with _lock:
                if any(j.get("thread_id") == thread_id and j.get("chat_id") == chat_id
                       for j in _queue):
                    return None
                _queue.append(job)
            logger.info("[topic-experts] queued rename to %r for temp thread %s",
                        name, thread_id)
            _ensure_worker()
            return None
        if event_type == "forum_topic_edited":
            # Rename of an already-provisioned topic: keep the profile, note the new label.
            logger.info("[topic-experts] topic %s renamed to %r; profile %s unchanged",
                        thread_id, name, previous.get("profile"))
            return None
        logger.info("[topic-experts] thread %s already provisioned as %s", thread_id, previous["profile"])
        return None

    job = {"chat_id": chat_id, "thread_id": thread_id, "name": name}
    with _lock:
        if any(j["thread_id"] == thread_id and j["chat_id"] == chat_id for j in _queue):
            return None
        _queue.append(job)
    logger.info("[topic-experts] queued provisioning for topic %r (thread %s)", name, thread_id)
    _ensure_worker()
    return None


def register(ctx) -> None:
    """Register the forum-topic observer + first-message safety net."""
    ctx.register_hook("gateway_platform_event", _on_platform_event)
    try:
        ctx.register_hook("pre_llm_call", pre_llm_call)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[topic-experts] pre_llm_call registration failed: %s", exc)
    _cfg_now = _cfg()
    logger.info("[topic-experts] armed (config=%s, restart_mode=%s, require_idle=%s, "
                "max_defer_seconds=%s)",
                CONFIG_FILE, _cfg_now.get("restart_mode", "graceful"),
                _cfg_now.get("require_idle", True), _cfg_now.get("max_defer_seconds", 1800))
