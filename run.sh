#!/bin/bash

SRC="$(dirname "$0")/src"

set -x
##### choose one:

PYTHONPATH="$SRC" python3 -m dwipe.main "$@"
# PYTHONPATH="$SRC" python3 src/dwipe/main.py "$@"
