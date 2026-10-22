"""
Remove the made-up "HIST-PO-..." placeholder jobs from the historic purchase orders.

Usage:
    python tidy_historic_pos.py            (preview only - changes nothing)
    python tidy_historic_pos.py --apply    (make the changes)

When the Purchase Order Register was imported, every old PO whose job isn't in the
app was parked on a stand-in job called HIST-PO-<CUSTOMER>, which made the data messy
and looked like jobs had been given TM numbers. This tool:

  * detaches those POs from the stand-in job (they simply have no job);
  * keeps the register's own job reference (e.g. TM-1510) as plain text in the PO's
    "Old job ref" field, taken out of the Description where it was buried;
  * does NOT keep a reference to a job number higher than the highest job actually
    raised in the app (a job that doesn't exist yet) - those are listed for checking;
  * then deletes each HIST-PO job, but only once nothing at all points at it;
  * strips register clutter from descriptions: "date sent in register: ..." notes and
    spreadsheet errors such as "#DIV/0!";
  * removes the blank, pre-numbered rows at the END of the register (no supplier, no
    description, no value, no date - e.g. PO-3238 onwards). A PO number is only issued
    when a PO is actually raised, so these must not use up numbers - the next PO number
    is the one after the last REAL purchase order.

No purchase order is deleted or re-priced, and none of them counts toward job costs
(they are history only). The old wording of each change is kept in the audit log and
in historic_po_tidy_report.csv in your data folder.

--apply takes a timestamped backup first and makes every change in one go.
"""
import csv
import re
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location
import migrations
from import_po_register import clean_register_ref, EXCEL_ERROR

REPORT_NAME = "historic_po_tidy_report.csv"
CHECK_NAME = "purchase_orders_to_check.csv"
LABEL_ONLY = re.compile(r"^(Site|Contact|End customer|Qty|Part numbers|Supplier quote|Register WO|date sent in register|value in register):", re.I)
NOISE = re.compile(r"^(date sent in register:|value in register: ')", re.I)
REG_NOTE = re.compile(r"^\s*Register WO:\s*(.*?)\s*$")


def referencing_columns(conn, target):
    out = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if fk[2] == target:
                out.append((table, fk[3]))
    return out


def tidy(conn):
    """Does all the clean-up on an open connection (inside whatever transaction the
    caller has started) and returns what it did. The caller commits or rolls back."""
    hi = [int(m.group(1)) for r in conn.execute("SELECT job_number FROM works_orders")
          for m in [re.match(r"^TM-(\d+)$", r["job_number"])] if m]
    highest = max(hi) if hi else 0
    holders = {r["works_order_id"]: r["job_number"] for r in conn.execute(
        "SELECT works_order_id, job_number FROM works_orders WHERE job_number LIKE 'HIST-PO-%'")}
    pos = conn.execute(
        f"SELECT purchase_order_id, po_number, works_order_id, description FROM purchase_orders "
        f"WHERE is_historic=1 AND works_order_id IN ({','.join('?' * len(holders)) or 'NULL'})", list(holders)).fetchall()

    rows, future = [], []
    for p in pos:
        desc = p["description"] or ""
        parts = desc.split(" | ")
        raw, kept_parts = "", []
        for part in parts:
            m = REG_NOTE.match(part)
            if m and not raw:
                raw = m.group(1)
            else:
                kept_parts.append(part)
        ref, fut = clean_register_ref(raw, highest)
        new_desc = " | ".join(x for x in kept_parts if x.strip()) or None
        if fut:
            future.append((p["po_number"], fut))
        rows.append((p["purchase_order_id"], p["po_number"], holders[p["works_order_id"]], ref, fut, new_desc))
        conn.execute("UPDATE purchase_orders SET works_order_id=NULL, register_job_ref=?, description=? WHERE purchase_order_id=?",
                     (ref, new_desc, p["purchase_order_id"]))
        conn.execute(
            "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
            ("purchase_orders", p["purchase_order_id"], "job", holders[p["works_order_id"]],
             f"(none) old ref: {ref or '-'}", "tidy_historic_pos.py"))

    # ---- clutter in descriptions
    cleaned = 0
    for p in conn.execute("SELECT purchase_order_id, description FROM purchase_orders WHERE is_historic=1 AND description IS NOT NULL").fetchall():
        parts = (p["description"] or "").split(" | ")
        keep = []
        for part in parts:
            tail = part.split(":", 1)[1].strip(" '") if ":" in part else part
            if NOISE.match(part) or EXCEL_ERROR.match(tail) or EXCEL_ERROR.match(part.strip()):
                continue
            keep.append(part)
        if len(keep) != len(parts):
            conn.execute("UPDATE purchase_orders SET description=? WHERE purchase_order_id=?", (" | ".join(keep) or None, p["purchase_order_id"]))
            cleaned += 1

    # ---- blank pre-numbered rows after the last real PO
    def po_n(num):
        m = re.match(r"^PO-(\d+)$", num or "")
        return int(m.group(1)) if m else None

    used = ("goods_in", "supplier_invoices", "purchase_order_lines")
    def is_blank(r):
        # nothing real on the row: unknown supplier, no value, no date, and no description
        # beyond register labels (Site:/Contact:/Register WO: ...) - an old job reference is not content
        segs = [x for x in (r["description"] or "").split(" | ") if x.strip()]
        only_labels = all(LABEL_ONLY.match(x) for x in segs)
        return (r["is_historic"] == 1 and only_labels and not r["order_date"] and not r["po_value"]
                and r["supplier_name"] == "Unknown Supplier")
    allpos = conn.execute("""SELECT p.purchase_order_id, p.po_number, p.description, p.order_date, p.po_value, p.is_historic,
                                    p.register_job_ref, s.supplier_name FROM purchase_orders p JOIN suppliers s USING(supplier_id)""").fetchall()
    real_max = max([po_n(r["po_number"]) or 0 for r in allpos if not is_blank(r)] or [0])
    blanks = []
    for r in allpos:
        n = po_n(r["po_number"])
        if is_blank(r) and (n is None or n > real_max):      # trailing blank run, or a blank row with a non-PO number
            if any(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE purchase_order_id=?", (r["purchase_order_id"],)).fetchone()[0] for t in used):
                continue
            blanks.append(r)
    for r in blanks:
        conn.execute("DELETE FROM purchase_orders WHERE purchase_order_id=?", (r["purchase_order_id"],))
        conn.execute(
            "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
            ("purchase_orders", r["purchase_order_id"], "blank pre-numbered row", r["po_number"], "(removed)", "tidy_historic_pos.py"))

    refs = referencing_columns(conn, "works_orders")
    removed, kept = [], []
    for wid, name in holders.items():
        left = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c}=?", (wid,)).fetchone()[0] for t, c in refs)
        if left:
            kept.append((name, left))
        else:
            conn.execute("DELETE FROM works_orders WHERE works_order_id=?", (wid,))
            removed.append(name)
    unknown_left = [(r[0], r[1]) for r in conn.execute(
        "SELECT p.po_number, p.is_historic FROM purchase_orders p JOIN suppliers s USING(supplier_id) "
        "WHERE s.supplier_name='Unknown Supplier' ORDER BY p.po_number")]
    return dict(unknown_left=unknown_left, rows=rows, future=future, removed=removed, kept=kept, cleaned=cleaned,
                blanks=blanks, real_max=real_max, highest=highest)


def needs_tidy(conn):
    """True if this database still has anything for tidy() to do."""
    if conn.execute("SELECT 1 FROM works_orders WHERE job_number LIKE 'HIST-PO-%' LIMIT 1").fetchone():
        return True
    if conn.execute("SELECT 1 FROM purchase_orders WHERE is_historic=1 AND (description LIKE '%date sent in register%' "
                    "OR description LIKE '%#DIV/0!%' OR description LIKE '%#N/A%' OR description LIKE '%#REF!%' "
                    "OR description LIKE '%#VALUE!%') LIMIT 1").fetchone():
        return True
    if conn.execute("""SELECT 1 FROM purchase_orders p JOIN suppliers s USING(supplier_id)
                       WHERE p.is_historic=1 AND COALESCE(p.description,'')='' AND p.order_date IS NULL
                         AND p.po_value=0 AND s.supplier_name='Unknown Supplier' LIMIT 1""").fetchone():
        return True
    return False


def main():
    apply_changes = "--apply" in sys.argv
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to tidy.")
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.ensure_schema_current(conn)     # same small changes the app makes on start-up
    migrations.relax_po_job_link(conn)
    try:
        conn.execute("BEGIN")
        result = tidy(conn)
        rows, future, removed, kept = result["rows"], result["future"], result["removed"], result["kept"]
        cleaned, blanks, real_max, highest = result["cleaned"], result["blanks"], result["real_max"], result["highest"]

        db_location.ensure_data_dir()
        report = Path(db_location.DATA_DIR) / REPORT_NAME
        with open(report, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["PO", "Was on", "Old job ref kept", "Reference dropped (job not raised)"])
            for _, po, was, ref, fut, _d in rows:
                wr.writerow([po, was, ref or "", fut or ""])

        print(f"\n{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}")
        print(f"  Purchase orders detached from a stand-in job: {len(rows)}")
        print(f"    with an old job reference kept as text:     {sum(1 for r in rows if r[3])}")
        print(f"    with no job reference at all:               {sum(1 for r in rows if not r[3] and not r[4])}")
        print(f"  Stand-in HIST-PO jobs removed: {len(removed)}" + (f"  ({', '.join(removed)})" if removed else ""))
        print(f"  Descriptions cleared of register clutter (date-sent notes, #DIV/0! etc.): {cleaned}")
        if blanks:
            print(f"  Blank pre-numbered rows removed: {len(blanks)}  ({min(b['po_number'] for b in blanks)} to {max(b['po_number'] for b in blanks)})")
        ul = result["unknown_left"]
        print(f"  Purchase orders still showing 'Unknown Supplier': {len(ul)}" + (f"   e.g. {', '.join(p for p, _h in ul[:8])}" if ul else ""))
        print(f"  Last real purchase order: PO-{real_max}   ->   the next PO number will be PO-{real_max + 1}")
        for name, n in kept:
            print(f"  Kept {name}: {n} other record(s) still point at it")
        if future:
            print(f"\n  References to jobs that haven't been raised yet (highest job in the app is TM-{highest}) - NOT kept:")
            for po, fut in future:
                print(f"    {po}: {fut}")
        check = Path(db_location.DATA_DIR) / CHECK_NAME
        with open(check, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["PO number", "Supplier", "Job", "Value", "Order date", "Historic", "Old job ref", "Description"])
            for r in conn.execute("""SELECT p.po_number, s.supplier_name, w.job_number, p.po_value, p.order_date, p.is_historic,
                                            p.register_job_ref, p.description
                                     FROM purchase_orders p JOIN suppliers s USING(supplier_id)
                                     LEFT JOIN works_orders w ON w.works_order_id = p.works_order_id
                                     WHERE s.supplier_name = 'Unknown Supplier' OR p.po_number NOT GLOB 'PO-[0-9]*'
                                     ORDER BY p.po_number"""):
                wr.writerow(list(r))
        print(f"\nFull list: {report}")
        print(f"POs to check by eye (unknown supplier, or a number that isn't PO-nnnn): {check}\n")
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
