#!/usr/bin/env bash
# One-shot setup for a fresh Raspberry Pi checkout.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Creating virtual environment"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
echo "==> Installing dependencies (a few minutes on a Pi)"
./.venv/bin/pip install -r requirements.txt

[ -f .env ] || { cp .env.example .env; echo "==> Created .env - add your keys"; }
[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "==> Created config.yaml - works as-is; see the comments to customise"; }

echo
echo "Next:"
echo "  1. Edit .env         (ANTHROPIC_API_KEY, optionally GNEWS_API_KEY)"
echo "  2. Optional: edit config.yaml (space.public_url if your phone uses another address)"
echo "  3. ./.venv/bin/python -m newsbot check"
echo "  4. ./.venv/bin/python -m newsbot        (or install the systemd service)"
echo "  5. ./.venv/bin/python -m newsbot pair   (open the link on your phone)"
