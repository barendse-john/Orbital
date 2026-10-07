#!/usr/bin/env bash
# Snapshots the things git doesn't track: your API keys, your config, the
# database (topics, timezone, digest time, sent-article history) and, if you
# use reports from Drive, the Google service account key.
#
#   ./scripts/backup.sh                  -> ~/newsbot-backups/
#   ./scripts/backup.sh /mnt/usb         -> somewhere else
#
# The archive contains secrets in plaintext, so it's written 0600. Copy it
# off the Pi - a backup that only exists on the card it's backing up is not
# a backup.
set -euo pipefail
cd "$(dirname "$0")/.."

DEST="${1:-$HOME/newsbot-backups}"
KEEP=14
STAMP=$(date +%Y%m%d-%H%M%S)
ARCHIVE="$DEST/newsbot-$STAMP.tar.gz"

mkdir -p "$DEST"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

for f in .env config.yaml; do
    [ -f "$f" ] && cp "$f" "$STAGE/" || echo "note: no $f to back up"
done
KEY="data/google-service-account.json"
[ -f "$KEY" ] && cp "$KEY" "$STAGE/" || true

# The bot writes to the database continuously, and it runs in WAL mode, so
# copying the file directly can capture a torn state. sqlite3's backup API
# takes a consistent snapshot of a live database.
DB="data/newsbot.db"
if [ -f "$DB" ]; then
    python3 - "$DB" "$STAGE/newsbot.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
source.close()
target.close()
PY
else
    echo "note: no database yet"
fi

tar -czf "$ARCHIVE" -C "$STAGE" .
chmod 600 "$ARCHIVE"

# Keep the most recent KEEP archives, drop the rest.
ls -1t "$DEST"/newsbot-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | \
    xargs -r rm --

echo "Backed up to $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo
echo "Copy it somewhere that isn't this Pi, e.g. from your laptop:"
echo "  scp $(whoami)@$(hostname):$ARCHIVE ."
