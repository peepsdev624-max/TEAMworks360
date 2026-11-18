"""Share your customers and suppliers with another TEAMworks360 installation (for example a colleague's laptop).

Two steps:
  1. EXPORT on the laptop that has the lists. It writes one small file (TEAMworks360-customers-suppliers.json)
     to the Desktop. It holds names, addresses, contacts, e-mail, phone, VAT number and payment terms - nothing
     else: no jobs, prices, invoices or users.
  2. IMPORT on the other laptop. It first shows a preview. Anything with a name the other laptop already has
     (ignoring capital letters and extra spaces) is left exactly as it is; only the missing ones are added.
     A backup of the database is taken before anything is changed. Close TEAMworks360 before importing.

Run it with no arguments for a simple menu (that is what the .bat / .command files do), or:

    python share_customers_suppliers.py export [file]
    python share_customers_suppliers.py import file [--apply]
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import db_location

FILE_NAME = "TEAMworks360-customers-suppliers.json"
CUSTOMER_FIELDS = ["customer_name", "address", "contact_name", "email", "phone", "payment_terms_days", "payment_terms_eow"]
SUPPLIER_FIELDS = ["supplier_name", "supply_of", "reason_for_use", "authorised_by", "address", "contact_name", "email", "vat_number"]


def desktop_folder():
    """The Desktop the person actually sees. With OneDrive backing up the Desktop (common on Windows) that is
    ~/OneDrive/Desktop (or "OneDrive - <company>/Desktop"), not ~/Desktop."""
    home = Path.home()
    candidates = [p / "Desktop" for p in sorted(home.glob("OneDrive*"))] + [home / "Desktop"]
    for c in candidates:
        if c.is_dir():
            return c
    return home


def default_file():
    return desktop_folder() / FILE_NAME


def show_in_folder(path):
    """Best-effort: open the folder holding the file so it can be found at once."""
    import os, subprocess
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif os.environ.get("DISPLAY"):
            subprocess.Popen(["xdg-open", str(Path(path).parent)])
    except OSError:
        pass


def key(name):
    return " ".join((name or "").lower().split())


def existing_columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def export_data(conn):
    conn.row_factory = sqlite3.Row
    out = {"exported_from": "TEAMworks360", "exported_on": datetime.now().strftime("%Y-%m-%d %H:%M"), "customers": [], "suppliers": []}
    for table, fields, order in (("customers", CUSTOMER_FIELDS, "customer_name"), ("suppliers", SUPPLIER_FIELDS, "supplier_name")):
        have = existing_columns(conn, table)
        cols = [f for f in fields if f in have]
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {order}"):
            out[table].append({f: r[f] for f in cols})
    return out


def plan_import(conn, data):
    """What an import would do: which rows are new and which the database already has."""
    result = {}
    for table, name_col in (("customers", "customer_name"), ("suppliers", "supplier_name")):
        have = {key(r[0]) for r in conn.execute(f"SELECT {name_col} FROM {table}")}
        new, seen = [], set()
        skipped = 0
        for row in data.get(table, []):
            k = key(row.get(name_col))
            if not k or k in seen:
                continue
            seen.add(k)
            if k in have:
                skipped += 1
            else:
                new.append(row)
        result[table] = {"new": new, "already_there": skipped}
    return result


def apply_import(conn, plan):
    added = {"customers": 0, "suppliers": 0}
    for table, fields in (("customers", CUSTOMER_FIELDS), ("suppliers", SUPPLIER_FIELDS)):
        have = existing_columns(conn, table)
        for row in plan[table]["new"]:
            cols = [f for f in fields if f in have and f in row]
            cols += ["created_by", "created_date"] if "created_by" in have else []
            vals = [row[f] for f in cols if f not in ("created_by", "created_date")]
            if "created_by" in have:
                vals += ["Shared list import", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
            conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
            added[table] += 1
    conn.commit()
    return added


def print_plan(plan, apply_changes):
    print(f"\n{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}")
    for table, label in (("customers", "Customers"), ("suppliers", "Suppliers")):
        p = plan[table]
        print(f"  {label}: {len(p['new'])} to add, {p['already_there']} already here (left as they are)")
        for r in p["new"][:15]:
            print("     +", r.get("customer_name") or r.get("supplier_name"))
        if len(p["new"]) > 15:
            print(f"     ... and {len(p['new']) - 15} more")


def do_export(path):
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to export.")
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    try:
        data = export_data(conn)
    finally:
        conn.close()
    path = Path(path)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nExported {len(data['customers'])} customers and {len(data['suppliers'])} suppliers.")
    print(f"File: {path.resolve()}\nCopy that one file to the other laptop (USB stick, e-mail or shared folder), then run Import there.")


def do_import(path, apply_changes, confirm=None):
    path = Path(str(path).strip().strip('"'))
    if not path.exists():
        sys.exit(f"File not found: {path}")
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH}. Start TEAMworks360 once on this laptop first, then close it and run this again.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("exported_from") != "TEAMworks360":
        sys.exit("That file doesn't look like a TEAMworks360 customers-and-suppliers export.")
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    try:
        plan = plan_import(conn, data)
        print_plan(plan, apply_changes)
        total = sum(len(plan[t]["new"]) for t in plan)
        if not apply_changes:
            return plan
        if total == 0:
            print("\nNothing to add - this laptop already has all of them.")
            return plan
        if confirm is not None and not confirm():
            print("Cancelled - nothing was changed.")
            return plan
        db_location.backup_db()
        print("\nA backup of the database was taken first (in the backups folder next to it).")
        added = apply_import(conn, plan)
        print(f"Done. Added {added['customers']} customers and {added['suppliers']} suppliers.")
        return plan
    finally:
        conn.close()


def menu():
    print("\nShare customers and suppliers\n  1 = EXPORT (on the laptop that has the lists)\n  2 = IMPORT (on the laptop that needs them)")
    choice = input("Type 1 or 2: ").strip()
    if choice == "1":
        do_export(default_file())
        show_in_folder(default_file())
    elif choice == "2":
        shown = default_file()
        p = input(f"Drag the file into this window, or press Enter to use {shown}: ").strip().strip('"') or str(shown)
        plan = do_import(p, False)
        if sum(len(plan[t]["new"]) for t in plan):
            if input("\nType YES to add these (a backup is taken first): ").strip() == "YES":
                do_import(p, True)
            else:
                print("Cancelled - nothing was changed.")
    else:
        print("Cancelled.")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        return menu()
    if args[0] == "export":
        target = args[1] if len(args) > 1 else default_file()
        do_export(target)
        return show_in_folder(target)
    if args[0] == "import" and len(args) > 1:
        return do_import(args[1], "--apply" in sys.argv)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
