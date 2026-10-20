#!/usr/bin/env bash
set -e; cd "$(dirname "$0")"
.venv/bin/python fetch.py
git add docs/data.json docs/index.html docs/icons data/manual.json data/iso2to3.json fetch.py asc_reports.py update.sh .gitignore
git commit -m "Update data $(TZ=America/New_York date '+%F %H:%M ET')" || exit 0
git push
