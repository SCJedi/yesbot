#!/bin/bash
# YesBot — Claude Code decision hook
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
YESBOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Find yesbot.py — check common locations
if [ -f "$YESBOT_DIR/yesbot/yesbot.py" ]; then
    YESBOT_PY="$YESBOT_DIR/yesbot/yesbot.py"
elif [ -f "$YESBOT_DIR/yesbot.py" ]; then
    YESBOT_PY="$YESBOT_DIR/yesbot.py"
else
    # Search relative to project root
    YESBOT_PY="$(find "$YESBOT_DIR" -name 'yesbot.py' -maxdepth 3 2>/dev/null | head -1)"
fi

if [ -z "$YESBOT_PY" ]; then
    exit 0  # Can't find yesbot, pass through
fi

EVENT="${CLAUDE_HOOK_EVENT:-PreToolUse}"
RESULT=$(cat | python "$YESBOT_PY" --decide --event "$EVENT" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 2 ]; then
    echo "$RESULT" >&2
    exit 2
fi
exit 0
