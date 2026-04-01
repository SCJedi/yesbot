# YesBot

**Autopilot for Claude Code.** YesBot watches every tool call Claude Code makes and auto-approves, blocks, or escalates based on your rules — so you can walk away while it works.

Think of it as the [drinking bird](https://simpsons.fandom.com/wiki/Drinking_Bird) that presses "Y" for you, except it actually reads what it's approving.

## What It Does

- **Auto-approves safe actions** — file reads, searches, tests, linting
- **Blocks dangerous actions** — `rm -rf`, force pushes, credential access
- **Escalates uncertain actions** — deploys, pushes, anything involving real money
- **Logs every decision** — full audit trail with timestamps, tool inputs, and responses
- **Live dashboard** — watch decisions stream in real-time at localhost:8766

## Quick Start

### 1. Install (30 seconds)

```bash
# Copy the yesbot/ directory into your project
cp -r yesbot/ /path/to/your/project/yesbot/

# Install the hooks
cd /path/to/your/project
python yesbot/yesbot.py --install
```

### 2. Turn it on

```bash
python yesbot/yesbot.py --on
```

### 3. Work in Claude Code

That's it. YesBot handles the prompts. Open the dashboard to watch:

```bash
python yesbot/yesbot.py --dashboard
# Open http://localhost:8766
```

### 4. Turn it off

```bash
python yesbot/yesbot.py --off
```

## How It Works

YesBot uses Claude Code's **hook system** — no screen scraping, no GUI automation, no brittle hacks. When Claude Code wants to use a tool (read a file, run a command, edit code), the hook fires with structured JSON describing what it wants to do. YesBot reads it, checks your rules, and decides.

```
Claude Code: "I want to run: python -m pytest"
    |  (PreToolUse hook)
YesBot: checks rules -> "python" is in the allowed list -> ALLOW
    |
Claude Code: runs the command, continues working
```

## Rules

Edit `yesbot-rules.md` to customize behavior. It's plain English:

```markdown
## Always Allow
- Reading any file
- Running: python, pip, git status/diff/log, npm, pytest, ruff

## Always Block
- rm -rf, deltree, format commands
- git push --force
- sudo or admin elevation

## Ask Me (halt and wait)
- git push (non-force)
- Deploying to production
- Anything involving real money
```

Changes take effect immediately — no restart needed.

### Change Rules From the Dashboard

Click any action badge (ALLOW/BLOCK/PASS) in the decision log, pick a new rule, and it updates `yesbot-rules.md` automatically.

## Dashboard

The live dashboard at `localhost:8766` shows:

- **Toggle button** — one click on/off
- **Session sidebar** — auto-discovers Claude Code sessions, per-session on/off
- **Decision log** — sortable by date, session, tool, action
- **Q&A pairs** — see what Claude wanted to do AND what happened
- **Click to change rules** — click any badge to update rules inline

## CLI Reference

```bash
python yesbot/yesbot.py --on              # Enable YesBot
python yesbot/yesbot.py --off             # Disable YesBot
python yesbot/yesbot.py --status          # Show current state
python yesbot/yesbot.py --install         # Install hooks into Claude Code settings
python yesbot/yesbot.py --uninstall       # Remove hooks
python yesbot/yesbot.py --dashboard       # Start live dashboard (port 8766)
python yesbot/yesbot.py --dashboard --port 9000  # Custom port
python yesbot/yesbot.py --log             # Show last 20 decisions in terminal
```

## Requirements

- Python 3.10+
- Claude Code (with hook support)
- Flask (`pip install flask`) — only needed for dashboard
- psutil (`pip install psutil`) — optional, for session auto-discovery

## How It's Different

| Feature | YesBot | Permission settings | Screen automation |
|---------|--------|-------------------|-------------------|
| Per-command rules | Natural language | Binary allow/deny | No |
| Live dashboard | Real-time | No | No |
| Decision logging | Full audit trail | No | No |
| Change rules live | Click in dashboard | Restart needed | No |
| No GUI dependency | Pure hooks | Pure hooks | Fragile |
| Works headless | Yes | Yes | No |

## License

MIT

Built by [Infinite Visions AI Agents](https://www.infinitevisionsaiagents.com) — we build AI systems that pay for themselves.
