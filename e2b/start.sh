#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

cd "$REPO_DIR"

if [ ! -f .env ]; then
  echo "[E2B] .env not found. Copy .env.example to .env and set DISCORD_TOKEN."
  exit 1
fi

if command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON=python
else
  echo "[E2B] Python 3 is required but was not found in this environment."
  exit 1
fi

$PYTHON -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

mkdir -p logs

echo "[E2B] Starting PupPet bot..."
nohup python main.py > logs/e2b-bot.log 2>&1 &

echo "[E2B] Bot started in background."
echo "[E2B] Logs: $REPO_DIR/logs/e2b-bot.log"
echo "[E2B] To stop it: pkill -f 'python main.py'"
