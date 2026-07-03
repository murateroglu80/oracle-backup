#!/usr/bin/env bash

# Oracle RMAN Backup Script Runner (Multi-Instance, with Virtual Environment)
# Version: v7.0.0 (Faz 2)
#
# Usage:
#   ./run.sh --config config/db-server1_orcl1.yaml          # Backup single instance
#   ./run.sh --config config/db-server1_prod2.yaml --dry-run # Dry-run
#   ./run.sh --status                                        # Fleet status overview
#   ./run.sh --config config/db1.yaml --test-mail            # Test email
#   ./run.sh --config config/db1.yaml --test-db              # Test DB connection
#
# Config file discovery (if not found in given path):
#   1. Project root
#   2. config/ subdirectory
#
# Multi-instance: Each config/<instance>.yaml gets its own instance_id, logs, history.
# Org-wide: config/shared.yaml (MAIL_CONFIG + MONITORING_CONFIG) auto-merged per instance.

set -e

# Get the directory where the script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

VENV_DIR="venv"
REQ_FILE="requirements.txt"
SCRIPT_FILE="backup.py"
MIN_PYTHON="3.6"

# Cleanup function (guaranteed deactivate on exit)
cleanup() {
    if [ -n "$VIRTUAL_ENV" ]; then
        deactivate 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if [ "$(printf '%s\n' "$MIN_PYTHON" "$PYTHON_VERSION" | sort -V | head -n1)" != "$MIN_PYTHON" ]; then
    echo "[ERROR] Python 3.6+ required, found $PYTHON_VERSION"
    exit 1
fi

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

# Run the python script with unbuffered output (-u for real-time JSONL logging)
# and any arguments passed to run.sh
echo "[RUN] Executing $SCRIPT_FILE with args: $@"
python3 -u "$SCRIPT_FILE" "$@"
