#!/usr/bin/env bash
set -euo pipefail
HOST="192.168.3.4"
OUTPUT="${1:-l15a-v14-media.pcap}"
IFACE="$(route -n get "$HOST" | awk '/interface:/{print $2; exit}')"
if [[ -z "$IFACE" ]]; then
  echo "Could not determine interface to $HOST" >&2
  exit 1
fi

echo "Capturing $HOST on $IFACE to $OUTPUT (Ctrl-C to stop)"
exec sudo tcpdump -i "$IFACE" -nn -s 0 -U -w "$OUTPUT" \
  "host $HOST and (tcp port 8899 or tcp port 7274 or udp port 36524)"
