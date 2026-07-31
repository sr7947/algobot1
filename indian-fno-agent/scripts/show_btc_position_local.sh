#!/usr/bin/env bash
# Place the approved BTCUSD paper position into the LOCAL API so Positions shows it.
# Run this on the same machine as localhost:5173 / :8000
set -euo pipefail
API="${API_BASE:-http://127.0.0.1:8000}"

echo "Posting BTCUSD paper position to ${API} ..."
if curl -sf -X POST "${API}/api/v1/positions/load-seed" >/tmp/pos-seed.json 2>/dev/null; then
  echo "load-seed OK:"
  cat /tmp/pos-seed.json
  echo
else
  echo "load-seed not available — using create-sample"
  curl -sf -X POST "${API}/api/v1/positions/create-sample" \
    -H 'Content-Type: application/json' \
    -d '{"symbol":"BTCUSD","direction":"BUY","entry":65200,"qty":1,"leverage":25,"current":65200,"asset_class":"CRYPTO"}' \
    | tee /tmp/pos-sample.json
  echo
fi

echo "Current CRYPTO positions:"
curl -sf "${API}/api/v1/positions?asset_class=CRYPTO" | python3 -m json.tool | head -80
echo
echo "Refresh http://localhost:5173/positions"
