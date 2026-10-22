"""
Tidy the "Raised By" names on purchase orders so each person appears once.

Usage:
    python tidy_raised_by.py            (preview only - changes nothing)
    python tidy_raised_by.py --apply    (make the changes)

The imported PO history spells the same person many ways ("S.Lee", "S Lee",
"Simon Lee"; "Craig", "C Sansom", "Craig sansom"; ...). This maps each spelling to
one full name using the table in PEOPLE below, so the Raised By filter on the
Purchase Orders page lists each person once.

Rules:
  * Where an entry names two people ("S Lee/K Hallam", "KH/JM/TB") the person
    who is NOT K Hallam is used (K Hallam is the name that appears alongside
    nearly everyone, so she is treated as the co-signer); with no Hallam the
    first-named person is used.
  * Anything it doesn't recognise ("T Bentley Jrn", "A & R Jackson", "Steve Euerby",
    blanks) is left exactly as it was and listed, for you to decide.
  * The original wording of every changed PO is kept in the audit log and in
    raised_by_tidy_report.csv in your data folder, so it can always be put back.

--apply takes a timestamped backup first and changes everything in one go.
"""
import csv
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location

REPORT_NAME = "raised_by_tidy_report.csv"

# full name -> every spelling / initials that means that person (compared ignoring
# case, full stops and extra spaces)
PEOPLE = {
    "Steve Campbell": ["S Campbell", "Steve Campbell", "SC"],
    "Simon Lee": ["S Lee", "Slee", "Simon Lee", "SL"],
    "Craig Sansom": ["Craig Sansom", "Craig", "C Sansom", "Criag Sansom", "CS"],
    "Martin Roe": ["Martin Roe", "M Roe", "MRoe", "MR"],
    "Stuart Rochester": ["S Rochester", "Stuart Rochester"],
    "John Murray": ["John Murray", "John Murry", "J Murray", "J Murry", "JM"],
    "Wayne Gronbach": ["Wayne Gronbach", "Wayne"],
    "Alistair Wicks": ["Alistair Wicks"],
    "Kay Hallam": ["K Hallam", "Kay Hallam", "KHallam", "KH"],
    "Tom Bentley": ["T Bentley", "Tom Bentley", "Thomas Bentley", "TB"],
    "Daniel Lee": ["D Lee", "Dan Lee", "Daniel Lee", "Dan", "DL"],
    "Andy Jackson": ["A Jackson", "Andy Jackson"],
    "Rob Jackson": ["Rob Jackson", "RJ"],
    "Tom Davies": ["Tom Davies", "T Davies"],
    "Jake Bonallie": ["J Bonallie", "Jake Bonallie"],
}
HALLAM = "Kay Hallam"


def norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[.]", " ", (s or "").lower())).strip()


LOOKUP = {norm(a): full for full, aliases in PEOPLE.items() for a in aliases}


def canonical(raised_by):
    """Returns the one-name version, or None if it should be left alone."""
    text = (raised_by or "").strip()
    if not text:
        return None
    names = []
    for part in text.split("/"):
        full = LOOKUP.get(norm(part))
        if full is None:
            return None          # something we don't recognise - leave the whole entry alone
        names.append(full)
    others = [n for n in names if n != HALLAM]
    return (others or names)[0]


def main():
    apply_changes = "--apply" in sys.argv
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to tidy.")
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        changes = Counter()          # (old, new) -> POs
        untouched = Counter()
        ids = []
        for r in conn.execute("SELECT purchase_order_id, raised_by FROM purchase_orders").fetchall():
            old = r["raised_by"]
            new = canonical(old)
            if new is None:
                if (old or "").strip():
                    untouched[old] += 1
                continue
            if new == old:
                continue
            changes[(old, new)] += 1
            ids.append((r["purchase_order_id"], old, new))
        for pid, old, new in ids:
            conn.execute("UPDATE purchase_orders SET raised_by=? WHERE purchase_order_id=?", (new, pid))
            conn.execute(
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                ("purchase_orders", pid, "raised_by", old, new, "tidy_raised_by.py"))

        db_location.ensure_data_dir()
        report = Path(db_location.DATA_DIR) / REPORT_NAME
        with open(report, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["Was", "Now", "POs"])
            for (old, new), n in sorted(changes.items(), key=lambda x: (x[0][1], -x[1])):
                wr.writerow([old, new, n])
            wr.writerow([])
            wr.writerow(["Left alone (not recognised)", "POs"])
            for old, n in untouched.most_common():
                wr.writerow([old, n])

        print(f"\n{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}")
        print(f"  POs renamed: {len(ids)}   (spellings merged: {len(changes)})")
        by_new = Counter()
        for (old, new), n in changes.items():
            by_new[new] += n
        for new, n in by_new.most_common():
            olds = ", ".join(f"'{o}'" for (o, nw), k in sorted(changes.items(), key=lambda x: -x[1]) if nw == new)
            print(f"    {new:<18} {n:>5}  <- {olds}")
        if untouched:
            print("\n  Left alone (not recognised - decide these yourself):")
            for old, n in untouched.most_common():
                print(f"    {n:>5}  {old}")
        print(f"\nFull list: {report}\n")
        if apply_changes:
            conn.commit()
            print("Done. Changes saved.")
        else:
            conn.rollback()
            print("Nothing was changed. Run again with --apply to make these changes.")
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
