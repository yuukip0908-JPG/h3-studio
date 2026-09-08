#!/usr/bin/env bash
# Install (or remove) the dead-man switch as a launchd agent for this user.
#
# It runs scripts/gpu_deadman.py every 5 minutes. That script stops a RUNNING
# Pod only when the Studio's pulse has gone cold -- meaning the Studio died or
# the Mac did. A Studio that was stopped on purpose removes its own pulse, so a
# Pod started later by hand is left alone.
#
# It never deletes a Pod and never touches the Network Volume.
#
#   bash scripts/install_deadman.sh              # install and start
#   bash scripts/install_deadman.sh --status
#   bash scripts/install_deadman.sh --uninstall
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.h3studio.gpudeadman"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

case "${1:-}" in
  --uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed: $LABEL"
    exit 0
    ;;
  --status)
    if launchctl list | grep -q "$LABEL"; then
      echo "running: $LABEL"
      launchctl list "$LABEL" 2>/dev/null | sed -n '1,12p' || true
    else
      echo "not installed"
    fi
    echo "log: $ROOT/results/logs/gpu_deadman.log"
    exit 0
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/results/logs"
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
    <string>$ROOT/scripts/gpu_deadman.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$ROOT/results/logs/gpu_deadman.out</string>
  <key>StandardErrorPath</key><string>$ROOT/results/logs/gpu_deadman.err</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "installed: $LABEL  (every 5 min)"
echo "  plist : $PLIST"
echo "  log   : $ROOT/results/logs/gpu_deadman.log"
echo "  remove: bash scripts/install_deadman.sh --uninstall"
