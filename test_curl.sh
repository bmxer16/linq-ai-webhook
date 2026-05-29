#!/usr/bin/env bash
# Fire a fake inbound Linq message at a running server to test the loop.
# Usage:
#   ./test_curl.sh                       # hits local server on :5000
#   ./test_curl.sh https://your.onrender.com   # hits your live Render URL
BASE="${1:-http://localhost:5000}"
curl -sS -X POST "$BASE/webhook/linq" \
  -H "Content-Type: application/json" \
  -d '{
    "event_type": "message.received",
    "data": {
      "direction": "inbound",
      "sender_handle": { "handle": "+15556667777", "is_me": false },
      "parts": [ { "type": "text", "value": "Hi, I called earlier about getting a quote" } ]
    }
  }'
echo
