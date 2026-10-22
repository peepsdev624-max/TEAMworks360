#!/bin/bash
# Double-click this file ONLY on the one computer that should be the
# shared host - this machine becomes the server, holding the one real
# database everyone shares, and everyone else just opens a browser to
# the address this prints. They don't need Python, this folder, or their
# own copy of the app - they should use open_shared_app.command instead.
#
# Use start_mac_linux.command instead of this if you specifically want a
# separate, local-only copy for yourself (e.g. testing), with nobody
# else connecting to it and nothing shared with the real data.
#
# (First time on macOS: right-click -> Open, to get past the
# "unidentified developer" warning - you only need to do that once.)
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
# in sync whenever requirements.txt changes. waitress is the one this
# launcher needs beyond what the single-user launcher checks for.
if ! "$PYTHON" -c "import flask, reportlab, PIL, openpyxl, waitress" >/dev/null 2>&1; then
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
    echo "No database found - setting up for the first time..."
    "$PYTHON" seed.py
    "$PYTHON" import_team_order_book.py
fi

"$PYTHON" serve_shared.py
