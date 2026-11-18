#!/bin/bash
# Copies your customers and suppliers to another laptop (export here, import there).
# Close TEAMworks360 before running this.
cd "$(dirname "$0")"
PYTHON=python3
command -v python3 >/dev/null 2>&1 || PYTHON=python
"$PYTHON" share_customers_suppliers.py
