#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

git pull
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart codebase-searcher
sudo systemctl status codebase-searcher --no-pager
