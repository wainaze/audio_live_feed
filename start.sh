#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

if [ -f config.env ]; then
  set -a
  source config.env
  set +a
fi

source venv/bin/activate

python main.py