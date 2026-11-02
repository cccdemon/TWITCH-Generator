#!/bin/sh
# whisper.cpp server entrypoint for ClipBox: fetch the ggml model once, then serve.
# Image: ghcr.io/ggml-org/whisper.cpp:main-vulkan (whisper-server and
# download-ggml-model.sh live in /usr/local/bin, models volume at /models).
set -eu

MODEL="${WHISPER_MODEL:-large-v3-turbo}"
LANG_OPT="${WHISPER_LANG:-de}"
THREADS="${WHISPER_THREADS:-4}"
FILE="/models/ggml-${MODEL}.bin"

if [ ! -s "$FILE" ]; then
  echo "whisper: downloading model ${MODEL} to /models ..."
  download-ggml-model.sh "$MODEL" /models
fi
ls -l "$FILE"

echo "whisper: starting whisper-server model=${MODEL} lang=${LANG_OPT} threads=${THREADS} extra='${WHISPER_EXTRA_ARGS:-}'"
# shellcheck disable=SC2086  # EXTRA_ARGS is meant to split
exec whisper-server -m "$FILE" --host 0.0.0.0 --port 8080 -l "$LANG_OPT" -t "$THREADS" ${WHISPER_EXTRA_ARGS:-}
