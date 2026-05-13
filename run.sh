#!/bin/bash
# launchd wrapper — long-running poll loop.
# Switched from per-tick `once` (launchd StartInterval=30s) to a persistent
# `loop` because 30s polling can't beat push-notification users — by the time
# we scanned, posts already had 9-36 comments. The plist now uses KeepAlive
# instead of StartInterval; if this exits, launchd restarts it.
set -e
cd "$(dirname "$0")"

ARM=1            # set to 0 for dry-run
INTERVAL=2       # seconds between scan ticks

source .venv/bin/activate

if [ "$ARM" = "1" ]; then
  exec python src/sniper.py loop --arm --interval "$INTERVAL"
else
  exec python src/sniper.py loop --interval "$INTERVAL"
fi
