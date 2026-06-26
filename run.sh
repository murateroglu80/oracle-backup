#!/usr/bin/env bash

# RMAN Backup Script Runner (with Virtual Environment)
# Usage: ./run.sh [--dry-run] [--test-mail] [--test-transfer] [--test-db]

set -e

# Get the directory where the script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

VENV_DIR="venv"
REQ_FILE="requirements.txt"
SCRIPT_FILE="backup.py"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "[SETUP] Virtual environment not found. Creating one..."
    python3 -m venv "$VENV_DIR"
fi

# Activate venv
echo "[SETUP] Activating virtual environment..."
source "$VENV_DIR/bin/activate"

# Install or upgrade requirements if requirements.txt exists
if [ -f "$REQ_FILE" ]; then
    echo "[SETUP] Checking dependencies..."
    pip install -q --upgrade pip
    pip install -q -r "$REQ_FILE"
fi

# Run the python script with any arguments passed to run.sh
echo "[RUN] Executing $SCRIPT_FILE..."
python3 "$SCRIPT_FILE" "$@"

# Deactivate venv
deactivate
