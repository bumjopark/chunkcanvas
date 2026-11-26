#!/usr/bin/env bash
# Install the bare-metal systemd user service. UNIT_NAME permits independent installations.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8300}"
UNIT_NAME="${UNIT_NAME:-chunkcanvas}"
UNIT_DIR="$HOME/.config/systemd/user"
[[ "$UNIT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || { echo "Invalid unit name" >&2; exit 1; }
[[ "$PORT" =~ ^[1-9][0-9]{0,4}$ ]] && (( PORT <= 65535 )) || { echo "Invalid port" >&2; exit 1; }
[ -f "$ROOT/.env" ] || { echo "Missing $ROOT/.env (copy .env.example)"; exit 1; }
[ -x /usr/bin/bwrap ] || { echo "The user service requires bubblewrap." >&2; exit 1; }
[ -f "$ROOT/apps/web/dist/index.html" ] || { echo "Build the web app first"; exit 1; }
mkdir -p "$UNIT_DIR"
temporary=$(mktemp "$UNIT_DIR/.$UNIT_NAME.XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
"$ROOT/services/api/.venv/bin/python" -B - "$ROOT" "$PORT" "$temporary" <<'PY'
import sys
from pathlib import Path
root, port, destination = sys.argv[1:]
if any(c in root for c in '"\\\r\n$'):
    raise SystemExit("Unsupported character in service path")
template = (Path(root) / "deploy/systemd/chunkcanvas.service").read_text()
Path(destination).write_text(template.replace("@ROOT@", root.replace("%", "%%")).replace("@PORT@", port))
PY
mv -T "$temporary" "$UNIT_DIR/$UNIT_NAME.service"
systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME.service"
systemctl --user restart "$UNIT_NAME.service"
echo "$UNIT_NAME.service started on 127.0.0.1:$PORT"
