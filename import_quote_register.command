#!/bin/bash
# Imports the TEAM Quote Register spreadsheet into the app's Quotes - safely:
# it only adds quotes (never changes one already there), shows you a preview
# first and takes a backup. It never deletes or changes your customers, works
# orders, purchase orders or existing quotes.
# Close TEAMworks360 before running this.
set -e
cd "$(dirname "$0")"

PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python

if ! "$PYTHON" -c "import flask, reportlab, PIL, openpyxl, dateutil" >/dev/null 2>&1; then
    echo "Installing dependencies (first run only)..."
    "$PYTHON" -m pip install -r requirements.txt
fi

echo
echo "Drag the Quote Register spreadsheet onto this window (or type its full path),"
echo "then press Enter."
read -r -p "Quote Register file: " XL
XL="${XL%\"}"; XL="${XL#\"}"; XL="${XL%\'}"; XL="${XL#\'}"
XL="$(echo "$XL" | sed 's/\\ / /g; s/[[:space:]]*$//')"

echo
echo "===== PREVIEW - nothing is changed yet ====="
"$PYTHON" import_quote_register.py "$XL"

echo
read -r -p "Type YES to apply these changes (a backup is taken first): " GO
if [ "$GO" != "YES" ]; then
    echo "Cancelled - nothing was changed."
    exit 0
fi
"$PYTHON" import_quote_register.py "$XL" --apply
