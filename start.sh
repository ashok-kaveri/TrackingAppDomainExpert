#!/bin/bash
# TrackingAppDomainExpert — Start Streamlit + ngrok tunnel
# Public URL: https://buffing-federal-swifter.ngrok-free.dev

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=8504

echo "=============================================="
echo " TrackingAppDomainExpert"
echo " Public URL: https://buffing-federal-swifter.ngrok-free.dev"
echo "=============================================="

# Start Streamlit in the background
echo "Starting Streamlit on port $PORT..."
PYTHONPATH=. .venv/bin/streamlit run ui/chat_app.py \
  --server.port $PORT \
  --server.headless true &
STREAMLIT_PID=$!

# Wait for Streamlit to be ready
echo "Waiting for Streamlit to start..."
sleep 5

# Start ngrok tunnel with static domain
echo "Starting ngrok tunnel..."
ngrok http $PORT \
  --domain=buffing-federal-swifter.ngrok-free.dev \
  --log=stdout &
NGROK_PID=$!

echo ""
echo "✅ App is live at: https://buffing-federal-swifter.ngrok-free.dev"
echo ""
echo "Press Ctrl+C to stop everything."

# Graceful shutdown on Ctrl+C
trap "echo 'Stopping...'; kill $STREAMLIT_PID $NGROK_PID 2>/dev/null; exit 0" INT TERM

wait
