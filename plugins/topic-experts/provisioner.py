#!/usr/bin/env python3
"""provisioner.py — Topic -> Hermes profile provisioner for Telegram forum groups.

Creates an expert profile for a Telegram forum topic (thread_id), writes its SOUL,
seeds matching skills, and registers a gateway profile_route so that every message
in that topic runs under that isolated profile.

Requires gateway.multiplex_profiles: true on the DEFAULT profile for routes to act.

Commands:
  list                    Show profiles + configured routes
  provision               Create/refresh an expert profile (+ route with --apply)
  route                   Add/replace a route only
  remove                  Disable a route (profile kept)
  enable-multiplex        Turn multiplexing on + sync allowlist
  sync-allowlist          Recompute gateway.multiplex_profile_allowlist from routes

Examples:
  python3 topic_profile.py provision --role "media buying" --thread-id 7225 --apply
  python3 topic_profile.py provision --role "devops lead" --dry-run
  python3 topic_profile.py enable-multiplex --restart
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))
CONFIG = HERMES_HOME / "config.yaml"
PROFILES = HERMES_HOME / "profiles"
SKILLS_SRC = HERMES_HOME / "skills"
# Default chat this tool targets. Empty = --chat-id is required. Set to your
# Telegram forum supergroup id (e.g. "-1001234567890") to skip the flag.
DEFAULT_CHAT_ID = os.environ.get("TOPIC_PROFILE_CHAT_ID", "")
LOCAL_ROUTER = os.environ.get("TOPIC_PROFILE_ROUTER", "http://localhost:11435/v1")
ROUTER_MODEL = os.environ.get("TOPIC_PROFILE_MODEL", "glm-5.3-flash")

OUTPUT_RULES = """\
Output rules (hard):
- Drop articles, filler, hedging, pleasantries. Fragments OK.
- Short sentences. Specific numbers. No em dashes. No curly quotes. No semicolons.
- No AI-markers: never "delve", "leverage", "navigate", "utilize", "comprehensive".
- No emojis. No "as an AI". No restating the question. No narrating tool calls.
- Code blocks exact. Errors verbatim. Never fabricate tool output.
- If uncertain: say "I don't know" and check with a tool.
"""

SOUL_TEMPLATE = """\
# {title}

You are the **{role}** expert. Own the problem end-to-end.

## Scope
{scope}

## Rules
- Short sentences. Specific numbers. No filler.
- No AI-markers (never "delve", "leverage", "utilize").
- No em dashes, no curly quotes, no semicolons.
- Research before claiming. Cite sources.
- One recommendation. Bold bottom line. Exactly one next action.
- Code exact. Errors verbatim. Never fabricate.
- If uncertain: "I don't know" + check with a tool.
- Use parallel tool calls when possible. Lazy tool selection: load only what you need.
- Compression is on. Old turns are summarized. Key facts go to memory.

## Onboarding
First conversation: ask 3 short questions before long output:
1. What is the goal?
2. What is in scope / out of scope?
3. What existing knowledge should I know?
Store answers as durable facts.

After onboarding: rewrite this SOUL.md with the learned context.
Keep it under 300 tokens. Remove generic rules. Keep only what matters to this topic.

## Response format
- Simple questions: 1-2 lines.
- Analysis: max 5 bullets.
- Code: exact block with comments.
- Always end with: **Next:** [one action].

## Memory
Store {role} facts with memory tool. Keep MEMORY.md compact. Procedures go to skills.

{rules}
## Boundaries
- Scoped to {role}. Unrelated stays out.
- Never log secrets. Replace with [REDACTED].
"""


# ---------------------------------------------------------------- utilities
def die(msg: str, code: int = 1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def slugify(text: str, maxlen: int = 26) -> str:
    s = re.sub(r"[^\w]+", "-", text)
    s = re.sub(r"-+", "-", s).strip("-").lower()
    slug = s[:maxlen].strip("-")
    if not slug:
        slug = "topic-" + hex(abs(hash(text)) & 0xFFFF)[2:]
    return slug


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def hermes_bin() -> str:
    for cand in ("/usr/local/bin/hermes", "/root/.local/bin/hermes"):
        if Path(cand).exists():
            return cand
    found = shutil.which("hermes")
    if not found:
        die("hermes CLI not found")
    return found


# ---------------------------------------------------------------- config editing
# The gateway keys this tool owns. Everything else in config.yaml is left byte-identical:
# a ruamel round-trip re-wraps long plain scalars (e.g. an `X-API-Key:` line) and can
# silently insert a fold space into a secret.
MANAGED_KEYS = ("multiplex_profiles", "multiplex_profile_allowlist", "profile_routes")


def load_yaml(path: Path = None):
    """Read-only view of a config file. Never used to write."""
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    with open(path or CONFIG) as fh:
        return y, y.load(fh)


def _block_end(lines: list[str], start: int, indent: int) -> int:
    """Index after the last line belonging to a block opened at *start* with *indent*."""
    i = start + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() and (len(line) - len(line.lstrip())) <= indent:
            break
        i += 1
    return i


def render_managed_block(models: dict) -> list[str]:
    """Render the 2-space-indented gateway children we manage."""
    out: list[str] = []
    if "multiplex_profiles" in models:
        out.append(f"  multiplex_profiles: {'true' if models['multiplex_profiles'] else 'false'}")
    if "multiplex_profile_allowlist" in models:
        names = models["multiplex_profile_allowlist"] or []
        if names:
            out.append("  multiplex_profile_allowlist:")
            out.extend(f"    - {n}" for n in names)
        else:
            out.append("  multiplex_profile_allowlist: []")
    if "profile_routes" in models:
        routes = models["profile_routes"] or []
        if routes:
            out.append("  profile_routes:")
            for r in routes:
                out.append(f"    - name: {r['name']}")
                out.append(f"      platform: {r['platform']}")
                out.append(f"      chat_id: \"{r['chat_id']}\"")
                out.append(f"      thread_id: \"{r['thread_id']}\"")
                out.append(f"      profile: {r['profile']}")
                out.append(f"      enabled: {'true' if r.get('enabled', True) else 'false'}")
        else:
            out.append("  profile_routes: []")
    return out


def write_gateway_keys(models: dict, target: Path = None) -> Path:
    """Surgically set MANAGED_KEYS under the top-level `gateway:` block.

    Returns the backup path. Validates the result and rolls back on failure.
    """
    import yaml as pyyaml
    target = target or CONFIG
    original = target.read_text(encoding="utf-8")
    lines = original.splitlines()
    backup = target.with_name(target.name + f".bak.topicprofile.{int(time.time())}")
    backup.write_text(original, encoding="utf-8")

    # Locate top-level `gateway:`
    g = None
    for i, line in enumerate(lines):
        if line.rstrip() == "gateway:" or line.startswith("gateway:"):
            g = i
            break
    if g is None:
        lines.append("")
        lines.append("gateway:")
        g = len(lines) - 1
    block_end = _block_end(lines, g, 0)

    # Drop any existing managed keys inside the block (and their children).
    kept: list[str] = []
    i = g + 1
    while i < block_end:
        line = lines[i]
        stripped = line.strip()
        key = stripped.split(":")[0] if ":" in stripped else None
        if key in MANAGED_KEYS and line.startswith("  ") and not line.startswith("   "):
            i = _block_end(lines, i, 2)
            continue
        kept.append(line)
        i += 1

    new_block = kept + render_managed_block(models)
    result = lines[: g + 1] + new_block + lines[block_end:]
    text = "\n".join(result) + "\n"

    parsed = pyyaml.safe_load(text)  # raises on malformed output
    gw = parsed.get("gateway") or {}
    for key in models:
        if key == "profile_routes":
            assert len(gw.get("profile_routes") or []) == len(models[key] or []), "route count mismatch"
        elif key in models:
            assert gw.get(key) == models[key], f"{key} mismatch after write"

    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(target)
    return backup


def save_yaml(y, data, target: Path = None):
    """Legacy shim kept for callers that still pass a parsed doc; delegates to the surgical writer."""
    gw = (data or {}).get("gateway") or {}
    models = {k: gw[k] for k in MANAGED_KEYS if k in gw}
    return write_gateway_keys(models, target or CONFIG)


def get_gateway(data) -> dict:
    if "gateway" not in data or data["gateway"] is None:
        data["gateway"] = {}
    return data["gateway"]


def existing_routes(data) -> list:
    gw = data.get("gateway") or {}
    routes = gw.get("profile_routes")
    if routes is None:
        routes = data.get("profile_routes")
    return list(routes or [])


def set_routes(data, routes: list):
    gw = get_gateway(data)
    gw["profile_routes"] = routes
    if "profile_routes" in data:
        del data["profile_routes"]


def route_key(chat_id, thread_id) -> tuple:
    return (str(chat_id), str(thread_id))


def upsert_route(data, *, name, profile, chat_id, thread_id) -> str:
    routes = existing_routes(data)
    key = route_key(chat_id, thread_id)
    action = "added"
    kept = []
    for r in routes:
        if route_key(r.get("chat_id", ""), r.get("thread_id", "")) == key:
            action = "updated"
            continue
        kept.append(r)
    kept.append({
        "name": name,
        "platform": "telegram",
        "chat_id": str(chat_id),
        "thread_id": str(thread_id),
        "profile": profile,
        "enabled": True,
    })
    set_routes(data, kept)
    return action


# ---------------------------------------------------------------- profile build
def profile_exists(name: str) -> bool:
    return (PROFILES / name).is_dir()


def create_profile(name: str, role: str) -> None:
    if profile_exists(name):
        print(f"  profile {name} exists, reusing")
        return
    # No --clone-from: cloning copies the full skills tree (~59MB/profile) and the default
    # .env (which holds TELEGRAM_BOT_TOKEN -> multiplexer token conflict). We seed a lean
    # skill set and an explicit key allowlist instead.
    cmd = [hermes_bin(), "profile", "create", name,
           "--no-alias", "--description", f"{role} expert (topic-scoped)"]
    res = run(cmd)
    if res.returncode != 0 and not profile_exists(name):
        die(f"profile create failed: {res.stdout}{res.stderr}")
    print(f"  created profile {name}")


# Keys a topic expert legitimately needs. Messaging-bot tokens are deliberately
# absent: a served profile carrying the primary Telegram token aborts
# multiplexer startup (token conflict). Extend via your own wrapper if your
# experts need more (never add TELEGRAM_*/DISCORD_*/SLACK_* keys).
ENV_ALLOWLIST = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
    "BRAVE_API_KEY", "OPENCODE_GO_API_KEY",
)

# Never propagate these into a served profile, whatever the allowlist says.
ENV_DENY_PREFIXES = ("TELEGRAM_", "DISCORD_", "SLACK_", "MATRIX_", "WHATSAPP_", "SIGNAL_")


def copy_env_allowlist(home: Path) -> list[str]:
    src = HERMES_HOME / ".env"
    if not src.exists():
        return []
    kept, names = [], []
    for line in src.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key = raw.split("=", 1)[0].strip()
        if key not in ENV_ALLOWLIST or key.startswith(ENV_DENY_PREFIXES):
            continue
        kept.append(raw)
        names.append(key)
    dest = home / ".env"
    header = ("# Per-profile secrets for this Hermes profile.\n"
              "# Generated by topic_profile.py: explicit allowlist, no messaging tokens.\n")
    dest.write_text(header + "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    os.chmod(dest, 0o600)
    return names


SHARED_MEMORY_MARKER = "<!-- topic-experts shared-owner-context -->"


def seed_shared_memory(home: Path) -> list[str]:
    """Copy durable owner context (default profile USER.md/MEMORY.md) into a new
    expert profile so it doesn't start with amnesia about the owner.

    The built-in store reads exactly these two filenames under the profile's
    own memories/ dir, so the seed lands where recall already looks.
    Idempotent: skips files already carrying the marker.
    """
    src_dir = HERMES_HOME / "memories"
    dest_dir = home / "memories"
    dest_dir.mkdir(parents=True, exist_ok=True)
    seeded = []
    for name in ("USER.md", "MEMORY.md"):
        src = src_dir / name
        if not src.exists():
            continue
        try:
            content = src.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if not content:
            continue
        dest = dest_dir / name
        existing = dest.read_text(encoding="utf-8", errors="ignore") if dest.exists() else ""
        if SHARED_MEMORY_MARKER in existing:
            continue
        header = (f"{SHARED_MEMORY_MARKER}\n"
                  f"# Shared owner context (seeded from the default profile at provisioning; "
                  f"keep updated in place)\n\n")
        rest = ("\n\n" + existing.strip()) if existing.strip() else "\n"
        dest.write_text(header + content + rest, encoding="utf-8")
        seeded.append(name)
    return seeded


def write_soul(home: Path, role: str, scope: str) -> None:
    # role here is the LLM-generated professional title (e.g. "B2B Media Buying Strategist")
    title = role.title() if role else "Expert"
    body = SOUL_TEMPLATE.format(title=title, role=role, scope=scope, rules=OUTPUT_RULES)
    (home / "SOUL.md").write_text(body, encoding="utf-8")


def seed_skills(home: Path, role: str, limit: int = 8) -> list[str]:
    """Copy skills whose name/description matches role keywords. Returns copied names."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", role.lower()) if len(t) > 2]
    if not tokens:
        return []
    matches = []
    for skill_md in SKILLS_SRC.rglob("SKILL.md"):
        rel = skill_md.parent.relative_to(SKILLS_SRC)
        if rel.parts[0] in {"_archive", "archive"}:
            continue
        try:
            head = skill_md.read_text(encoding="utf-8", errors="ignore")[:1200].lower()
        except OSError:
            continue
        score = sum(1 for t in tokens if t in head or t in str(rel).lower())
        if score:
            matches.append((score, rel))
    matches.sort(key=lambda x: (-x[0], str(x[1])))
    dest_root = home / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    copied = []
    for _, rel in matches[:limit]:
        dest = dest_root / rel
        if dest.exists():
            continue
        shutil.copytree(SKILLS_SRC / rel, dest, dirs_exist_ok=True)
        copied.append(str(rel))
    return copied


def write_role_skill(home: Path, role: str, scope: str, sources: list[str]) -> None:
    name = "expert-charter"
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    body = f"""---
name: {name}
description: Use when starting or steering work in this topic. Charter for the {role} expert profile.
---

# {role.title()} charter

## Mandate
{scope}

## Standing method
1. Restate the real decision in one line before doing anything.
2. Gather evidence with tools. No claim without a source or a measurement.
3. Produce the artifact (script, doc, number), not a description of it.
4. Verify by running or reading back. Report actual output.
5. Close with one next action.

{OUTPUT_RULES}
## Reference material on disk
{chr(10).join('- ' + s for s in sources) if sources else '- none seeded, add with skill_manage/web research'}
"""
    (d / "SKILL.md").write_text(body, encoding="utf-8")


def write_config(home: Path, role: str, model: str, provider: str, base_url: str) -> None:
    """Lean profile config: fast like a simple bot (few turns, few tools), Hebrew display."""
    from ruamel.yaml import YAML
    y = YAML()
    y.preserve_quotes = True
    cfg_path = home / "config.yaml"
    data = y.load(cfg_path.read_text()) if cfg_path.exists() else {}
    data["model"] = {"default": model, "provider": provider, "base_url": base_url, "api_key": "not-needed"}
    data["agent"] = {"max_turns": 20, "task_completion_guidance": True,
                     "disabled_toolsets": ["image_gen", "video_gen", "computer_use", "browser",
                                           "kanban", "delegation", "cronjob"],
                     "verbose": False}
    data["terminal"] = {"backend": "local", "cwd": str(home), "timeout": 180}
    data["display"] = {"compact": True, "streaming": True, "show_cost": True, "language": "en"}
    data["context"] = {"engine": "compressor"}
    data["compression"] = {"enabled": True, "threshold": 0.5, "target_ratio": 0.2, "protect_last_n": 20}
    data["memory"] = {"memory_enabled": True, "user_profile_enabled": True,
                      "memory_char_limit": 1500, "user_char_limit": 800}
    data["topic_role"] = role
    with open(cfg_path, "w") as fh:
        y.dump(data, fh)


def rename_profile(old: str, role: str, scope: str, thread_id: str = "") -> tuple[str, str]:
    """Promote a profile to a new role name (e.g. temp exp-topic-8255 -> exp-crm-expert).

    Moves the profile dir, rewrites SOUL + charter + topic_role, repoints the
    gateway route and syncs the allowlist. Returns (new_name, backup_name).
    """
    from ruamel.yaml import YAML
    src = PROFILES / old
    if not src.is_dir():
        die(f"no such profile {old}")
    slug = f"exp-{slugify(role)}"
    if slug != old and (PROFILES / slug).is_dir():
        slug = f"{slug}-{thread_id}" if thread_id else f"{slug}-x"
        if (PROFILES / slug).is_dir():
            die(f"rename collision: {slug} already exists")
    home = PROFILES / slug
    if slug != old:
        shutil.move(str(src), str(home))
    write_soul(home, role, scope)
    sources = sorted(p.name for p in (home / "skills").iterdir() if p.is_dir()) if (home / "skills").is_dir() else []
    write_role_skill(home, role, scope, sources)
    y = YAML()
    y.preserve_quotes = True
    cfg_path = home / "config.yaml"
    data = y.load(cfg_path.read_text()) if cfg_path.exists() else {}
    data["topic_role"] = role
    with open(cfg_path, "w") as fh:
        y.dump(data, fh)
    gy, gdata = load_yaml()
    routes = existing_routes(gdata)
    hit = 0
    for r in routes:
        if str(r.get("profile")) == old:
            r["profile"] = slug
            r["name"] = slug
            hit += 1
    if not hit:
        die(f"no route pointing at {old}")
    set_routes(gdata, routes)
    sync_allowlist(gdata)
    backup = save_yaml(gy, gdata)
    print(f"renamed {old} -> {slug} ({hit} route(s)), backup {backup.name}")
    return slug, backup.name


def llm_enrich(role: str, timeout: int = 25) -> tuple[str, str]:
    """Ask the router for a professional title + scope. Returns (title, scope).

    The opencode-go router caps output at ~400 tokens. If content is empty,
    fall back to reasoning_content. Parse JSON if possible; else return raw.
    """
    prompt = (
        f"Topic name: '{role}'. "
        f"Return ONLY valid JSON with no markdown, no backticks, no preamble:\n"
        f'{{"title": "Professional expert title (2-4 words, specific not generic)", '
        f'"scope": "One sentence: what this expert owns end-to-end, no filler"}}'
    )
    body = json.dumps({"model": ROUTER_MODEL, "max_tokens": 500, "temperature": 0.3,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(f"{LOCAL_ROUTER}/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer not-needed"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode())
        msg = payload["choices"][0]["message"]
        text = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
        try:
            data = json.loads(text)
            title = data.get("title", role).strip()
            scope = data.get("scope", "").strip()
            return (title, scope)
        except json.JSONDecodeError:
            # fallback: first non-empty line as title, rest as scope
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            return (lines[0] if lines else role, " ".join(lines[1:]) if len(lines) > 1 else "")
    except Exception as exc:  # noqa: BLE001
        print(f"  enrich skipped ({type(exc).__name__}), using template scope")
        return (role, "")


# ---------------------------------------------------------------- commands
def cmd_list(_args):
    from ruamel.yaml import YAML
    y = YAML()
    data = y.load(CONFIG.read_text())
    gw = data.get("gateway") or {}
    print(f"multiplex_profiles: {gw.get('multiplex_profiles', False)}")
    print(f"allowlist: {gw.get('multiplex_profile_allowlist')}")
    print("routes:")
    for r in existing_routes(data):
        print(f"  [{ 'on' if r.get('enabled', True) else 'off'}] {r.get('name')} -> {r.get('profile')} "
              f"(chat {r.get('chat_id')} thread {r.get('thread_id')})")
    print("profiles:")
    for p in sorted(PROFILES.glob("exp-*")):
        size = run(["du", "-sh", str(p)]).stdout.split()[0]
        print(f"  {p.name:28} {size}")


def cmd_provision(args):
    role = args.role.strip()
    slug = args.profile or f"exp-{slugify(role)}"
    home = PROFILES / slug
    model = args.model or ROUTER_MODEL
    provider = args.provider or "opencode-go"
    base_url = args.base_url or "http://localhost:11435/v1"

    print(f"role      : {role}")
    print(f"profile   : {slug}")
    print(f"home      : {home}")
    print(f"thread    : {args.thread_id} (chat {args.chat_id})")

    scope = (args.scope or "").strip()
    if not scope and not args.no_enrich and not args.dry_run:
        _title, scope = llm_enrich(role)
    if not scope:
        scope = (f"Own {role} work for this workspace: research, decisions, artifacts and execution in this domain. "
                 f"Produce working output, not opinions.")

    if args.dry_run:
        print("\n[dry-run] would create profile, write SOUL + charter skill, seed matching skills,")
        print(f"[dry-run] and write route to {args.config_target}")
        print(f"[dry-run] scope preview: {scope[:200]}")
        return

    create_profile(slug, role)
    keys = copy_env_allowlist(home)
    write_soul(home, role, scope)
    copied = seed_skills(home, role)
    write_role_skill(home, role, scope, copied)
    write_config(home, role, model, provider, base_url)
    print(f"  env keys carried: {', '.join(keys) or 'none'}")
    print(f"  SOUL.md written, {len(copied)} skills seeded: {', '.join(copied[:6]) or 'none'}")

    if args.apply:
        target = Path(args.config_target) if args.config_target else CONFIG
        y, data = load_yaml(target)
        act = upsert_route(data, name=slug, profile=slug,
                           chat_id=args.chat_id, thread_id=args.thread_id)
        sync_allowlist(data)
        if args.enable_multiplex:
            get_gateway(data)["multiplex_profiles"] = True
        backup = save_yaml(y, data, target)
        print(f"  route {act}: {slug} -> chat {args.chat_id} thread {args.thread_id}")
        print(f"  config written ({target.name}), backup {backup.name}")
        if not (data.get("gateway") or {}).get("multiplex_profiles"):
            print("  NOTE: multiplex_profiles is off, route is inert. Run: enable-multiplex --restart")
    else:
        print("  route NOT applied (pass --apply)")

    if args.restart:
        schedule_restart()


def sync_allowlist(data) -> list:
    gw = get_gateway(data)
    names = sorted({str(r.get("profile")) for r in existing_routes(data)
                    if str(r.get("profile", "")).startswith("exp-") and r.get("enabled", True)})
    if names:
        gw["multiplex_profile_allowlist"] = names
    return names


def cmd_route(args):
    y, data = load_yaml()
    act = upsert_route(data, name=args.name or args.profile, profile=args.profile,
                       chat_id=args.chat_id, thread_id=args.thread_id)
    names = sync_allowlist(data)
    backup = save_yaml(y, data)
    print(f"route {act}: {args.profile} -> {args.chat_id}/{args.thread_id}")
    print(f"allowlist: {names}")
    print(f"backup: {backup}")
    if args.restart:
        schedule_restart()


def cmd_remove(args):
    y, data = load_yaml()
    routes = existing_routes(data)
    kept, hit = [], 0
    for r in routes:
        if str(r.get("profile")) == args.profile or r.get("name") == args.profile:
            hit += 1
            continue
        kept.append(r)
    if not hit:
        die(f"no route for {args.profile}")
    set_routes(data, kept)
    names = sync_allowlist(data)
    backup = save_yaml(y, data)
    print(f"removed {hit} route(s) for {args.profile}; allowlist now {names}")
    print(f"backup: {backup}")


def cmd_enable_multiplex(args):
    y, data = load_yaml()
    gw = get_gateway(data)
    gw["multiplex_profiles"] = True
    names = sync_allowlist(data)
    if not names:
        print("WARNING: no exp-* routes found. All profiles would be served. Add routes first.")
    backup = save_yaml(y, data)
    print(f"multiplex_profiles: true | allowlist: {names or 'UNSET (serves every profile)'}")
    print(f"backup: {backup}")
    if args.restart:
        schedule_restart()
    else:
        print("Restart to apply: python3 topic_profile.py enable-multiplex --restart")


def cmd_sync(args):
    y, data = load_yaml()
    names = sync_allowlist(data)
    backup = save_yaml(y, data)
    print(f"allowlist synced: {names}")
    print(f"backup: {backup}")


def bot_token() -> str:
    """Read the group bot token from the default profile's .env (never printed)."""
    env = HERMES_HOME / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("TELEGRAM_BOT_TOKEN not found in " + str(env))


def tg_api(method: str, **params) -> dict:
    """Minimal Bot API call. Returns the raw JSON reply so callers can prove it worked."""
    body = json.dumps(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{bot_token()}/{method}",
                                 data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _scope_lines(scope: str, limit: int = 3, max_chars: int = 140) -> list[str]:
    """Pick the first *limit* content lines out of an enriched scope brief."""
    lines = []
    for raw in (scope or "").splitlines():
        t = raw.strip().lstrip("-*•0123456789. ").strip()
        if len(t) < 12:
            continue
        lines.append(t[:max_chars])
        if len(lines) >= limit:
            break
    return lines


def greeting_text(role: str, profile: str, scope: str = "", temp: bool = False) -> str:
    if temp:
        return (f"It looks like a new topic just opened here that I haven't met yet ({role}).\n"
                f"Until we set it up properly, I'm the temporary expert here ({profile}).\n"
                f"Reply in one sentence: what is this topic's name and goal?\n"
                f"(Or just rename the topic above and I'll take care of the rest.)")
    head = (f"I'm the {role} expert for this topic.\n"
            f"Running as a separate profile ({profile}): my own memory, skills, and sessions.")
    context = ""
    points = _scope_lines(scope)
    if points:
        context = ("\nWhat I understand about this topic:\n" +
                   "\n".join(f"- {p}" for p in points))
    return (f"{head}{context}\n"
            f"To aim me precisely, answer briefly:\n"
            f"1. What is your goal for this topic?\n"
            f"2. What is my role here (what's in / what's out)?\n"
            f"3. What knowledge or materials do you already have that I should know?\n"
            f"Just write here directly, no mention needed.")


def send_topic_greeting(chat_id, thread_id, role: str, profile: str, scope: str = "",
                       temp: bool = False, *, dry_run: bool = False) -> dict:
    """Post the expert's first message into its own topic. This is the 'talk to me directly' step."""
    text = greeting_text(role, profile, scope, temp)
    if dry_run:
        return {"ok": True, "dry_run": True, "chat_id": str(chat_id),
                "thread_id": str(thread_id), "text": text}
    try:
        res = tg_api("sendMessage", chat_id=int(chat_id), message_thread_id=int(thread_id),
                     text=text, disable_notification=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(res.get("ok")), "message_id": (res.get("result") or {}).get("message_id"),
            "error": None if res.get("ok") else res.get("description")}


def cmd_greet(args):
    """Post the first message from the topic's own expert into that topic."""
    role = args.role or ""
    profile = args.profile or ""
    if not role or not profile:
        state = {}
        sf = HERMES_HOME / "topic-experts-state.json"
        try:
            state = (json.loads(sf.read_text(encoding="utf-8")).get("topics") or {})
        except Exception:  # noqa: BLE001
            state = {}
        entry = state.get(f"{args.chat_id}:{args.thread_id}") or {}
        role = role or entry.get("name") or "topic"
        profile = profile or entry.get("profile") or ""
    res = send_topic_greeting(args.chat_id, args.thread_id, role, profile, dry_run=not args.send)
    if res.get("dry_run"):
        print("DRY RUN (pass --send to post):")
        print(f"  chat {res['chat_id']} thread {res['thread_id']}")
        print(res["text"])
        return
    if res.get("ok"):
        print(f"greeted: chat {args.chat_id} thread {args.thread_id} message_id {res.get('message_id')}")
    else:
        print(f"greet FAILED: {res.get('error')}")


def schedule_restart(delay: int = 6):
    """Detached restart: a transient systemd unit survives the gateway's own cgroup teardown."""
    unit = f"hermes-gw-restart-{int(time.time())}"
    cmd = ["systemd-run", "--user", "--collect", f"--unit={unit}", "--on-active", str(delay),
           "/bin/systemctl", "--user", "restart", "hermes-gateway.service"]
    res = run(cmd)
    if res.returncode == 0:
        print(f"  gateway restart scheduled in {delay}s ({unit})")
    else:
        print(f"  restart scheduling failed: {res.stderr.strip()}")
        print("  restart manually from SSH: systemctl --user restart hermes-gateway.service")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show routes + exp-* profiles").set_defaults(func=cmd_list)

    pr = sub.add_parser("provision", help="create expert profile (+ route)")
    pr.add_argument("--role", required=True)
    pr.add_argument("--profile")
    pr.add_argument("--thread-id", default="")
    pr.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    pr.add_argument("--model", default="")
    pr.add_argument("--provider", default="")
    pr.add_argument("--base-url", default="")
    pr.add_argument("--no-enrich", action="store_true")
    pr.add_argument("--scope", default="", help="explicit domain brief (skips the router call)")
    pr.add_argument("--apply", action="store_true", help="write route into config.yaml")
    pr.add_argument("--enable-multiplex", action="store_true")
    pr.add_argument("--config-target", default="", help="alternate config file (testing)")
    pr.add_argument("--restart", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=cmd_provision)

    rt = sub.add_parser("route", help="add/replace one route")
    rt.add_argument("--profile", required=True)
    rt.add_argument("--name")
    rt.add_argument("--thread-id", required=True)
    rt.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    rt.add_argument("--restart", action="store_true")
    rt.set_defaults(func=cmd_route)

    rm = sub.add_parser("remove", help="disable a route")
    rm.add_argument("--profile", required=True)
    rm.set_defaults(func=cmd_remove)

    em = sub.add_parser("enable-multiplex", help="turn on multiplexing + sync allowlist")
    em.add_argument("--restart", action="store_true")
    em.set_defaults(func=cmd_enable_multiplex)

    sub.add_parser("sync-allowlist").set_defaults(func=cmd_sync)

    gr = sub.add_parser("greet", help="post the expert's first message into its own topic")
    gr.add_argument("--thread-id", required=True)
    gr.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    gr.add_argument("--role", default="")
    gr.add_argument("--profile", default="")
    gr.add_argument("--send", action="store_true", help="actually post (default is a dry run)")
    gr.set_defaults(func=cmd_greet)
    return p


if __name__ == "__main__":
    args = build_parser().parse_args()
    args.func(args)
