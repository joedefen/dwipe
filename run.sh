#!/bin/bash

SRC="$(dirname "$0")/src"

set -x
##### choose one:

PYTHONPATH="$SRC" python3 -m zapdev.main "$@"
# PYTHONPATH="$SRC" python3 src/zapdev/main.py "$@"
