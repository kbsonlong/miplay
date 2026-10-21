#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m unittest discover -s tests -v
python3 -m compileall -q src tools tests miplay_client.py miplay_client_v14.py
python3 miplay_client.py
python3 miplay_client_v14.py
