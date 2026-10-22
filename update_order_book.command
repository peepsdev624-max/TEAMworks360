#!/bin/bash
# Updates the app's Works Orders from a newer copy of the TEAM Order Book
# spreadsheet - safely: it adds new jobs and refreshes existing ones, shows
# you a preview first, takes a backup, and never deletes your customers,
# suppliers, purchase orders, goods in, despatch notes or quotes.
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
echo "Drag the new Order Book spreadsheet onto this window (or type its full path),"
echo "then press Enter."
read -r -p "Order Book file: " XL
XL="${XL%\"}"; XL="${XL#\"}"; XL="${XL%\'}"; XL="${XL#\'}"
XL="$(echo "$XL" | sed 's/\\ / /g; s/[[:space:]]*$//')"

echo
echo "===== PREVIEW - nothing is changed yet ====="
"$PYTHON" update_order_book.py "$XL"

echo
read -r -p "Type YES to apply these changes (a backup is taken first): " GO
if [ "$GO" != "YES" ]; then
    echo "Cancelled - nothing was changed."
    exit 0
fi
"$PYTHON" update_order_book.py "$XL" --apply
