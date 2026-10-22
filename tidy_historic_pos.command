#!/bin/bash
# Removes the made-up HIST-PO placeholder jobs from historic purchase orders.
# Shows you a preview first and takes a backup before changing anything. Close TEAMworks360 before running this.
set -e
cd "$(dirname "$0")"

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

if ! "$PYTHON" -c "import flask, reportlab, PIL, openpyxl, dateutil" >/dev/null 2>&1; then
    echo "Installing dependencies (first run only)..."
    "$PYTHON" -m pip install -r requirements.txt
fi

echo
echo "===== PREVIEW - nothing is changed yet ====="
"$PYTHON" tidy_historic_pos.py

echo
read -r -p "Type YES to apply these changes (a backup is taken first): " GO
if [ "$GO" != "YES" ]; then
    echo "Cancelled - nothing was changed."
    exit 0
fi
"$PYTHON" tidy_historic_pos.py --apply
