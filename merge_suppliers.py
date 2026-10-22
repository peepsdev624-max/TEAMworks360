"""
Merge duplicate suppliers (misspellings and name variants) into one record each.

Usage:
    python merge_suppliers.py            (preview only - changes nothing)
    python merge_suppliers.py --apply    (make the changes)

The PO import (and earlier Order Book imports) created a supplier for every
spelling it met, e.g. "Red Stawberry Solutions" next to "Red Strawberry Solutions".
This folds each listed duplicate into the supplier you actually keep:

  * every purchase order, supplier invoice and stock item that pointed at the
    duplicate now points at the one you keep (nothing is lost or re-priced);
  * any detail the kept supplier is missing (address, contact, email, VAT number,
    supply of, reason for use, authorised by) is filled in from the duplicate;
  * the duplicate is then removed.

Only the exact pairs in MERGES below are touched, and only when both names exist
in your database - so it is safe to run on any copy, and running it twice does
nothing the second time. In each pair the kept name is the one already on your
approved supplier register. After the merge it lists other suppliers whose names
look similar but which it did NOT merge, for you to check yourself.

--apply takes a timestamped backup of the database first and makes every change
in one go (all or nothing).
"""
import difflib
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location

# (duplicate to remove, supplier to keep)
MERGES = [
    ("Red Stawberry Solutions", "Red Strawberry Solutions"),
    ("Stonebridge testting", "Stonebridge Testing"),
    ("St. John Ambulance", "St Johns Ambulance"),
    ("Spring master", "Springmasters"),
    ("Flexo spring", "Flexo Springs"),
    ("Workwere express", "Workwear Express"),
    ("RG Engineering", "R G Engineering"),
    ("GoodHand", "Good HandUK"),
    ("Safety Lifting", "Safety Lifting Gear"),
    ("Spondon Blasting", "Spondon Blasting Services"),
    ("Mint Security", "Mint Security Systems Ltd"),
    ("HP Consulting", "HP Consulting Services Ltd"),
    ("Cutwel", "Cutwel Tools Ltd"),
    ("Orb Design & Print", "ORB Design"),
    ("Cell pack", "Cell Pack Solutions"),
    ("Euerby", "Euerby Engineering"),
    ("JPK Eng", "JKP Engineering"),
    ("Wells Fabs", "Wells Fabrications"),
    ("North Eastern Powder Coatings", "North Eastern Power Coatings"),
]
FILL_FIELDS = ["address", "contact_name", "email", "vat_number", "supply_of", "reason_for_use", "authorised_by"]
# Entries that look like notes rather than suppliers - only listed, never changed.
ODD_NAMES = ["Check with Dan", "B2B Quote", "Midlands", "Unknown Supplier"]


def referencing_columns(conn):
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if fk[2] == "suppliers":
                out.append((table, fk[3]))
    return out


def similar_key(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"\b(ltd|limited|lted|plc|inc|llp)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def main():
    apply_changes = "--apply" in sys.argv
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to merge.")
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    refs = referencing_columns(conn)
    try:
        conn.execute("BEGIN")
        by_name = {r["supplier_name"]: r for r in conn.execute("SELECT * FROM suppliers")}
        merged = []
        for dup, keep in MERGES:
            d, k = by_name.get(dup), by_name.get(keep)
            if not d or not k or d["supplier_id"] == k["supplier_id"]:
                continue
            moved = 0
            for table, col in refs:
                moved += conn.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (k["supplier_id"], d["supplier_id"])).rowcount
            for f in FILL_FIELDS:
                if not (k[f] or "").strip() and (d[f] or "").strip():
                    conn.execute(f"UPDATE suppliers SET {f}=? WHERE supplier_id=?", (d[f], k["supplier_id"]))
            conn.execute("DELETE FROM suppliers WHERE supplier_id=?", (d["supplier_id"],))
            conn.execute(
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                ("suppliers", k["supplier_id"], "merged duplicate supplier", dup, keep, "merge_suppliers.py"))
            merged.append((dup, keep, moved))

        print(f"\n{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}")
        if merged:
            print("Merged duplicates:" if apply_changes else "Will merge:")
            for dup, keep, moved in merged:
                print(f"  - '{dup}' -> '{keep}'  ({moved} record(s) moved)")
        else:
            print("  Nothing to merge - none of the known duplicates are in this database.")

        remaining = [r for r in conn.execute("SELECT supplier_id, supplier_name FROM suppliers")]
        keys = [(r["supplier_name"], similar_key(r["supplier_name"])) for r in remaining]
        similar = []
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i][1], keys[j][1]
                if len(a) < 5 or len(b) < 5:
                    continue
                if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.86 \
                        or (min(len(a), len(b)) >= 6 and (a.startswith(b) or b.startswith(a))):
                    similar.append((keys[i][0], keys[j][0]))
        if similar:
            print("\nLook similar but NOT merged (check these yourself - some are genuinely different companies):")
            for a, b in similar:
                print(f"  - '{a}'  /  '{b}'")
        names = {r["supplier_name"] for r in remaining}
        odd = [n for n in ODD_NAMES if n in names]
        if odd:
            print("\nThese look like notes rather than suppliers: " + ", ".join(f"'{n}'" for n in odd))

        if apply_changes:
            conn.commit()
            print("\nDone. Changes saved.")
        else:
            conn.rollback()
            print("\nNothing was changed. Run again with --apply to make these changes.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    if "--apply" in sys.argv and Path(db_location.DB_PATH).exists():
        db_location.backup_db()
        print(f"Backup of your database taken in {db_location.BACKUP_DIR}")
    main()
