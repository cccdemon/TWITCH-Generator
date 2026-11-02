#!/usr/bin/env bash
# Spike 4: post one 30-min WAV chunk to whisper-server and time it.
# Usage: bash whisper_post.sh /path/chunk_0000.wav [verbose_json|srt|json] [http://localhost:8080]
set -euo pipefail

WAV="${1:?wav path}"
FMT="${2:-verbose_json}"
URL="${3:-http://localhost:8080}"
OUT="/tmp/whisper_${FMT}.out"

DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WAV" 2>/dev/null || echo 1800)
echo "file=$WAV duration=${DUR}s format=$FMT url=$URL"

T0=$(date +%s.%N)
HTTP=$(curl -s -o "$OUT" -w '%{http_code}' "$URL/inference" \
  -F "file=@$WAV" \
  -F "language=de" \
  -F "temperature=0.0" \
  -F "response_format=$FMT")
T1=$(date +%s.%N)

WALL=$(python3 -c "print(round($T1-$T0,1))")
FACTOR=$(python3 -c "print(round($DUR/($T1-$T0),1))")
echo "http=$HTTP wall=${WALL}s realtime_factor=${FACTOR}x  (saved: $OUT, $(wc -c <"$OUT") bytes)"

if [ "$FMT" = "verbose_json" ] || [ "$FMT" = "json" ]; then
  python3 - "$OUT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
print("top-level keys:", sorted(d.keys()))
segs = d.get("segments", [])
print(f"segments={len(segs)}")
for s in segs[:3]:
    print(json.dumps({k: v for k, v in s.items() if k != "tokens" and k != "words"}, ensure_ascii=False))
if segs:
    print("segment keys:", sorted(segs[0].keys()))
    print("start/end type:", type(segs[0].get("start")).__name__, "| last end:", segs[-1].get("end"))
print("text[:200]:", (d.get("text") or "")[:200])
PY
else
  echo "--- first 20 lines:"
  head -20 "$OUT"
fi
