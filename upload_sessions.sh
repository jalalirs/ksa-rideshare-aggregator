#!/usr/bin/env bash
# Upload local session files as Railway env vars (base64-encoded).
# Requires: `railway login` + `railway link` already done in this directory.
#
# Usage:
#   ./upload_sessions.sh                  # both uber and careem
#   ./upload_sessions.sh uber             # uber only

set -euo pipefail

cd "$(dirname "$0")"

upload_one() {
    local provider=$1
    local file="sessions/auth_state_${provider}.json"
    local varname="AUTH_STATE_$(echo "$provider" | tr a-z A-Z)"

    if [ ! -f "$file" ]; then
        echo "skip $provider — $file not found (run: python auth.py $provider)"
        return
    fi

    local b64
    b64=$(base64 < "$file" | tr -d '\n')
    echo "→ setting $varname (${#b64} chars)"
    railway variables --set "${varname}=${b64}"
}

targets=${1:-both}
if [ "$targets" = "both" ]; then
    upload_one uber
    upload_one careem
else
    upload_one "$targets"
fi

echo
echo "✓ done. Railway will redeploy with the new env vars."
