#!/usr/bin/env bash
# Stop whatever scripts/dev.sh started.
set -uo pipefail
pkill -f "jobhunt serve" && echo "api: stopped" || echo "api: not running"
pkill -f "vite" && echo "ui: stopped" || echo "ui: not running"
