cat > /home/talshaubi/projects/personal-assistant/run.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

# --- config ---
REPO_DIR="/home/talshaubi/projects/personal-assistant"
BRANCH="main"
PYTHON="$REPO_DIR/.venv/bin/python"
REQ_FILE="$REPO_DIR/requirements.txt"
ENTRY_CMD="$PYTHON $REPO_DIR/app.py"   # change if your entry point differs

# --- wait for network ---
for i in {1..20}; do
  if ping -c1 -W1 1.1.1.1 >/dev/null 2>&1; then break; fi
  sleep 1
done

cd "$REPO_DIR"

# --- update code (force-sync, safest for unattended boots) ---
git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH"
# keep local-only stuff
git clean -fd -e .env -e .venv -e run.sh

# --- venv & deps ---
if [ ! -x "$PYTHON" ]; then
  python3 -m venv "$REPO_DIR/.venv"
  "$PYTHON" -m pip install --upgrade pip wheel
fi

if [ -f "$REQ_FILE" ]; then
  "$PYTHON" -m pip install --upgrade -r "$REQ_FILE"
fi

# --- run the app ---
exec $ENTRY_CMD
EOF

chmod +x /home/talshaubi/projects/personal-assistant/run.sh