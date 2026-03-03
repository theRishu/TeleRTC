#!/bin/bash
# TeleRTC Platform Runner

set -e
cd "$(dirname "$0")"

# 1. Load env
if [ -f .env ]; then
  export $(grep -v '^#' .env | xargs)
fi

echo "📦 Syncing dependencies..."
pip3 install -r requirements.txt -q

echo "🚀 Starting TeleRTC Signaling Server (Port ${PORT:-8084})..."
# Run FastAPI in background
python3 -m uvicorn app:app --host 0.0.0.0 --port ${PORT:-8084} &
SERVER_PID=$!

# Wait for server
sleep 2

echo "🤖 Starting Telegram Bot..."
cd botsrc
python3 main.py

# Cleanup server on bot exit
kill $SERVER_PID

