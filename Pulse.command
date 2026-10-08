#!/bin/bash
# Double-click this to open Pulse. The Python environment stays behind the app.
cd "$(dirname "$0")"
if ! curl -sf --max-time 1 http://127.0.0.1:8787/health >/dev/null; then
  .venv/bin/pulse serve --host 127.0.0.1 --port 8787 >/tmp/pulse.log 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    curl -sf --max-time 1 http://127.0.0.1:8787/health >/dev/null && break
    sleep 0.3
  done
fi
open "http://127.0.0.1:8787/sign-in"
