#!/usr/bin/env bash
set -e; cd "$(dirname "$0")"
.venv/bin/python fetch.py
git add docs/data.json docs/icons
git commit -m "Update data $(TZ=America/New_York date '+%F %H:%M ET')" || exit 0
git push
