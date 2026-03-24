#!/usr/bin/env python3
"""
YesBot — autopilot decision engine for Claude Code hooks.

Reads yesbot-rules.md and makes allow/block/pass-through decisions
for Claude Code tool calls, so you can walk away while it works.

CLI modes:
    --decide --event PreToolUse   Read stdin JSON, output decision
    --install                     Add hooks to settings.local.json
    --uninstall                   Remove hooks from settings
    --on                          Enable YesBot
    --off                         Disable YesBot
    --status                      Show current state
    --log                         Show last 20 decisions
    --test Tool '{"key":"val"}'   Test a hypothetical tool call
    --dashboard [--port N]        Start live dashboard (default port 8766)
"""

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, jsonify, request as flask_request
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# ── Paths ────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_project_root():
    """Find project root by walking up to find .claude/ or .git/"""
    d = Path(__file__).resolve().parent
    for _ in range(10):
        if (d / '.claude').exists() or (d / '.git').exists():
            return d
        parent = d.parent
        if parent == d:
            break
        d = parent
    return Path(__file__).resolve().parent


PROJECT_ROOT = _find_project_root()
DATA_DIR = SCRIPT_DIR / "data"
STATE_FILE = DATA_DIR / "yesbot-state.json"
LOG_FILE = DATA_DIR / "yesbot-decisions.jsonl"
RULES_FILE = SCRIPT_DIR / "yesbot-rules.md"
SETTINGS_FILE = PROJECT_ROOT / ".claude" / "settings.local.json"

# ── Default state ────────────────────────────────────────────────────────

DEFAULT_STATE = {
    "enabled": False,
    "task": None,
    "session_id": None,
    "session_name": None,
    "enabled_at": None,
    "error_count": 0,
    "max_errors": 3,
    "decisions_count": 0,
    "targeted_sessions": {},
    "global_enabled": True,
    "active_sessions": {},
}

# ── Allow / Block lists ─────────────────────────────────────────────────

# Tools that are always allowed (read-only or safe)
ALWAYS_ALLOW_TOOLS = {"Read", "Glob", "Grep", "Agent", "WebFetch", "WebSearch",
                      "TodoRead", "TodoWrite", "Skill", "ToolSearch"}

# Bash command first-words that are safe
BASH_ALLOW_FIRST = {
    "python", "python3", "pip", "pip3", "git", "npm", "node", "npx",
    "pytest", "ruff", "black", "mypy", "ls", "dir", "cat", "head",
    "tail", "echo", "pwd", "cd", "curl", "wget", "mkdir", "cp",
    "mv", "touch", "find", "grep", "rg", "which", "where", "env",
    "printenv", "sort", "uniq", "wc", "diff", "tee", "true", "false",
    "test", "[", "type", "command", "set",
}

# Bash patterns that are always blocked
BASH_BLOCK_PATTERNS = [
    "rm -rf",
    "rm -fr",
    "deltree",
    "format ",
    "sudo ",
    "shutdown",
    "reboot",
    "git branch -D",
    "git branch -d",
    "git rebase",
    "git push --force",
    "git push -f ",
]

# Bash patterns that require user confirmation (pass-through)
BASH_ASK_PATTERNS = [
    "git push",
    "pip install --user",
    "pip install -g",
    "npm -g",
    "npm install -g",
]

# ── State management ────────────────────────────────────────────────────

def load_state() -> dict:
    """Load state from disk, or return defaults."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULT_STATE)


def save_state(state: dict) -> None:
    """Persist state to disk."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _get_session_id() -> str:
    """Get or create a session ID for tracking."""
    state = load_state()
    sid = state.get("session_id")
    if not sid:
        import uuid
        sid = str(uuid.uuid4())[:8]
        state["session_id"] = sid
        save_state(state)
    return sid


def _describe_tool_action(tool_name: str, tool_input: dict) -> str:
    """Generate a human-readable description of what the tool wants to do."""
    if tool_input.get("description"):
        return tool_input["description"]

    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        if not cmd:
            return "Run an empty command"
        short = cmd[:100] + ("..." if len(cmd) > 100 else "")
        return "Run shell command: %s" % short

    if tool_name == "Read":
        path = tool_input.get("file_path", "unknown")
        name = Path(path).name if path else "unknown"
        return "Read file: %s" % name

    if tool_name == "Edit":
        path = tool_input.get("file_path", "unknown")
        name = Path(path).name if path else "unknown"
        old = (tool_input.get("old_string", "") or "")[:40]
        return "Edit %s (replacing '%s...')" % (name, old) if old else "Edit %s" % name

    if tool_name == "Write":
        path = tool_input.get("file_path", "unknown")
        name = Path(path).name if path else "unknown"
        return "Create/overwrite file: %s" % name

    if tool_name == "Glob":
        pattern = tool_input.get("pattern", "")
        return "Search for files matching: %s" % pattern

    if tool_name == "Grep":
        pattern = tool_input.get("pattern", "")
        return "Search file contents for: %s" % pattern[:60]

    if tool_name == "Agent":
        desc = tool_input.get("description", tool_input.get("prompt", ""))[:80]
        return "Launch sub-agent: %s" % desc

    if tool_name == "WebFetch":
        url = tool_input.get("url", "")[:60]
        return "Fetch web page: %s" % url

    if tool_name == "WebSearch":
        query = tool_input.get("query", "")[:60]
        return "Web search: %s" % query

    if tool_name in ("Task", "TaskCreate"):
        return "Create background task"

    if tool_name == "Skill":
        skill = tool_input.get("skill", "")
        return "Invoke skill: %s" % skill

    return "%s tool call" % tool_name


def _explain_decision(action: str, reason: str, tool_name: str) -> str:
    """Generate a human-readable explanation of why this decision was made."""
    if action == "allow":
        if "Always-allow" in reason:
            return "%s is a safe read-only tool, always permitted" % tool_name
        if "Allowed command" in reason:
            cmd = reason.replace("Allowed command: ", "")
            return "'%s' is in the approved commands list" % cmd
        if "within project" in reason.lower():
            return "File is inside the project directory, safe to modify"
        if "Task management" in reason:
            return "Task/agent management is always permitted"
        return "Allowed: %s" % reason

    if action == "block":
        if "Blocked pattern" in reason:
            pattern = reason.replace("Blocked pattern: ", "")
            return "BLOCKED: '%s' matches a dangerous command pattern" % pattern
        if "outside project" in reason.lower():
            return "BLOCKED: file is outside the project sandbox"
        return "BLOCKED: %s" % reason

    if action == "pass":
        if "Requires approval" in reason:
            return "This action needs your explicit approval per the rules"
        if "Unknown command" in reason:
            cmd = reason.replace("Unknown command: ", "")
            return "'%s' is not in the approved list - asking you" % cmd
        if "Unknown tool" in reason:
            return "Unrecognized tool - deferring to you"
        return "Asking you: %s" % reason

    return reason


def _sanitize_input(tool_input: dict) -> dict:
    """Store full tool input but sanitize secrets and truncate long values."""
    sanitized = {}
    secret_keys = {'key', 'secret', 'password', 'token', 'credential', 'passphrase'}
    for k, v in tool_input.items():
        if any(s in k.lower() for s in secret_keys):
            sanitized[k] = '[REDACTED]'
        elif isinstance(v, str) and len(v) > 500:
            sanitized[k] = v[:500] + '...[truncated]'
        else:
            sanitized[k] = v
    return sanitized


def _sanitize_output(output) -> str:
    """Sanitize tool output for logging. Truncate if very long."""
    if output is None:
        return ""
    s = str(output)
    if len(s) > 2000:
        return s[:2000] + "...[truncated]"
    return s


def _track_session(session_id, name=None, project=None):
    """Register or update an active session."""
    state = load_state()
    sessions = state.get("active_sessions", {})
    now = datetime.now(timezone.utc).isoformat()

    if session_id not in sessions:
        sessions[session_id] = {
            "name": name or state.get("task") or "Session %s" % session_id[:8],
            "project": project or PROJECT_ROOT.name,
            "first_seen": now,
            "last_seen": now,
            "decision_count": 0,
            "enabled": True,
        }
    else:
        sessions[session_id]["last_seen"] = now
        sessions[session_id]["decision_count"] = sessions[session_id].get("decision_count", 0) + 1
        if name:
            sessions[session_id]["name"] = name

    state["active_sessions"] = sessions
    save_state(state)


def _scan_claude_processes():
    """Detect running Claude Code sessions.

    Filters to claude.exe processes that:
    - Are actual claude.exe (not bash subprocesses mentioning 'claude')
    - Are less than 24 hours old (skip zombies)
    """
    if not PSUTIL_AVAILABLE:
        return []

    import time as _time
    now = _time.time()
    sessions = []
    seen_pids = set()

    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd', 'create_time']):
        try:
            name = proc.info.get('name', '') or ''
            if name.lower() != 'claude.exe':
                continue

            pid = proc.info['pid']
            if pid in seen_pids:
                continue
            seen_pids.add(pid)

            age_hours = (now - proc.info.get('create_time', 0)) / 3600
            if age_hours > 24:
                continue

            cwd = proc.info.get('cwd', '') or ''
            cmdline = proc.info.get('cmdline') or []
            cmd_str = ' '.join(cmdline)

            project = Path(cwd).name if cwd else 'unknown'

            import datetime as _dt
            created = _dt.datetime.fromtimestamp(proc.info.get('create_time', 0))

            sessions.append({
                'pid': pid,
                'project': project,
                'cwd': cwd,
                'created': created.isoformat(),
                'age': '%.0fh ago' % age_hours if age_hours >= 1 else '%.0fm ago' % (age_hours * 60),
                'cmdline_preview': cmd_str[:200],
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    sessions.sort(key=lambda s: s.get('created', ''), reverse=True)
    return sessions


def log_response(tool_name, tool_input, tool_output, event="PostToolUse"):
    """Log the response/result from a completed tool call."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    session_id = _get_session_id()
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "tool": tool_name,
        "action": "response",
        "description": _describe_tool_action(tool_name, tool_input or {}),
        "tool_input": _sanitize_input(tool_input or {}),
        "tool_output": _sanitize_output(tool_output),
        "session_id": session_id,
        "project": PROJECT_ROOT.name,
        "working_dir": str(PROJECT_ROOT),
    }
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    _track_session(session_id)


def log_decision(tool_name: str, action: str, reason: str, tool_input: dict = None, event: str = "PreToolUse") -> None:
    """Append a decision to the JSONL log with full context."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    description = _describe_tool_action(tool_name, tool_input or {})
    rationale = _explain_decision(action, reason, tool_name)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "tool": tool_name,
        "action": action,
        "reason": reason,
        "description": description,
        "rationale": rationale,
        "session_id": _get_session_id(),
        "project": PROJECT_ROOT.name,
        "tool_input": _sanitize_input(tool_input or {}),
        "working_dir": str(PROJECT_ROOT),
    }

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    state = load_state()
    state["decisions_count"] = state.get("decisions_count", 0) + 1
    save_state(state)
    _track_session(_get_session_id())


# ── Decision logic ──────────────────────────────────────────────────────

def is_path_in_project(file_path: str) -> bool:
    """Check if a file path is within the project root."""
    try:
        resolved = Path(file_path).resolve()
        return str(resolved).lower().startswith(str(PROJECT_ROOT).lower())
    except (OSError, ValueError):
        return False


def decide_bash(command: str) -> tuple[str, str]:
    """
    Decide on a Bash command.
    Returns (action, reason) where action is 'allow', 'block', or 'pass'.
    """
    cmd_lower = command.lower().strip()

    # Check block patterns first (most specific)
    for pattern in BASH_BLOCK_PATTERNS:
        if pattern.lower() in cmd_lower:
            return "block", f"Blocked pattern: {pattern}"

    # Check ask-user patterns (before allow, since 'git push' contains 'git')
    for pattern in BASH_ASK_PATTERNS:
        if pattern.lower() in cmd_lower:
            return "pass", f"Requires approval: {pattern}"

    # Check first word of command against allowlist
    first_word = _extract_first_command(cmd_lower)
    if first_word in BASH_ALLOW_FIRST:
        return "allow", f"Allowed command: {first_word}"

    # Unknown command — pass through to user
    return "pass", f"Unknown command: {first_word}"


def _extract_first_command(cmd: str) -> str:
    """Extract the first meaningful command word from a shell command string."""
    cmd = cmd.strip()

    # Skip env var assignments (FOO=bar cmd ...)
    parts = cmd.split()
    for part in parts:
        if "=" in part and not part.startswith("-"):
            continue
        # Strip path prefix (e.g., /usr/bin/python -> python)
        basename = part.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        # Strip .exe suffix on Windows
        if basename.endswith(".exe"):
            basename = basename[:-4]
        return basename

    return cmd.split()[0] if cmd.split() else ""


def decide_edit_write(tool_input: dict) -> tuple[str, str]:
    """Decide on Edit or Write tool calls."""
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return "allow", "No file_path specified"

    if is_path_in_project(file_path):
        return "allow", "File within project root"
    else:
        return "block", f"File outside project root: {file_path}"


def decide(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """
    Main decision function.
    Returns (action, reason):
      - 'allow': exit 0, no output
      - 'block': exit 2, reason on stderr
      - 'pass': exit 0, no output (fall through to user prompt)
    """
    if tool_name in ALWAYS_ALLOW_TOOLS:
        return "allow", f"Always-allow tool: {tool_name}"

    if tool_name == "Bash":
        command = tool_input.get("command", "")
        if not command:
            return "allow", "Empty bash command"
        return decide_bash(command)

    if tool_name in ("Edit", "Write"):
        return decide_edit_write(tool_input)

    if tool_name in ("Task", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate"):
        return "allow", f"Task management tool: {tool_name}"

    return "pass", f"Unknown tool: {tool_name}"


# ── CLI: --decide ────────────────────────────────────────────────────────

def cmd_decide(event: str) -> None:
    """Read tool call JSON from stdin, make decision, exit with code."""
    state = load_state()
    if not state.get("enabled", False):
        sys.exit(0)

    active = state.get("active_sessions", {})
    current_sid = state.get("session_id", "")
    if current_sid and current_sid in active:
        if not active[current_sid].get("enabled", True):
            sys.exit(0)

    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    if event == "PostToolUse":
        tool_output = data.get("tool_output", "")
        log_response(tool_name, tool_input, tool_output, event=event)
        sys.exit(0)

    action, reason = decide(tool_name, tool_input)
    log_decision(tool_name, action, reason, tool_input, event=event)

    if action == "block":
        state = load_state()
        state["error_count"] = state.get("error_count", 0) + 1
        if state["error_count"] >= state.get("max_errors", 3):
            state["enabled"] = False
            save_state(state)
            print(f"YESBOT BLOCKED: {reason} (disabled after {state['error_count']} blocks)", file=sys.stderr)
        else:
            save_state(state)
            print(f"YESBOT BLOCKED: {reason}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


# ── CLI: --install / --uninstall ─────────────────────────────────────────

HOOK_MARKER = "bash .claude/hooks/yesbot-hook.sh"

HOOK_ENTRY = {
    "matcher": "",
    "hooks": [{
        "type": "command",
        "command": "bash .claude/hooks/yesbot-hook.sh",
        "timeout": 5000,
    }],
}

POST_HOOK_ENTRY = {
    "matcher": "",
    "hooks": [{
        "type": "command",
        "command": "CLAUDE_HOOK_EVENT=PostToolUse bash .claude/hooks/yesbot-hook.sh",
        "timeout": 5000,
    }],
}


def cmd_install(dry_run: bool = False) -> None:
    """Add YesBot hooks to settings.local.json."""
    # Copy hook script to .claude/hooks/
    hooks_dir = PROJECT_ROOT / '.claude' / 'hooks'
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_src = SCRIPT_DIR / 'yesbot-hook.sh'
    hook_dst = hooks_dir / 'yesbot-hook.sh'
    if hook_src.exists():
        shutil.copy2(str(hook_src), str(hook_dst))
        print(f"Copied hook script to {hook_dst}")
    else:
        print(f"Warning: {hook_src} not found. Create it or copy yesbot-hook.sh to .claude/hooks/ manually.", file=sys.stderr)

    if SETTINGS_FILE.exists():
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    else:
        settings = {}

    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    post = hooks.setdefault("PostToolUse", [])

    pre_installed = False
    for entry in pre:
        for h in entry.get("hooks", []):
            if HOOK_MARKER in h.get("command", ""):
                pre_installed = True

    post_installed = False
    for entry in post:
        for h in entry.get("hooks", []):
            if HOOK_MARKER in h.get("command", ""):
                post_installed = True

    if pre_installed and post_installed:
        print("YesBot hooks already installed.")
        return

    if not pre_installed:
        pre.append(HOOK_ENTRY)
    if not post_installed:
        post.append(POST_HOOK_ENTRY)

    if dry_run:
        print("Would write to settings.local.json:")
        print(json.dumps(settings, indent=2))
    else:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print(f"YesBot hooks installed in {SETTINGS_FILE} (PreToolUse + PostToolUse)")


def cmd_uninstall() -> None:
    """Remove YesBot hooks from settings.local.json."""
    if not SETTINGS_FILE.exists():
        print("No settings file found.")
        return

    settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    hooks = settings.get("hooks", {})
    removed = False

    for hook_type in ("PreToolUse", "PostToolUse"):
        entries = hooks.get(hook_type, [])
        new_entries = []
        for entry in entries:
            is_yesbot = False
            for h in entry.get("hooks", []):
                if HOOK_MARKER in h.get("command", ""):
                    is_yesbot = True
                    removed = True
            if not is_yesbot:
                new_entries.append(entry)
        hooks[hook_type] = new_entries

    if removed:
        SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        print("YesBot hooks removed from settings.local.json")
    else:
        print("YesBot hooks not found in settings.")


# ── CLI: --on / --off / --status / --log ─────────────────────────────────

def cmd_on() -> None:
    """Enable YesBot."""
    import uuid
    state = load_state()
    state["enabled"] = True
    state["enabled_at"] = datetime.now(timezone.utc).isoformat()
    state["error_count"] = 0
    state["session_id"] = str(uuid.uuid4())[:8]
    state["session_name"] = "Session %s" % state["session_id"][:8]
    save_state(state)
    _track_session(state["session_id"], name=state["session_name"])
    print("YesBot ENABLED — session: %s" % state["session_name"])


def cmd_off() -> None:
    """Disable YesBot."""
    state = load_state()
    state["enabled"] = False
    save_state(state)
    print("YesBot DISABLED")


def cmd_status() -> None:
    """Show current state."""
    state = load_state()
    print(json.dumps(state, indent=2))


def cmd_log() -> None:
    """Show last 20 decisions."""
    if not LOG_FILE.exists():
        print("No decisions logged yet.")
        return

    lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-20:]
    for line in recent:
        try:
            entry = json.loads(line)
            ts = entry.get("ts", "?")[:19]
            tool = entry.get("tool", "?")
            action = entry.get("action", "?")
            reason = entry.get("reason", "")
            cmd = entry.get("command", "")
            display = f"[{ts}] {action:5s} {tool}"
            if cmd:
                display += f" — {cmd[:60]}"
            elif reason:
                display += f" — {reason[:60]}"
            print(display)
        except json.JSONDecodeError:
            print(line)


# ── CLI: --test ──────────────────────────────────────────────────────────

def cmd_test(tool_name: str, tool_input_json: str) -> None:
    """Test a hypothetical tool call without logging."""
    try:
        tool_input = json.loads(tool_input_json)
    except json.JSONDecodeError:
        print(f"Invalid JSON: {tool_input_json}", file=sys.stderr)
        sys.exit(1)

    action, reason = decide(tool_name, tool_input)
    print(f"Action: {action}")
    print(f"Reason: {reason}")
    if action == "block":
        sys.exit(2)
    else:
        sys.exit(0)


# ── CLI: --dashboard ─────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YesBot &mdash; Live Dashboard</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0a0a0f; color: #e0e0e0; font-family: 'Consolas', 'Courier New', monospace; }
.header { border-bottom: 1px solid #1a3a4a; padding: 12px 20px; display: flex; align-items: center; gap: 12px; }
.header h1 { font-size: 16px; font-weight: bold; color: #38bdf8; flex: 1; }
.status-dot-lg { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.status-on { background: #22c55e; box-shadow: 0 0 8px #22c55e; }
.status-off { background: #ef4444; }
.status-label { font-size: 12px; font-weight: bold; }
.task-bar { padding: 8px 20px; background: #0d1117; border-bottom: 1px solid #1a3a4a; font-size: 12px; color: #666; display: flex; gap: 24px; flex-wrap: wrap; }
.task-bar .task-item { display: flex; gap: 6px; }
.task-bar .task-label { color: #555; }
.task-bar .task-value { color: #e0e0e0; }
.task-bar .task-value.muted { color: #888; }
.controls { padding: 10px 20px; border-bottom: 1px solid #1a3a4a; }
.control-row { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
.btn { padding: 6px 14px; border: 1px solid #333; border-radius: 4px; cursor: pointer; font-family: inherit; font-size: 12px; }
.btn-on { background: #052e16; color: #22c55e; border-color: #166534; }
.btn-off { background: #2d0a0a; color: #ef4444; border-color: #991b1b; }
.btn-secondary { background: #1a1a2e; color: #888; border-color: #333; }
.btn-small { padding: 3px 8px; font-size: 11px; }
.btn-all-on { background: #052e16; color: #22c55e; border-color: #166534; }
.btn-all-off { background: #2d0a0a; color: #ef4444; border-color: #991b1b; }
.btn-add { background: #1a1a2e; color: #38bdf8; border-color: #1e3a5f; }
input[type="text"] { background: #0d1117; border: 1px solid #333; color: #e0e0e0; padding: 6px 10px; border-radius: 4px; flex: 1; font-family: inherit; font-size: 12px; }
.label { color: #666; font-size: 11px; text-transform: uppercase; min-width: 60px; }
.layout { display: flex; height: calc(100vh - 90px); }
.sidebar { width: 220px; border-right: 1px solid #1a3a4a; overflow-y: auto; flex-shrink: 0; background: #0a0a0f; }
.sidebar-header { padding: 10px 12px; font-size: 11px; color: #555; text-transform: uppercase; border-bottom: 1px solid #1a3a4a; letter-spacing: 0.05em; }
.sidebar-actions { padding: 8px 12px; border-bottom: 1px solid #1a3a4a; display: flex; gap: 4px; }
.session-item { padding: 10px 12px; border-bottom: 1px solid #111; cursor: pointer; display: flex; align-items: center; gap: 8px; }
.session-item:hover { background: #0d1117; }
.session-item.active { background: #0d1520; border-left: 2px solid #38bdf8; }
.session-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.session-dot.on { background: #22c55e; }
.session-dot.off { background: #555; }
.session-info { flex: 1; min-width: 0; }
.session-name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-meta { font-size: 10px; color: #555; margin-top: 2px; }
.session-count { font-size: 10px; color: #555; background: #1a1a1a; padding: 1px 6px; border-radius: 8px; }
.toggle-btn { padding: 2px 8px; border-radius: 3px; font-size: 10px; cursor: pointer; border: 1px solid; font-weight: bold; }
.toggle-btn.on { background: #052e16; color: #22c55e; border-color: #166534; }
.toggle-btn.off { background: #1a1a1a; color: #555; border-color: #333; }
.session-detail { padding: 8px 12px 8px 28px; background: #060810; border-bottom: 1px solid #111; }
.preview-label { font-size: 10px; color: #444; text-transform: uppercase; margin-bottom: 4px; }
.preview-entry { font-size: 11px; color: #888; }
.preview-result { font-size: 11px; color: #555; padding-left: 12px; }
.sidebar-section { border-top: 1px solid #1a3a4a; }
.sidebar-footer { padding: 8px 12px; border-top: 1px solid #1a3a4a; display: flex; gap: 6px; }
.process-item { padding: 8px 12px; display: flex; align-items: center; gap: 8px; font-size: 11px; }
.process-dot { width: 6px; height: 6px; border-radius: 50%; background: #38bdf8; flex-shrink: 0; }
.process-name { color: #888; }
.process-meta { font-size: 10px; color: #444; }
.main-content { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
.stats { display: flex; gap: 24px; padding: 12px 20px; border-bottom: 1px solid #1a3a4a; }
.stat { text-align: center; }
.stat-value { font-size: 24px; font-weight: bold; color: #38bdf8; }
.stat-label { font-size: 11px; color: #666; text-transform: uppercase; }
.log-wrap { padding: 0 20px 8px; flex: 1; overflow-y: auto; }
.log-header { display: grid; grid-template-columns: 130px 120px 80px 65px 1fr 1fr; gap: 8px; padding: 6px 0; border-bottom: 1px solid #2a3a4a; font-size: 11px; color: #555; text-transform: uppercase; letter-spacing: 0.05em; position: sticky; top: 0; background: #0a0a0f; z-index: 10; }
.decision { display: grid; grid-template-columns: 130px 120px 80px 65px 1fr 1fr; gap: 8px; padding: 7px 0; border-bottom: 1px solid #111; font-size: 12px; align-items: start; cursor: pointer; }
.sortable { cursor: pointer; user-select: none; }
.sortable:hover { color: #38bdf8; }
.sort-arrow { font-size: 10px; color: #38bdf8; }
.decision:hover { background: #0d1117; }
.response-row { background: #060810; font-size: 11px; cursor: default; border-bottom: 1px solid #0a0a0f; }
.response-row:hover { background: #080c14; }
.col-time { color: #555; white-space: nowrap; }
.col-tool { color: #38bdf8; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.col-session { color: #a0a0c0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.col-response { color: #666; grid-column: 5 / -1; padding-left: 8px; word-break: break-word; line-height: 1.4; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 11px; font-weight: bold; letter-spacing: 0.05em; }
.badge-allow { background: #052e16; color: #22c55e; border: 1px solid #166534; }
.badge-block { background: #2d0a0a; color: #ef4444; border: 1px solid #991b1b; }
.badge-pass  { background: #2d2200; color: #eab308; border: 1px solid #854d0e; }
.badge-response { background: #0a0a1a; color: #4a5568; border: 1px solid #1a2a3a; }
.col-desc { color: #c0c0c0; word-break: break-word; line-height: 1.4; }
.col-rationale { color: #888; word-break: break-word; line-height: 1.4; font-size: 11px; }
.empty { color: #444; padding: 24px 0; text-align: center; }
.detail-panel { position: fixed; right: 0; top: 0; width: 45%; height: 100vh; background: #0d1117; border-left: 1px solid #1a3a4a; padding: 16px; overflow-y: auto; z-index: 100; }
.detail-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; color: #38bdf8; }
.detail-panel pre { color: #c0c0c0; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
.badge.clickable { cursor: pointer; }
.badge.clickable:hover { opacity: 0.8; box-shadow: 0 0 6px rgba(255,255,255,0.2); }
.rule-menu { position: fixed; background: #1a1a2e; border: 1px solid #2a3a4a; border-radius: 6px; padding: 4px 0; z-index: 200; box-shadow: 0 4px 16px rgba(0,0,0,0.5); min-width: 160px; }
.rule-menu-title { padding: 6px 12px; font-size: 11px; color: #555; border-bottom: 1px solid #2a3a4a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 250px; }
.rule-option { padding: 8px 12px; cursor: pointer; font-size: 12px; }
.rule-option:hover { background: #0d1117; }
.rule-option.allow { color: #22c55e; }
.rule-option.block { color: #ef4444; }
.rule-option.ask { color: #eab308; }
</style>
</head>
<body>
<div class="header">
  <h1>YesBot &mdash; Live Dashboard</h1>
  <span class="status-dot-lg status-off" id="status-dot"></span>
  <span class="status-label" id="status-text">LOADING</span>
</div>
<div class="task-bar">
  <div class="task-item"><span class="task-label">Session:</span><span class="task-value muted" id="session-text">&mdash;</span></div>
  <div class="task-item"><span class="task-label">Project:</span><span class="task-value muted" id="project-text">&mdash;</span></div>
</div>
<div class="controls">
  <div class="control-row">
    <button id="btn-toggle" class="btn btn-off" onclick="toggleYesBot()" style="min-width:120px;font-size:14px;padding:8px 20px;">DISABLED</button>
  </div>
</div>
<div class="layout">
  <div class="sidebar">
    <div class="sidebar-header">SESSIONS</div>
    <div id="session-list"></div>
    <div class="sidebar-section">
      <div class="sidebar-header">RUNNING PROCESSES</div>
      <div id="process-list"></div>
    </div>
    <div class="sidebar-footer">
      <button class="btn btn-small btn-all-on" onclick="allSessionsOn()">All ON</button>
      <button class="btn btn-small btn-all-off" onclick="allSessionsOff()">All OFF</button>
    </div>
  </div>
  <div class="main-content">
    <div class="stats">
      <div class="stat"><div class="stat-value" id="stat-total">0</div><div class="stat-label">Total</div></div>
      <div class="stat"><div class="stat-value" id="stat-allow" style="color:#22c55e">0</div><div class="stat-label">Allow</div></div>
      <div class="stat"><div class="stat-value" id="stat-block" style="color:#ef4444">0</div><div class="stat-label">Block</div></div>
      <div class="stat"><div class="stat-value" id="stat-pass" style="color:#eab308">0</div><div class="stat-label">Pass</div></div>
      <div class="stat"><div class="stat-value" id="stat-errors" style="color:#f97316">0</div><div class="stat-label">Errors</div></div>
    </div>
    <div class="log-wrap">
      <div class="log-header">
        <span class="sortable" onclick="sortBy('ts')">DATE/TIME <span class="sort-arrow" id="sort-ts">&#9660;</span></span>
        <span class="sortable" onclick="sortBy('session')">SESSION <span class="sort-arrow" id="sort-session"></span></span>
        <span class="sortable" onclick="sortBy('tool')">TOOL <span class="sort-arrow" id="sort-tool"></span></span>
        <span class="sortable" onclick="sortBy('action')">ACTION <span class="sort-arrow" id="sort-action"></span></span>
        <span>DESCRIPTION</span>
        <span>RATIONALE</span>
      </div>
      <div id="log"><div class="empty">Waiting for decisions...</div></div>
    </div>
  </div>
</div>
<div id="detail-panel" class="detail-panel" style="display:none">
  <div class="detail-header">
    <span>Decision Detail</span>
    <button onclick="hideDetail()" class="btn btn-small btn-secondary">Close</button>
  </div>
  <pre id="detail-content"></pre>
</div>
<div id="rule-menu" class="rule-menu" style="display:none">
  <div class="rule-menu-title" id="rule-menu-title">Change rule for: ...</div>
  <div class="rule-option allow" onclick="setRule('allow')">Always Allow</div>
  <div class="rule-option block" onclick="setRule('block')">Always Block</div>
  <div class="rule-option ask" onclick="setRule('ask')">Ask Me</div>
</div>
<script>
function esc(s) {
    var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;
}

function truncate(s, max) {
    if (!s) return '';
    if (s.length <= max) return s;
    return s.substring(0, max) + '...';
}

var _ruleMenuTool = '';
var _ruleMenuPattern = '';

function showRuleMenu(event, tool, pattern) {
    event.stopPropagation();
    var menu = document.getElementById('rule-menu');
    var title = document.getElementById('rule-menu-title');
    _ruleMenuTool = tool;
    _ruleMenuPattern = pattern;
    title.textContent = tool + ': ' + pattern.substring(0, 40);
    menu.style.display = 'block';
    menu.style.left = Math.min(event.clientX, window.innerWidth - 180) + 'px';
    menu.style.top = Math.min(event.clientY, window.innerHeight - 120) + 'px';
}

function setRule(target) {
    fetch('/api/rules/update', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tool: _ruleMenuTool, pattern: _ruleMenuPattern, target: target})
    }).then(function(r) { return r.json(); }).then(function(data) {
        if (data.ok) {
            var menu = document.getElementById('rule-menu');
            menu.innerHTML = '<div style="padding:12px;color:#22c55e;">Updated!<\/div>';
            setTimeout(function() { menu.style.display = 'none'; refresh(); }, 800);
        }
    });
}

document.addEventListener('click', function() {
    document.getElementById('rule-menu').style.display = 'none';
});

function _extractPattern(d) {
    if (d.tool === 'Bash') {
        var cmd = (d.command || d.description || '').replace('Run shell command: ', '');
        var words = cmd.split(/\s+/);
        return words.slice(0, Math.min(2, words.length)).join(' ');
    }
    return d.tool || '';
}

var _allDecisions = [];
var _selectedSession = null;
var _sortCol = 'ts';
var _sortDir = 'desc';

function _getSessionName(sid, activeSessions) {
    if (!sid) return '\u2014';
    var sess = activeSessions && activeSessions[sid];
    if (sess && sess.name) return sess.name;
    return sid.substring(0, 8);
}

function sortBy(col) {
    if (_sortCol === col) {
        _sortDir = _sortDir === 'desc' ? 'asc' : 'desc';
    } else {
        _sortCol = col;
        _sortDir = col === 'ts' ? 'desc' : 'asc';
    }
    document.querySelectorAll('.sort-arrow').forEach(function(el) { el.textContent = ''; });
    var arrow = document.getElementById('sort-' + col);
    if (arrow) arrow.textContent = _sortDir === 'desc' ? '\u25bc' : '\u25b2';
    refresh();
}

function showDetail(idx) {
    var d = _allDecisions[idx];
    if (!d) return;
    document.getElementById('detail-content').textContent = JSON.stringify(d, null, 2);
    document.getElementById('detail-panel').style.display = 'block';
}

function hideDetail() {
    document.getElementById('detail-panel').style.display = 'none';
}

function selectSession(sid) {
    if (_selectedSession === sid) {
        _selectedSession = null;
    } else {
        _selectedSession = sid;
        fetch('/api/sessions/' + encodeURIComponent(sid) + '/latest')
            .then(function(r) { return r.json(); })
            .then(function(entries) {
                var el = document.getElementById('preview-' + sid);
                if (el && entries.length > 0) {
                    var last = entries[entries.length - 1];
                    el.innerHTML = '<div class="preview-label">Last activity:<\/div>'
                        + '<div class="preview-entry">' + esc((last.action||'').toUpperCase() + ' ' + (last.tool||'') + ': ' + (last.description||'')) + '<\/div>'
                        + (last.tool_output ? '<div class="preview-result">' + esc(String(last.tool_output).substring(0, 150)) + '<\/div>' : '');
                } else if (el) {
                    el.innerHTML = '<div class="preview-label">No activity yet<\/div>';
                }
            })
            .catch(function() {});
    }
    refresh();
}

function renameSession(sid, currentName) {
    var newName = prompt('Rename session:', currentName || '');
    if (newName === null || newName === currentName) return;
    fetch('/api/sessions/rename', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({session_id: sid, name: newName})
    }).then(function() { refresh(); });
}

function toggleYesBot() {
    fetch('/api/status').then(function(r){return r.json();}).then(function(status) {
        return fetch('/api/toggle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: !status.enabled})
        });
    }).then(function() { refresh(); });
}

function toggleSessionPilot(sid) {
    fetch('/api/sessions').then(function(r){return r.json();}).then(function(data) {
        var active = data.active || {};
        var current = active[sid];
        var newState = !(current && current.enabled);
        return fetch('/api/sessions/toggle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session_id: sid, enabled: newState})
        });
    }).then(function() { refresh(); });
}

function allSessionsOn() {
    fetch('/api/sessions').then(function(r){return r.json();}).then(function(data) {
        var active = data.active || {};
        var promises = Object.keys(active).map(function(sid) {
            return fetch('/api/sessions/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: sid, enabled: true})
            });
        });
        return Promise.all(promises);
    }).then(function() { refresh(); });
}

function allSessionsOff() {
    fetch('/api/sessions').then(function(r){return r.json();}).then(function(data) {
        var active = data.active || {};
        var promises = Object.keys(active).map(function(sid) {
            return fetch('/api/sessions/toggle', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: sid, enabled: false})
            });
        });
        return Promise.all(promises);
    }).then(function() { refresh(); });
}

function addSession() {
    var label = prompt('Session label:');
    if (!label) return;
    var projectDir = prompt('Project directory (leave empty for current):', '') || '';
    fetch('/api/sessions/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({label: label, project_dir: projectDir})
    }).then(function() { refresh(); });
}

function _timeSince(isoStr) {
    if (!isoStr) return '';
    var secs = (Date.now() - new Date(isoStr).getTime()) / 1000;
    if (secs < 60) return 'just now';
    if (secs < 3600) return Math.floor(secs/60) + 'min ago';
    if (secs < 86400) return Math.floor(secs/3600) + 'hr ago';
    return Math.floor(secs/86400) + 'd ago';
}

function renderSidebar(active, processes) {
    var list = document.getElementById('session-list');
    var entries = Object.entries(active).sort(function(a, b) {
        return (b[1].last_seen || '').localeCompare(a[1].last_seen || '');
    });

    list.innerHTML = entries.map(function(pair) {
        var sid = pair[0];
        var info = pair[1];
        var isSelected = sid === _selectedSession;
        var dotCls = info.enabled !== false ? 'on' : 'off';
        var toggleCls = info.enabled !== false ? 'on' : 'off';
        var toggleLabel = info.enabled !== false ? 'ON' : 'OFF';
        var count = info.decision_count || 0;
        var name = info.name || 'Session ' + sid.substring(0, 8);
        var timeSince = _timeSince(info.last_seen);

        return '<div class="session-item' + (isSelected ? ' active' : '') + '" data-sid="' + sid + '" data-name="' + esc(name) + '" onclick="selectSession(this.dataset.sid)" ondblclick="renameSession(this.dataset.sid, this.dataset.name)">'
            + '<span class="session-dot ' + dotCls + '"><\/span>'
            + '<div class="session-info">'
            + '<div class="session-name">' + esc(name) + '<\/div>'
            + '<div class="session-meta">' + count + ' decisions' + (timeSince ? ' \u00b7 ' + timeSince : '') + '<\/div>'
            + '<\/div>'
            + '<button class="toggle-btn ' + toggleCls + '" data-sid="' + sid + '" onclick="event.stopPropagation(); toggleSessionPilot(this.dataset.sid)">' + toggleLabel + '<\/button>'
            + '<\/div>'
            + '<div class="session-detail" id="detail-' + esc(sid) + '" style="display:' + (isSelected ? 'block' : 'none') + '">'
            + '<div id="preview-' + esc(sid) + '" class="session-preview"><div class="preview-label">Loading...<\/div><\/div>'
            + '<\/div>';
    }).join('');

    if (entries.length === 0) {
        list.innerHTML = '<div style="padding:12px;color:#444;font-size:11px;">No sessions yet<\/div>';
    }

    renderProcesses(_cachedProcesses);
}

function renderProcesses(processes) {
    var plist = document.getElementById('process-list');
    if (!plist) return;
    if (!processes || processes.length === 0) {
        plist.innerHTML = '<div class="process-item"><span class="process-meta">No Claude Code processes detected<\/span><\/div>';
    } else {
        plist.innerHTML = processes.map(function(p) {
            return '<div class="process-item">'
                + '<span class="process-dot"><\/span>'
                + '<div><div class="process-name">' + esc(p.project || p.name || 'claude') + '<\/div>'
                + '<div class="process-meta">PID ' + p.pid + ' \u00b7 ' + esc(p.age || '') + '<\/div><\/div>'
                + '<\/div>';
        }).join('');
    }
}

var _cachedProcesses = [];

function refreshProcesses() {
    fetch('/api/processes').then(function(r){return r.json();}).then(function(procs) {
        _cachedProcesses = procs;
        renderProcesses(procs);
    }).catch(function(){});
}

function refresh() {
    Promise.all([
        fetch('/api/decisions?limit=100').then(function(r){return r.json();}),
        fetch('/api/status').then(function(r){return r.json();}),
        fetch('/api/sessions').then(function(r){return r.json();})
    ]).then(function(results) {
        var decisions = results[0];
        var status = results[1];
        var sessData = results[2];
        var processes = _cachedProcesses;
        var activeSessions = sessData.active || {};

        document.getElementById('status-dot').className =
            'status-dot-lg ' + (status.enabled ? 'status-on' : 'status-off');
        document.getElementById('status-text').textContent =
            status.enabled ? 'ENABLED' : 'DISABLED';
        document.getElementById('session-text').textContent =
            (status.session_name || status.session_id || '') + (status.session_id ? ' (' + status.session_id + ')' : '');
        document.getElementById('project-text').textContent =
            status.project || '';

        var btn = document.getElementById('btn-toggle');
        btn.textContent = status.enabled ? 'ENABLED' : 'DISABLED';
        btn.className = 'btn ' + (status.enabled ? 'btn-on' : 'btn-off');

        renderSidebar(activeSessions, processes);

        var filtered = decisions;
        if (_selectedSession) {
            filtered = decisions.filter(function(d) { return d.session_id === _selectedSession; });
        }
        var nonResponse = filtered.filter(function(d) { return d.action !== 'response'; });
        document.getElementById('stat-total').textContent = nonResponse.length;
        document.getElementById('stat-allow').textContent =
            nonResponse.filter(function(d) { return d.action === 'allow'; }).length;
        document.getElementById('stat-block').textContent =
            nonResponse.filter(function(d) { return d.action === 'block'; }).length;
        document.getElementById('stat-pass').textContent =
            nonResponse.filter(function(d) { return d.action === 'pass'; }).length;
        document.getElementById('stat-errors').textContent =
            status.error_count || 0;

        var log = document.getElementById('log');
        var sorted = filtered.slice();
        sorted.sort(function(a, b) {
            var aVal, bVal;
            if (_sortCol === 'ts') {
                aVal = a.ts || ''; bVal = b.ts || '';
            } else if (_sortCol === 'session') {
                aVal = _getSessionName(a.session_id, activeSessions);
                bVal = _getSessionName(b.session_id, activeSessions);
            } else if (_sortCol === 'tool') {
                aVal = a.tool || ''; bVal = b.tool || '';
            } else if (_sortCol === 'action') {
                aVal = a.action || ''; bVal = b.action || '';
            } else {
                aVal = ''; bVal = '';
            }
            if (aVal < bVal) return _sortDir === 'asc' ? -1 : 1;
            if (aVal > bVal) return _sortDir === 'asc' ? 1 : -1;
            return 0;
        });
        _allDecisions = sorted;
        if (sorted.length === 0) {
            log.innerHTML = '<div class="empty">No decisions yet.<\/div>';
        } else {
            log.innerHTML = sorted.map(function(d, idx) {
                var dt = d.ts ? new Date(d.ts) : null;
                var dateStr = dt ? (dt.getMonth()+1) + '\/' + dt.getDate() + ' ' + dt.toTimeString().split(' ')[0] : '\u2014';
                if (d.action === 'response') {
                    var output = d.tool_output || '';
                    return '<div class="decision response-row" onclick="showDetail(' + idx + ')">'
                        + '<span><\/span><span><\/span><span><\/span>'
                        + '<span class="col-tool" style="color:#4a5568;font-size:11px;">&#8627; result<\/span>'
                        + '<span class="col-response" style="grid-column: 5 \/ -1;">' + esc(truncate(output, 200)) + '<\/span>'
                        + '<\/div>';
                }
                var badgeCls = d.action === 'allow' ? 'badge-allow' :
                               d.action === 'block' ? 'badge-block' : 'badge-pass';
                var actionLabel = (d.action || '?').toUpperCase();
                var toolName = d.tool || '?';
                var sessionName = _getSessionName(d.session_id, activeSessions);
                var description = d.description || d.reason || '';
                var rationale = d.rationale || d.reason || '';
                var rulePattern = _extractPattern(d);
                if (rulePattern.length > 60) rulePattern = rulePattern.substring(0, 60);
                var rulePatternAttr = esc(rulePattern);
                var ruleToolAttr = esc(d.tool || '');
                return '<div class="decision" onclick="showDetail(' + idx + ')">'
                    + '<span class="col-time">' + esc(dateStr) + '<\/span>'
                    + '<span class="col-session">' + esc(sessionName) + '<\/span>'
                    + '<span class="col-tool">' + esc(toolName) + '<\/span>'
                    + '<span><span class="badge ' + badgeCls + ' clickable" data-tool="' + ruleToolAttr + '" data-pattern="' + rulePatternAttr + '" onclick="showRuleMenu(event, this.dataset.tool, this.dataset.pattern)">' + esc(actionLabel) + '<\/span><\/span>'
                    + '<span class="col-desc">' + esc(description) + '<\/span>'
                    + '<span class="col-rationale">' + esc(rationale) + '<\/span>'
                    + '<\/div>';
            }).join('');
        }
    }).catch(function(e) {
        console.error('Refresh error:', e);
    });
}

setInterval(refresh, 5000);
setInterval(refreshProcesses, 30000);
refresh();
refreshProcesses();
</script>
</body>
</html>"""


def cmd_dashboard(port: int = 8766) -> None:
    """Start Flask dashboard server on the given port."""
    if not FLASK_AVAILABLE:
        print("Flask is not installed. Run: pip install flask", file=sys.stderr)
        sys.exit(1)

    app = Flask(__name__)

    @app.route("/")
    def index():
        return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"}

    @app.route("/api/status")
    def api_status():
        state = load_state()
        return jsonify({
            "enabled": state.get("enabled", False),
            "task": state.get("task"),
            "session_name": state.get("session_name"),
            "enabled_at": state.get("enabled_at"),
            "error_count": state.get("error_count", 0),
            "max_errors": state.get("max_errors", 3),
            "decisions_count": state.get("decisions_count", 0),
            "session_id": state.get("session_id"),
            "project": PROJECT_ROOT.name,
        })

    @app.route("/api/decisions")
    def api_decisions():
        limit = flask_request.args.get("limit", 100, type=int)
        if not LOG_FILE.exists():
            return jsonify([])
        lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
        recent = lines[-limit:] if len(lines) > limit else lines
        entries = []
        for line in recent:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return jsonify(entries)

    @app.route("/api/toggle", methods=["POST"])
    def api_toggle():
        """Toggle YesBot on/off globally."""
        data = flask_request.get_json(force=True) or {}
        state = load_state()
        if "enabled" in data:
            state["enabled"] = bool(data["enabled"])
            if state["enabled"]:
                state["enabled_at"] = datetime.now(timezone.utc).isoformat()
                state["session_id"] = str(__import__('uuid').uuid4())[:8]
            if "task" in data:
                state["task"] = data["task"]
        save_state(state)
        return jsonify({"ok": True, "enabled": state["enabled"]})

    @app.route("/api/sessions", methods=["GET"])
    def api_sessions():
        state = load_state()
        return jsonify({
            "targeted": state.get("targeted_sessions", {}),
            "active": state.get("active_sessions", {}),
        })

    @app.route("/api/sessions/rename", methods=["POST"])
    def api_rename_session():
        data = flask_request.get_json(force=True) or {}
        session_id = data.get("session_id", "")
        name = data.get("name", "")
        state = load_state()
        active = state.get("active_sessions", {})
        if session_id in active:
            active[session_id]["name"] = name
        state["active_sessions"] = active
        save_state(state)
        return jsonify({"ok": True})

    @app.route("/api/sessions/toggle", methods=["POST"])
    def api_toggle_session():
        data = flask_request.get_json(force=True) or {}
        session_id = data.get("session_id", "")
        label = data.get("label", "")
        enabled = data.get("enabled", True)
        project_dir = data.get("project_dir", "")

        state = load_state()

        if session_id:
            active = state.get("active_sessions", {})
            if session_id in active:
                active[session_id]["enabled"] = enabled
                state["active_sessions"] = active

        targeted = state.get("targeted_sessions", {})
        if label:
            targeted[label] = {"enabled": enabled, "project_dir": project_dir}
            state["targeted_sessions"] = targeted

        if session_id and session_id in targeted:
            targeted[session_id]["enabled"] = enabled
            state["targeted_sessions"] = targeted

        save_state(state)
        return jsonify({"ok": True, "enabled": enabled})

    @app.route("/api/sessions/add", methods=["POST"])
    def api_add_session():
        data = flask_request.get_json(force=True) or {}
        label = data.get("label", "")
        project_dir = data.get("project_dir", str(PROJECT_ROOT))

        state = load_state()
        sessions = state.get("targeted_sessions", {})
        sessions[label] = {"enabled": True, "project_dir": project_dir}
        state["targeted_sessions"] = sessions
        save_state(state)
        return jsonify({"ok": True, "sessions": sessions})

    @app.route("/api/sessions/remove", methods=["POST"])
    def api_remove_session():
        data = flask_request.get_json(force=True) or {}
        label = data.get("label", "")

        state = load_state()
        sessions = state.get("targeted_sessions", {})
        sessions.pop(label, None)
        state["targeted_sessions"] = sessions
        save_state(state)
        return jsonify({"ok": True, "sessions": sessions})

    @app.route("/api/processes")
    def api_processes():
        import time as _t
        now = _t.time()
        if not hasattr(api_processes, '_cache'):
            api_processes._cache = [[], 0]
        if now - api_processes._cache[1] > 15:
            api_processes._cache[0] = _scan_claude_processes()
            api_processes._cache[1] = now
        return jsonify(api_processes._cache[0])

    @app.route("/api/sessions/<session_id>/latest")
    def api_session_latest(session_id):
        if not LOG_FILE.exists():
            return jsonify([])

        lines = LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
        session_entries = []
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("session_id") == session_id:
                    session_entries.append(entry)
            except json.JSONDecodeError:
                pass

        return jsonify(session_entries[-3:])

    @app.route("/api/rules")
    def api_rules():
        if RULES_FILE.exists():
            return jsonify({"rules": RULES_FILE.read_text(encoding="utf-8")})
        return jsonify({"rules": ""})

    @app.route("/api/rules/update", methods=["POST"])
    def api_update_rule():
        """Move a tool/command to a different rule section."""
        data = flask_request.get_json(force=True) or {}
        tool = data.get("tool", "")
        pattern = data.get("pattern", "")
        target = data.get("target", "")

        if not pattern or target not in ("allow", "block", "ask"):
            return jsonify({"ok": False, "error": "Invalid params"}), 400

        rules_text = RULES_FILE.read_text(encoding="utf-8") if RULES_FILE.exists() else ""

        if tool == "Bash":
            new_rule = "- %s" % pattern
        else:
            new_rule = "- %s tool calls" % tool if tool else "- %s" % pattern

        section_map = {
            "allow": "## Always Allow",
            "block": "## Always Block",
            "ask": "## Ask Me (halt and wait)",
        }
        target_section = section_map[target]

        lines = rules_text.split('\n')
        cleaned = []
        for line in lines:
            if pattern.lower() in line.lower() and line.strip().startswith('-'):
                continue
            cleaned.append(line)

        result = []
        added = False
        for line in cleaned:
            result.append(line)
            if line.strip() == target_section and not added:
                result.append(new_rule)
                added = True

        if not added:
            result.append('')
            result.append(target_section)
            result.append(new_rule)

        RULES_FILE.write_text('\n'.join(result), encoding='utf-8')

        return jsonify({"ok": True, "rule": new_rule, "section": target_section})

    print(f"YesBot Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="YesBot — Claude Code autopilot decision engine")
    parser.add_argument("--decide", action="store_true", help="Read stdin JSON, output decision")
    parser.add_argument("--event", default="PreToolUse", help="Hook event type")
    parser.add_argument("--install", action="store_true", help="Add hooks to settings")
    parser.add_argument("--install-dry-run", action="store_true", help="Show what --install would write")
    parser.add_argument("--uninstall", action="store_true", help="Remove hooks from settings")
    parser.add_argument("--on", action="store_true", help="Enable YesBot")
    parser.add_argument("--off", action="store_true", help="Disable YesBot")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--log", action="store_true", help="Show last 20 decisions")
    parser.add_argument("--test", nargs=2, metavar=("TOOL", "JSON"), help="Test hypothetical")
    parser.add_argument("--dashboard", action="store_true", help="Start live dashboard on port 8766")
    parser.add_argument("--port", type=int, default=8766, help="Dashboard port (default 8766)")

    args = parser.parse_args()

    if args.decide:
        cmd_decide(args.event)
    elif args.install:
        cmd_install()
    elif args.install_dry_run:
        cmd_install(dry_run=True)
    elif args.uninstall:
        cmd_uninstall()
    elif args.on:
        cmd_on()
    elif args.off:
        cmd_off()
    elif args.status:
        cmd_status()
    elif args.log:
        cmd_log()
    elif args.test:
        cmd_test(args.test[0], args.test[1])
    elif args.dashboard:
        cmd_dashboard(args.port)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
