#!/usr/bin/env bash
set -euo pipefail

AUDIO_FILE="${1:-/tmp/l15a-v7-test.aiff}"
HOST="192.168.3.4"

if [[ ! -f "$AUDIO_FILE" ]]; then
  echo "Audio file not found: $AUDIO_FILE" >&2
  exit 1
fi

# The confirmed original V14 script is intentionally used as the default.
cd "$(dirname "$0")/.."
exec python3 miplay_client_v14.py \
  --connect --confirm-host "$HOST" --trigger-open \
  --audio-file "$AUDIO_FILE" --hold 60
