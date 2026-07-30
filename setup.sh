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
    echo "Created config.yml (workspace: ~/workspace — auto-discovers angellist repos)."
else
    echo "config.yml found."
fi

# --- Migrate off the old 3-alias setup, if present ---
MARKER="# PR Status Dashboard daemon"
for RC in "$HOME/.zshrc" "$HOME/.bashrc"; do
    if grep -qF "$MARKER" "$RC" 2>/dev/null; then
        sed -i.bak "/$MARKER/,+3d" "$RC"
        rm -f "$RC.bak"
        echo "Removed old pr-status-* aliases from $RC"
    fi
done

# --- Install the unified `pr-status` command on PATH ---
chmod +x "$SCRIPT_DIR/pr-status"
BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
ln -sf "$SCRIPT_DIR/pr-status" "$BIN_DIR/pr-status"
echo "Linked pr-status -> $BIN_DIR/pr-status"

echo ""
echo "Setup complete!"
case ":$PATH:" in
    *":$BIN_DIR:"*)
        echo "To get started:"
        echo "  pr-status start"
        echo "  open http://localhost:9600" ;;
    *)
        echo "NOTE: $BIN_DIR is not on your PATH. Add this to your shell rc file:"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
        echo "Then run: pr-status start" ;;
esac
echo ""
echo "Commands: pr-status {start|stop|restart|status|logs|run}"
echo "Config:   $SCRIPT_DIR/config.yml"
