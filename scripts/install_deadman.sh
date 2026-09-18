#!/usr/bin/env bash
# Install (or remove) the dead-man switch as a launchd agent for this user.
#
# Every 5 minutes it stops a RUNNING Pod -- only when the Studio's pulse has
# gone cold, meaning the Studio died or the Mac did. A Studio that was stopped
# on purpose removes its own pulse, so a Pod started later by hand is left
# alone. It never deletes a Pod and never touches the Network Volume.
#
# The agent does NOT run from this checkout. ~/Downloads is TCC-protected, and a
# launchd agent is refused every path inside it -- the previous install failed
# with "Operation not permitted" 1,908 times over 6.6 days without ever stopping
# anything. So scripts/gpu_deadman_agent.py (self-contained, no `studio`
# imports) is copied to ~/Library/Application Support/H3Studio/ and launchd runs
# it from there, where the Studio also mirrors its heartbeat.
#
#   bash scripts/install_deadman.sh              # install and start
#   bash scripts/install_deadman.sh --status
#   bash scripts/install_deadman.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.h3studio.gpudeadman"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
AGENT_DIR="$HOME/Library/Application Support/H3Studio"
AGENT="$AGENT_DIR/gpu_deadman.py"
LOG="$AGENT_DIR/gpu_deadman.log"
# Pinned, not `command -v python3`: the agent is stdlib-only, and a Homebrew
# python that gets upgraded or removed would silently take the switch with it.
PY="/usr/bin/python3"

case "${1:-}" in
  --uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL   (agent files kept in $AGENT_DIR)"
    exit 0
    ;;
  --status)
    if launchctl list | grep -q "$LABEL"; then
      echo "loaded: $LABEL"
      launchctl list "$LABEL" 2>/dev/null | grep -E '"(PID|LastExitStatus)"' || true
    else
      echo "NOT INSTALLED"
    fi
    echo "agent : $AGENT"
    echo "pulse : $AGENT_DIR/studio_heartbeat.json"
    echo "log   : $LOG"
    [[ -f "$LOG" ]] && { echo "--- last 5 log lines ---"; tail -5 "$LOG"; }
    exit 0
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$AGENT_DIR"
cp "$ROOT/scripts/gpu_deadman_agent.py" "$AGENT"
chmod 0755 "$AGENT"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$AGENT</string>
  </array>
  <key>WorkingDirectory</key><string>$AGENT_DIR</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$AGENT_DIR/gpu_deadman.out</string>
  <key>StandardErrorPath</key><string>$AGENT_DIR/gpu_deadman.err</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

echo "installed: $LABEL  (every 5 min)"
echo "  agent : $AGENT"
echo "  plist : $PLIST"
echo "  log   : $LOG"
echo "  remove: bash scripts/install_deadman.sh --uninstall"
