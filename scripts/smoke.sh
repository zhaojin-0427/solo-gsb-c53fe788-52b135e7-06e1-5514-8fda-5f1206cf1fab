#!/usr/bin/env bash
# End-to-end smoke test / demo for the chip timing adjudication API.
# Usage:  ./scripts/smoke.sh [BASE_URL]     (default http://localhost:8000)
set -euo pipefail

BASE="${1:-http://localhost:8000}"
pp() { python3 -m json.tool; }
req() { # req METHOD PATH [JSON_BODY] -> prints body, stores in $BODY, status in $STATUS
  local method="$1" path="$2" body="${3:-}"
  local args=(-s -X "$method" -H 'Content-Type: application/json')
  [ -n "$body" ] && args+=(-d "$body")
  BODY=$(curl "${args[@]}" -w $'\n%{http_code}' "$BASE$path")
  STATUS="${BODY##*$'\n'}"
  BODY="${BODY%$'\n'*}"
}

echo "==> GET /health"
req GET /health; echo "$BODY" | pp

echo "==> POST /tracks  (create immutable track)"
req POST /tracks '{
  "name": "Tianzhu Trail 2-Lap",
  "gun_time": "2026-10-01T08:00:00Z",
  "checkpoints": ["CP1", "CP2"],
  "finish": "FIN",
  "laps": 2,
  "segments": [
    {"min_seconds": 300, "max_seconds": 900},
    {"min_seconds": 300, "max_seconds": 900},
    {"min_seconds": 300, "max_seconds": 900}
  ]
}'
echo "HTTP $STATUS"; echo "$BODY" | pp
TRACK=$(echo "$BODY" | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')

echo "==> POST /readings  (CHIP-001, uploaded out of chronological order)"
for r in \
  '{"id":"rd-06","chip_id":"CHIP-001","point":"FIN","time":"2026-10-01T09:00:00Z"}' \
  '{"id":"rd-03","chip_id":"CHIP-001","point":"FIN","time":"2026-10-01T08:30:00Z"}' \
  '{"id":"rd-01","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:10:00Z"}' \
  '{"id":"rd-04","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:40:00Z"}' \
  '{"id":"rd-02","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:20:00Z"}' \
  '{"id":"rd-05","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:50:00Z"}' \
  '{"id":"rd-07","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:21:00Z"}' \
  '{"id":"rd-08","chip_id":"CHIP-001","point":"CP9","time":"2026-10-01T08:25:00Z"}' \
  '{"id":"rd-09","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:11:00Z"}'
do
  req POST /readings "$r"
  id=$(echo "$r" | python3 -c 'import sys, json; print(json.load(sys.stdin)["id"])')
  echo "  $id -> HTTP $STATUS"
  [ "$STATUS" = 201 ]
done

echo "==> POST /readings  (rd-06 again, identical content -> idempotent 200)"
req POST /readings '{"id":"rd-06","chip_id":"CHIP-001","point":"FIN","time":"2026-10-01T09:00:00Z"}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> POST /readings  (rd-01 with different content -> 409 conflict)"
req POST /readings '{"id":"rd-01","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:12:00Z"}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> POST /tracks/$TRACK/chips/CHIP-001/adjudications  (expected_version 0 -> v1)"
req POST "/tracks/$TRACK/chips/CHIP-001/adjudications" '{"expected_version": 0}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> POST /voids  (void misread rd-09)"
req POST /voids '{"reading_id":"rd-09","reason":"misread: duplicate of rd-01"}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> POST /voids  (same reading again -> idempotent 200)"
req POST /voids '{"reading_id":"rd-09"}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> POST /readings  (late backfill rd-00 and rd-10)"
req POST /readings '{"id":"rd-00","chip_id":"CHIP-001","point":"CP1","time":"2026-10-01T08:05:00Z"}'
echo "  rd-00 -> HTTP $STATUS"; [ "$STATUS" = 201 ]
req POST /readings '{"id":"rd-10","chip_id":"CHIP-001","point":"CP2","time":"2026-10-01T08:22:00Z"}'
echo "  rd-10 -> HTTP $STATUS"; [ "$STATUS" = 201 ]

echo "==> POST .../adjudications  (stale expected_version 0 -> 409)"
req POST "/tracks/$TRACK/chips/CHIP-001/adjudications" '{"expected_version": 0}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> POST .../adjudications  (expected_version 1 -> v2)"
req POST "/tracks/$TRACK/chips/CHIP-001/adjudications" '{"expected_version": 1}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> GET .../adjudications/1  (v1 is immutable: still adopts rd-01, no void)"
req GET "/tracks/$TRACK/chips/CHIP-001/adjudications/1"
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> GET .../adjudications  (version list)"
req GET "/tracks/$TRACK/chips/CHIP-001/adjudications"
echo "HTTP $STATUS"
echo "$BODY" | python3 -c 'import sys, json; print([v["version"] for v in json.load(sys.stdin)])'

echo "==> CHIP-002: only two valid passes -> dnf"
req POST /readings '{"id":"rd-20","chip_id":"CHIP-002","point":"CP1","time":"2026-10-01T08:10:00Z"}'
echo "  rd-20 -> HTTP $STATUS"; [ "$STATUS" = 201 ]
req POST /readings '{"id":"rd-21","chip_id":"CHIP-002","point":"CP2","time":"2026-10-01T08:20:00Z"}'
echo "  rd-21 -> HTTP $STATUS"; [ "$STATUS" = 201 ]
req POST "/tracks/$TRACK/chips/CHIP-002/adjudications" '{"expected_version": 0}'
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "==> GET /chips/CHIP-001/readings  (receive order, void flag)"
req GET /chips/CHIP-001/readings
echo "HTTP $STATUS"; echo "$BODY" | pp

echo "SMOKE OK"
