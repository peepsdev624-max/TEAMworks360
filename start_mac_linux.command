#!/bin/bash
# Double-click this file to run a separate, LOCAL-ONLY copy of
# TEAMworks360 on just this computer - its own database, not
# connected to anyone else. If TEAM has a shared host set up, use
# open_shared_app.command instead so you see (and add to) the real shared data.
# (First time on macOS: right-click -> Open, to get past the "unidentified
# developer" warning - you only need to do that once.)
set -e
cd "$(dirname "$0")"

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Python was not found. Install Python 3 from https://www.python.org/downloads/ and try again."
    read -p "Press Enter to close..."
    exit 1
fi

# Skip the dependency check entirely if it's already satisfied - makes
# every run after the first one noticeably faster. IMPORTANT: this list
# must include every package requirements.txt adds, or a newly-added
# dependency silently won't get installed on an existing setup - keep it
# in sync whenever requirements.txt changes.
if ! "$PYTHON" -c "import flask, reportlab, PIL, openpyxl" >/dev/null 2>&1; then
    echo "Installing dependencies (first run only)..."
    "$PYTHON" -m pip install -r requirements.txt
fi

# The database lives in a stable location outside this folder (see
# db_location.py) so that replacing this whole folder to install an update
# never touches it. This delegates the "does it exist yet" check to Python
# so it's never out of sync with what app.py/seed.py actually use - and it
# also runs the one-time move of a database from an older version of this
# app that still had it sitting right here next to the code.
if ! "$PYTHON" -c "
import db_location, os, sys
db_location.migrate_from_old_product_name()
db_location.migrate_legacy_files(os.getcwd())
sys.exit(0 if os.path.exists(db_location.DB_PATH) else 1)
" >/dev/null 2>&1; then
    echo
    echo "======================================================"
    echo "  No database found on this computer"
    echo "======================================================"
    echo
    echo "TEAM normally runs ONE SHARED copy of this app from a single host"
    echo "computer, with everyone else just opening a browser to it - if"
    echo "that's already set up, use \"Open TEAMworks360\""
    echo "(open_shared_app.command) instead of this file."
    echo
    echo "Continuing here creates a brand new, SEPARATE copy of the app"
    echo "with its own database on THIS computer only. Anything entered"
    echo "here - customers, suppliers, jobs, users, everything - stays on"
    echo "this computer and will NOT be seen by anyone else."
    echo
    read -p "Type YES to continue creating a new local-only copy: " CONFIRM
    if [ "$CONFIRM" != "YES" ]; then
        echo "Cancelled - nothing was created."
        read -p "Press Enter to close..." || true
        exit 0
    fi
    echo "Setting up a new local-only copy..."
    "$PYTHON" seed.py
    "$PYTHON" import_team_order_book.py
fi

echo "Starting TEAMworks360 - your browser will open automatically..."
"$PYTHON" app.py
