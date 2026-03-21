#!/bin/bash
#
# Beekeeper CLI Admin Tool Wrapper
# Activates the virtual environment and runs admin.py
#

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Path to venv and admin.py
VENV_DIR="$SCRIPT_DIR/venv"
ADMIN_PY="$SCRIPT_DIR/admin.py"

# Check if venv exists
if [ ! -d "$VENV_DIR" ]; then
    echo "Error: Virtual environment not found at $VENV_DIR" >&2
    echo "Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt" >&2
    exit 1
fi

# Check if admin.py exists
if [ ! -f "$ADMIN_PY" ]; then
    echo "Error: admin.py not found at $ADMIN_PY" >&2
    exit 1
fi

# Activate venv and run admin.py with all arguments
source "$VENV_DIR/bin/activate"
python "$ADMIN_PY" "$@"
