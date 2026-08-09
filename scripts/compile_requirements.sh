#!/bin/sh
set -eu

PYTHON_BIN=${PYTHON_BIN:-python}
export CUSTOM_COMPILE_COMMAND='scripts/compile_requirements.sh'

compile() {
    input=$1
    output=$2
    "$PYTHON_BIN" -m piptools compile \
        --resolver=backtracking \
        --generate-hashes \
        --strip-extras \
        --allow-unsafe \
        --quiet \
        --pip-args='--only-binary=:all:' \
        --output-file="$output" \
        "$input"
}

compile requirements/prod.in requirements/prod.txt
compile requirements/dev.in requirements/dev.txt
compile requirements/ci-tools.in requirements/ci-tools.txt
compile backup/requirements.in backup/requirements.txt
