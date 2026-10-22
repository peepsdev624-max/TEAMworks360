#!/bin/bash
# Double-click to try TEAMworks360 on THIS computer only, with made-up demo
# data - no real TEAM information is in this copy at all. Everything you
# enter stays on this computer, in its own separate folder
# (~/TEAMworks360-Demo), and nothing is shared with anyone else.
#
# Sign in with:   tester  /  TestDrive2026
# You'll be asked to set up two-factor authentication the first time -
# have an authenticator app (Microsoft or Google Authenticator) on your phone.
#
# (First time on macOS: right-click -> Open, to get past the
# "unidentified developer" warning.)
set -e
cd "$(dirname "$0")"

export TEAMWORKS360_DATA_DIR="$HOME/TEAMworks360-Demo"

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python was not found. Install Python 3 from https://www.python.org/downloads/ and try again."
    read -p "Press Enter to close..."
    exit 1
fi

if ! "$PYTHON" -c "import flask, reportlab, PIL, openpyxl" >/dev/null 2>&1; then
    echo "Installing dependencies (first run only)..."
    "$PYTHON" -m pip install -r requirements.txt
fi

if ! "$PYTHON" -c "
import db_location, os, sys
db_location.ensure_data_dir()
sys.exit(0 if os.path.exists(db_location.DB_PATH) else 1)
" >/dev/null 2>&1; then
    echo "Setting up demo data for the first time..."
    "$PYTHON" seed.py
    "$PYTHON" add_test_user.py
fi

echo
echo "Starting the TEST COPY - your browser will open automatically."
echo "Sign in with:  tester  /  TestDrive2026"
echo
"$PYTHON" app.py
