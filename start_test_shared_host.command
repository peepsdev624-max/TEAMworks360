#!/bin/bash
# Starts a separate, ISOLATED TEST COPY of the app - for trying things out
# or letting a few colleagues poke at a sandbox without any risk to the
# real data. See README.md, "Test copy (sandbox)".
#
# This uses its own data folder (~/TEAMworks360-Test, not ~/TEAMworks360)
# and its own port (5050, not 5000), so it can run side-by-side with the
# real start_shared_host.command on the same machine without the two ever
# colliding or mixing data.
#
# The very first time this runs, it seeds the test folder with a one-time
# copy of real data from test_seed_data/jpm.db (see that folder's
# README.txt for exactly what that means and how to refresh it later).
# After that first copy, this test copy lives completely on its own:
# nothing done here ever reaches the real database, and nothing added to
# the real database afterwards will ever appear here.
#
# Use start_shared_host.command instead of this if you want to host the
# real, live system that everyone's actual work goes into.
#
# (First time on macOS: right-click -> Open, to get past the
# "unidentified developer" warning - you only need to do that once.)
set -e
cd "$(dirname "$0")"

export TEAMWORKS360_DATA_DIR="$HOME/TEAMworks360-Test"

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
# in sync whenever requirements.txt changes (same list start_shared_host.command
# checks, since this also runs via serve_shared.py / waitress).
if ! "$PYTHON" -c "import flask, reportlab, PIL, openpyxl, waitress" >/dev/null 2>&1; then
    echo "Installing dependencies (first run only)..."
    "$PYTHON" -m pip install -r requirements.txt
fi

# Delegate "does the test copy already have its own database" to Python,
# same as the real launchers do, so this is never out of sync with what
# app.py/db_location.py actually use. Deliberately does NOT call
# migrate_from_old_product_name() or migrate_legacy_files() - those are
# one-time moves for the REAL data folder only and have nothing to do
# with this separate test folder.
if ! "$PYTHON" -c "
import db_location, os, sys
db_location.ensure_data_dir()
sys.exit(0 if os.path.exists(db_location.DB_PATH) else 1)
" >/dev/null 2>&1; then
    if [ -f "test_seed_data/jpm.db" ]; then
        echo "Setting up the test copy for the first time - seeding it from today's snapshot of the real data..."
        "$PYTHON" -c "
import db_location, shutil
db_location.ensure_data_dir()
shutil.copy2('test_seed_data/jpm.db', db_location.DB_PATH)
db_location.harden_file_permissions()
"
    else
        echo "No seed database found at test_seed_data/jpm.db - starting with a brand new, empty test copy instead."
        "$PYTHON" seed.py
    fi
fi

# A different default port from the real shared host (5000), so the two
# can run at the same time on the same machine without clashing.
export FLASK_PORT="${FLASK_PORT:-5050}"

"$PYTHON" serve_shared.py
