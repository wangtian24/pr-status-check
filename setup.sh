#!/usr/bin/env bash
# setup.sh - Install the PR Status Dashboard.
#
# Usage:
#   bash setup.sh
#
# This will:
#   1. Check prerequisites (python3, gh)
#   2. Create config.yml from the example if it doesn't exist
#   3. Add shell aliases to your rc file (~/.bashrc or ~/.zshrc)
#
# Prerequisites: python3, gh (GitHub CLI, authenticated)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Preflight checks ---
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3." >&2
    exit 1
fi

if ! command -v gh &>/dev/null; then
    echo "ERROR: gh (GitHub CLI) not found. Install from https://cli.github.com" >&2
    exit 1
fi

if ! gh auth status &>/dev/null; then
    echo "ERROR: gh is not authenticated. Run 'gh auth login' first." >&2
    exit 1
fi

GH_USER=$(gh api user --jq '.login')
echo "GitHub user: @$GH_USER"

# --- Create config.yml if missing ---
if [ ! -f "$SCRIPT_DIR/config.yml" ]; then
    cp "$SCRIPT_DIR/config.yml.example" "$SCRIPT_DIR/config.yml"
    echo ""
    echo "Created config.yml from config.yml.example."
    echo "Please edit it to add your repos:"
    echo "  $EDITOR $SCRIPT_DIR/config.yml"
    echo ""
    echo "Then re-run: bash setup.sh"
    exit 0
else
    echo "config.yml found."
fi

# --- Detect shell rc file ---
SHELL_NAME="$(basename "$SHELL")"
if [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
elif [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
else
    RC_FILE="$HOME/.bashrc"
    echo "WARNING: Unknown shell '$SHELL_NAME', defaulting to $RC_FILE"
fi

# --- Remove old aliases if present, then add new ones ---
MARKER="# PR Status Dashboard daemon"
if grep -qF "$MARKER" "$RC_FILE" 2>/dev/null; then
    sed -i.bak "/$MARKER/,+3d" "$RC_FILE"
    rm -f "$RC_FILE.bak"
    echo "Removed old aliases from $RC_FILE"
fi

cat >> "$RC_FILE" << ALIASES

${MARKER}
alias pr-status-start='nohup python3 ${SCRIPT_DIR}/server.py > ${SCRIPT_DIR}/daemon.log 2>&1 & echo \$! > ${SCRIPT_DIR}/daemon.pid && echo "PR dashboard started at http://localhost:9600 (pid \$(cat ${SCRIPT_DIR}/daemon.pid))"'
alias pr-status-stop='if [ -f ${SCRIPT_DIR}/daemon.pid ]; then kill \$(cat ${SCRIPT_DIR}/daemon.pid) 2>/dev/null && rm ${SCRIPT_DIR}/daemon.pid && echo "PR dashboard stopped"; else echo "No daemon running"; fi'
alias pr-status-restart='pr-status-stop; sleep 1; pr-status-start'
ALIASES

echo "Added aliases to $RC_FILE"
echo ""
echo "Setup complete! To get started:"
echo "  1. source $RC_FILE"
echo "  2. pr-status-start"
echo "  3. Open http://localhost:9600"
echo ""
echo "Commands: pr-status-start | pr-status-stop | pr-status-restart"
echo "Config:   $SCRIPT_DIR/config.yml"
