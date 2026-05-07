#!/bin/bash
set -e
cd "$(dirname "$0")"

export TURN_HOST="${TURN_HOST:-}"
export TURN_SECRET="${TURN_SECRET:-}"

pip3.14 install -r requirements.txt -q

exec python3.14 -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-8084}"
