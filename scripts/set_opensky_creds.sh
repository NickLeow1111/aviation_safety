#!/usr/bin/env bash
# Store OpenSky credentials on this machine, without ever echoing them.
#
# The values are typed at a hidden prompt: they never appear on screen, in your
# shell history, in a log, or in this chat. The file is written 0600 and is
# gitignored, so it cannot be committed by accident.
#
#   ./scripts/set_opensky_creds.sh          # set or replace the credentials
#   ./scripts/set_opensky_creds.sh --check  # is a credential present? (no values)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/agent/.env.local"

have_id()     { grep -qE '^OPENSKY_CLIENT_ID=.+' "$ENV_FILE" 2>/dev/null; }
have_secret() { grep -qE '^OPENSKY_CLIENT_SECRET=.+' "$ENV_FILE" 2>/dev/null; }

if [ "${1:-}" = "--check" ]; then
  if [ ! -f "$ENV_FILE" ]; then
    echo "no credential file yet: $ENV_FILE"
    echo "the app is running anonymously (400 credits/day, 340 usable)"
    exit 0
  fi
  if have_id && have_secret; then
    echo "credentials present in $ENV_FILE (values not shown)"
    echo "authenticated budget: 4000 credits/day, 3400 usable"
  else
    echo "credential file exists but is incomplete"
    have_id     || echo "  missing OPENSKY_CLIENT_ID"
    have_secret || echo "  missing OPENSKY_CLIENT_SECRET"
  fi
  exit 0
fi

printf 'OpenSky client id: '
read -r CLIENT_ID
printf 'OpenSky client secret (typing is hidden): '
read -r -s CLIENT_SECRET
printf '\n'

if [ -z "${CLIENT_ID}" ] || [ -z "${CLIENT_SECRET}" ]; then
  echo "Both values are required — nothing was written." >&2
  exit 1
fi

umask 077
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

TMP="$(mktemp)"
chmod 600 "$TMP"
trap 'rm -f "$TMP"' EXIT

# Keep any other settings in the file (budget, database URL) and replace only the
# two credential lines, so re-running this is safe.
grep -vE '^OPENSKY_CLIENT_(ID|SECRET)=' "$ENV_FILE" > "$TMP" || true
printf 'OPENSKY_CLIENT_ID=%s\nOPENSKY_CLIENT_SECRET=%s\n' "$CLIENT_ID" "$CLIENT_SECRET" >> "$TMP"
mv "$TMP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

echo
echo "Saved to $ENV_FILE  (mode $(stat -c '%a' "$ENV_FILE"))"
echo "Not printed here, not in your shell history, not in git."
echo
echo "Restart the app for it to take effect:"
echo "  pkill -f 'uvicorn app:app'        # then start it again as usual"
echo "  # or: sudo systemctl restart safetyintel"
echo
echo "Then confirm:"
echo "  curl -s localhost:7700/api/live/status"
echo "  (expect \"mode\": \"authenticated\")"
