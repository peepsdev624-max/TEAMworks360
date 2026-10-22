"""
Import the TEAM Purchase Order Register into the app as HISTORY.

Every PO is brought in flagged "Historic": you can find and filter them on the
Purchase Orders page, but they are NOT counted in any job's costs, commitments
or margins (the Order Book already carries those costs, and counting both would
double them). When you go live and start raising POs in the app, those work
exactly as before.

Safe to run on a live database: it only ADDS purchase orders. A PO number that
is already in the app is never changed, and running it twice adds nothing.

Usage:
    python import_po_register.py path/to/Team_Purchase_Order_Register.xlsx            (preview only)
    python import_po_register.py path/to/Team_Purchase_Order_Register.xlsx --apply    (make the changes)
    python import_po_register.py path/to/Team_Purchase_Order_Register.xlsx --replace-history --apply
        (first removes every purchase order previously imported as history, then imports the
         register again from scratch - live purchase orders are never touched)

How the register's columns land in the app:
    PO Ref                -> PO number
    Date PO Sent          -> Order date
    Expected Delivery     -> Date required
    Order Complete        -> Status   (YES -> Completed, "Cancelled" -> Cancelled, blank -> Issued)
    Works Order No.       -> the PO's Works Order, if that job is in the app
    Supplier              -> Supplier (matched to yours ignoring Ltd/Limited and punctuation;
                             a supplier you don't have yet is created)
    PO Owner              -> Raised by
    Description, quantity, part numbers, supplier quote number, site, contact and the
    register's own works-order wording -> Description (as extra notes on the end)
    Value                 -> PO value ("TBC"/"FOC"/blank -> 0, wording kept in the description)

A PO whose job isn't in the app (older jobs, "Internal" purchases) is simply
added with NO job - no made-up placeholder job. The register's own job reference
(e.g. "TM-1510") is kept as plain text in the PO's "Old job ref" field. A reference
that is higher than the highest job raised in the app (a job that doesn't exist yet)
is not kept, and is listed in the preview for you to check.

A preview writes po_register_preview.csv to your TEAMworks360 data folder and
changes nothing. --apply takes a timestamped backup first and makes every change
in one go (all or nothing).
"""
import csv
import datetime as dt
import re
import unicodedata
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location
import import_team_order_book as imp
import migrations

REPORT_NAME = "po_register_preview.csv"

# Register "End Customer" -> customer name the app already uses (case-insensitive).
END_CUSTOMER_ALIASES = {
    "quill international": "Quill International",
    "itp aero uk hucknall": "ITP Aero - Hucknall",
    "itp": "ITP Aero - Hucknall",
    "draeger": "Dräger", "dräger": "Dräger",
    "smw": "SMW",
    "towne lifting": "Towne", "towne": "Towne",
    "siemens": "Siemens",
    "napier": "Napier Turbochargers",
}
ROLLS_ROYCE = {"rolls royce", "rr", "rr subs"}
INTERNAL = {"total engineering asset management", "team", "internal midlands", "internal north", "internal"}


EXCEL_ERROR = re.compile(r"^#(DIV/0!|N/A|REF!|VALUE!|NAME\?|NUM!|NULL!)", re.I)


def clean(v):
    """Tidy text; a spreadsheet error such as #DIV/0! counts as empty."""
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    if EXCEL_ERROR.match(s):
        return None
    return s or None


def supplier_key(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"\b(ltd|limited|plc|inc|llp|uk)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def parse_value(raw):
    """(number, note). Handles '££62.32', '1,555.20.', 'EUR 9,626.10'; words give 0 + a note."""
    if raw is None:
        return 0.0, None
    if isinstance(raw, (int, float)):
        return float(raw), None
    s = str(raw).strip()
    if EXCEL_ERROR.match(s):
        return 0.0, None
    if not s:
        return 0.0, None
    cur = "EUR" if re.search(r"eur|€", s, re.I) else ("USD" if re.search(r"usd|\$", s, re.I) else None)
    t = re.sub(r"[^\d.,]", "", s).strip(".,")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+\.\d{2}", t):          # 4.166.66 -> 4,166.66
        head, tail = t.rsplit(".", 1)
        t = head.replace(".", "") + "." + tail
    t = t.replace(",", "")
    try:
        v = float(t)
    except ValueError:
        return 0.0, f"value in register: '{s}'"
    if cur:
        return v, f"value is in {cur} as typed in the register ('{s}') - not converted"
    return v, None


def parse_date(raw):
    if isinstance(raw, dt.datetime):
        d = raw.date()
    elif isinstance(raw, dt.date):
        d = raw
    else:
        return None
    return d.isoformat() if 2019 <= d.year <= 2027 else None


def clean_register_ref(raw, highest_job):
    """The register's job reference as plain text, minus any TM number higher than
    the highest job actually raised (those jobs don't exist yet). Returns
    (text or None, the dropped future number or None)."""
    text = (raw or "").strip()
    if not text:
        return None, None
    future = None
    for t in re.findall(r"TM-\d{3,5}", text):
        if int(t[3:]) > highest_job:
            future = t
            text = re.sub(re.escape(t) + r"\s*(?:[&,/]\s*)?", "", text).strip(" &,/")
    return (text or None), future


def read_register(path):
    import openpyxl
    warnings.simplefilter("ignore")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb["Data"]
    out = []
    for i, r in enumerate(ws.iter_rows(min_row=4, max_col=22, values_only=True), start=4):
        if clean(r[0]):
            out.append((i, r))
        if i > 30000:
            break
    return out


class Importer:
    def __init__(self, conn):
        self.conn = conn
        self.changes = []
        self.review = []
        self.counts = Counter()
        self.existing = {r["po_number"] for r in conn.execute("SELECT po_number FROM purchase_orders")}
        self.wo = {r["job_number"]: r["works_order_id"] for r in conn.execute("SELECT job_number, works_order_id FROM works_orders")}
        self.cust = {r["customer_name"].lower(): r["customer_id"] for r in conn.execute("SELECT customer_id, customer_name FROM customers")}
        self.sup = {}
        self.sup_exact = {}
        for r in conn.execute("SELECT supplier_id, supplier_name FROM suppliers"):
            self.sup.setdefault(supplier_key(r["supplier_name"]), r["supplier_id"])
            self.sup_exact[r["supplier_name"].lower()] = r["supplier_id"]
        self.future_refs = []
        hi = [int(m.group(1)) for r in conn.execute("SELECT job_number FROM works_orders") for m in [re.match(r"^TM-(\d+)$", r["job_number"])] if m]
        self.highest_job = max(hi) if hi else 0
        self.new_suppliers = Counter()
        row = conn.execute(
            "SELECT customer_id, COUNT(*) n FROM works_orders w WHERE job_number LIKE 'TM-%' GROUP BY customer_id ORDER BY n DESC LIMIT 1").fetchone()
        self.main_customer_id = row["customer_id"] if row else None   # the customer the app files Rolls Royce jobs under

    # ---- suppliers ----
    def supplier_id(self, raw, po):
        name = imp.normalise_supplier(clean(raw)) if clean(raw) else None
        merge = {k.lower(): v for k, v in migrations.SUPPLIER_MERGE_MAP.items() if v}
        if name and name.lower() in merge:
            name = merge[name.lower()]
        if not name:
            name = "Unknown Supplier"
        sid = self.sup_exact.get(name.lower()) or self.sup.get(supplier_key(name))
        if sid:
            return sid
        cur = self.conn.execute("INSERT INTO suppliers (supplier_name) VALUES (?)", (name,))
        sid = cur.lastrowid
        self.sup_exact[name.lower()] = sid
        self.sup[supplier_key(name)] = sid
        self.counts["suppliers_added"] += 1
        self.new_suppliers[name] += 1
        return sid

    # ---- placeholder jobs ----
    def customer_for_end_customer(self, raw):
        c = (clean(raw) or "").lower()
        if c in ROLLS_ROYCE:
            return self.main_customer_id
        if c in INTERNAL:
            return self.cust.get("team - north") or self.unknown_customer()
        target = END_CUSTOMER_ALIASES.get(c)
        if target and target.lower() in self.cust:
            return self.cust[target.lower()]
        return self.unknown_customer()

    def unknown_customer(self):
        cid = self.cust.get("unknown customer")
        if cid is None:
            cid = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Unknown Customer')").lastrowid
            self.cust["unknown customer"] = cid
        return cid

    def holder_job(self, customer_id):
        if customer_id in self.holders:
            return self.holders[customer_id]
        name = self.conn.execute("SELECT customer_name FROM customers WHERE customer_id=?", (customer_id,)).fetchone()["customer_name"]
        job_number = "HIST-PO-" + re.sub(r"[^A-Z0-9]+", "-", unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()).strip("-")[:30]
        row = self.conn.execute("SELECT works_order_id FROM works_orders WHERE job_number=?", (job_number,)).fetchone()
        if row:
            wid = row["works_order_id"]
        else:
            wid = self.conn.execute(
                """INSERT INTO works_orders (job_number, job_name, customer_id, status, contract_value, budget_cost)
                   VALUES (?,?,?,'Closed',0,0)""",
                (job_number, f"Historic purchase orders - {name} (jobs not in the app)", customer_id)).lastrowid
            self.counts["holder_jobs"] += 1
        self.holders[customer_id] = wid
        return wid

    # ---- one register row ----
    def add(self, row_no, r):
        po = clean(r[0])
        if po in self.existing:
            self.counts["already_in_app"] += 1
            return
        # Pre-numbered but unused rows (no supplier, no description, no value, no date)
        # are not purchase orders - a number is only issued when a PO is actually raised.
        if not (clean(r[7]) or clean(r[12]) or parse_value(r[19])[0] or parse_date(r[1])):
            self.counts["blank_rows_skipped"] += 1
            return
        self.existing.add(po)

        notes = []
        value, vnote = parse_value(r[19])
        if vnote:
            notes.append(vnote)
        wo_raw = clean(r[5])
        register_ref = None
        wos = re.findall(r"TM-\d{3,5}", wo_raw or "")
        linked = next((w for w in wos if w in self.wo), None)
        if linked:
            wo_id = self.wo[linked]
            self.counts["linked_to_job"] += 1
            if len(wos) > 1:
                notes.append(f"Register WO: {wo_raw}")
        else:
            wo_id = None
            self.counts["no_job"] += 1
            register_ref, future = clean_register_ref(wo_raw, self.highest_job)
            if future:
                self.counts["future_refs"] += 1
                self.future_refs.append((po, future))
        done = (clean(r[4]) or "").lower()
        status = "Completed" if done in ("yes", "y") else "Cancelled" if done == "cancelled" else "Issued"
        order_date = parse_date(r[1])
        date_required = parse_date(r[3])
        qty = r[13]
        if qty not in (None, "", 0) and not isinstance(qty, str):
            notes.append(f"Qty: {qty:g}")
        for label, v in (("Part numbers", r[16]), ("Supplier quote", r[20]), ("Site", r[6]), ("Contact", r[10]), ("End customer", r[9])):
            if clean(v) and clean(v).upper() not in ("N/A", "NA"):
                notes.append(f"{label}: {clean(v)}")
        desc = clean(r[12]) or ""
        desc = (desc + (" | " if desc and notes else "") + " | ".join(notes)).strip() or None

        sid = self.supplier_id(r[7], po)
        self.conn.execute(
            """INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, description, order_date,
                   po_value, status, date_required, raised_by, is_historic, register_job_ref)
               VALUES (?,?,?,?,?,?,?,?,?,1,?)""",
            (po, wo_id, sid, desc, order_date, value, status, date_required, clean(r[11]),
             register_ref if not linked else None))
        self.counts["added"] += 1
        self.counts["status_" + status] += 1
        self.changes.append((po, clean(r[7]) or "", linked or "(no job)", status, value))


def remove_historic_pos(conn):
    """For --replace-history: deletes every purchase order previously imported as
    history (is_historic=1), plus any empty HIST-PO stand-in jobs, so the register can
    be brought in again cleanly. Live purchase orders are never touched. Refuses if
    anything (goods in, supplier invoice) points at a historic PO."""
    ids = [r[0] for r in conn.execute("SELECT purchase_order_id FROM purchase_orders WHERE is_historic=1")]
    for table in ("goods_in", "supplier_invoices"):
        if ids and conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE purchase_order_id IN (SELECT purchase_order_id FROM purchase_orders WHERE is_historic=1)"
        ).fetchone()[0]:
            raise SystemExit(f"STOPPED - nothing changed: some historic purchase orders have {table.replace('_', ' ')} recorded against them.")
    conn.execute("DELETE FROM purchase_order_lines WHERE purchase_order_id IN (SELECT purchase_order_id FROM purchase_orders WHERE is_historic=1)")
    conn.execute("DELETE FROM purchase_orders WHERE is_historic=1")
    refs = []
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})"):
            if fk[2] == "works_orders":
                refs.append((table, fk[3]))
    for wid, in conn.execute("SELECT works_order_id FROM works_orders WHERE job_number LIKE 'HIST-PO-%'").fetchall():
        if not sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {c}=?", (wid,)).fetchone()[0] for t, c in refs):
            conn.execute("DELETE FROM works_orders WHERE works_order_id=?", (wid,))
    return len(ids)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv
    if not args:
        sys.exit("Usage: python import_po_register.py path/to/Team_Purchase_Order_Register.xlsx [--apply]")
    xlsx = Path(args[0])
    if not xlsx.exists():
        sys.exit(f"Spreadsheet not found: {xlsx}")
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to import into.")

    print(f"Reading {xlsx} ...")
    rows = read_register(xlsx)
    print(f"Found {len(rows)} purchase orders in the register.")

    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if "is_historic" not in {r[1] for r in conn.execute("PRAGMA table_info(purchase_orders)")}:
        # same one-line change the app makes to its own database when it starts
        conn.execute("ALTER TABLE purchase_orders ADD COLUMN is_historic INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    migrations.relax_po_job_link(conn)   # historic POs may have no job
    replace = "--replace-history" in sys.argv
    removed_history = 0
    try:
        conn.execute("BEGIN")
        if replace:
            removed_history = remove_historic_pos(conn)
        importer = Importer(conn)
        for row_no, r in rows:
            importer.add(row_no, r)
        db_location.ensure_data_dir()
        report = Path(db_location.DATA_DIR) / REPORT_NAME
        with open(report, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["PO", "Supplier (as in register)", "Works order", "Status", "Value"])
            wr.writerows(importer.changes)
            wr.writerow([])
            wr.writerow(["New suppliers created (check for spelling variants of existing ones)"])
            for n in sorted(importer.new_suppliers):
                wr.writerow([n])
        c = importer.counts
        print(f"""
{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}
{('  Old historic purchase orders removed first: ' + str(removed_history)) if replace else ''}
  Purchase orders added (all flagged Historic): {c['added']}
    Completed / Issued / Cancelled: {c['status_Completed']} / {c['status_Issued']} / {c['status_Cancelled']}
    On a job that's in the app:     {c['linked_to_job']}
    With no job (older jobs not in the app): {c['no_job']}
    Job reference higher than any job raised (not kept): {c['future_refs']}
  Already in the app (skipped): {c['already_in_app']}
  Blank pre-numbered rows (skipped): {c['blank_rows_skipped']}
  New suppliers created:        {c['suppliers_added']}

Full list: {report}
""")
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
