#!/usr/bin/env bash
# Pulls new commits from GitHub and restarts Orbital if anything changed.
# Run manually any time, or on the newsbot-update.timer schedule.
set -euo pipefail
cd "$(dirname "$0")/.."

LOG_TAG="newsbot-deploy"
log() { logger -t "$LOG_TAG" "$1"; echo "$1"; }

git fetch --quiet origin

LOCAL=$(git rev-parse @)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0   # nothing new - stay quiet so the timer log doesn't fill up
fi

log "Update found ($LOCAL -> $REMOTE), pulling"

if ! git pull --ff-only --quiet; then
    log "FAILED: git pull was not a fast-forward - local changes on the Pi? Resolve manually."
    exit 1
fi

if [ -f requirements.txt ] && [ -x .venv/bin/pip ]; then
    # A dependency that won't install must not stop the restart: the code
    # treats optional packages (like pywebpush) as optional.
    ./.venv/bin/pip install --quiet -r requirements.txt \
        || log "WARNING: pip install failed - restarting with what is installed"
fi

log "Restarting newsbot.service"
sudo systemctl restart newsbot

log "Deployed $(git rev-parse --short HEAD): $(git log -1 --format=%s)"
