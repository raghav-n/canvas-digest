#!/bin/zsh
# Install two launchd agents:
#   sg.raghavn.canvas-digest        runs agent.py (Claude scan) then digest.py daily at HH:MM
#   sg.raghavn.canvas-digest-server serves ./site on http://127.0.0.1:$PORT (always on)
# Usage: ./install.sh [HH:MM] [PORT]      defaults: 07:00 8765
# Re-run to change the time; ./install.sh --uninstall to remove.
set -euo pipefail

DIR="${0:A:h}"
PY="$(command -v python3)"
LA="$HOME/Library/LaunchAgents"
JOB=sg.raghavn.canvas-digest
SRV=sg.raghavn.canvas-digest-server
LOG="$DIR/logs"

unload() { launchctl bootout "gui/$UID/$1" 2>/dev/null || true; rm -f "$LA/$1.plist"; }

if [[ "${1:-}" == "--uninstall" ]]; then
  unload $JOB; unload $SRV; echo "Removed."; exit 0
fi

TIME="${1:-07:00}"; PORT="${2:-8765}"
H=$((10#${TIME%%:*})); M=$((10#${TIME##*:}))
mkdir -p "$LA" "$LOG" "$DIR/site"

unload $JOB
cat > "$LA/$JOB.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$JOB</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-c</string>
    <string>"$PY" "$DIR/agent.py" --out "$DIR/site"; "$PY" "$DIR/digest.py" --out "$DIR/site"</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$H</integer><key>Minute</key><integer>$M</integer>
  </dict>
  <key>StandardOutPath</key><string>$LOG/digest.log</string>
  <key>StandardErrorPath</key><string>$LOG/digest.log</string>
</dict></plist>
EOF

unload $SRV
cat > "$LA/$SRV.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$SRV</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>http.server</string><string>$PORT</string>
    <string>--bind</string><string>127.0.0.1</string><string>--directory</string><string>$DIR/site</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG/server.log</string>
  <key>StandardErrorPath</key><string>$LOG/server.log</string>
</dict></plist>
EOF

launchctl bootstrap "gui/$UID" "$LA/$JOB.plist"
launchctl bootstrap "gui/$UID" "$LA/$SRV.plist"
printf "Digest runs daily at %02d:%02d. If the Mac is asleep then, it runs on wake.\n" $H $M
echo "Open http://127.0.0.1:$PORT  ·  run now: launchctl kickstart gui/$UID/$JOB"
