#!/bin/bash
# Clears the hours booked on the Hours Quoted & Booked screen so the app can start
# capturing real hours. Shows a preview first and takes a backup before it changes anything.
# Close TEAMworks360 before running this.
set -e
cd "$(dirname "$0")"

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

echo
echo "===== PREVIEW - nothing is changed yet ====="
"$PYTHON" clear_hours.py

echo
read -r -p "Type YES to clear the booked hours (a backup is taken first): " GO
if [ "$GO" != "YES" ]; then
    echo "Cancelled - nothing was changed."
    exit 0
fi
"$PYTHON" clear_hours.py --apply
