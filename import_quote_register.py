"""
Import the TEAM Quote Register spreadsheet into the app's Quotes.

Safe to run on a live database: it only ADDS quotes. A quote number that is
already in the app (including the one you raised there yourself) is never
changed, customers and works orders are not modified, and running it twice does
not create duplicates. The only other thing it can add is a customer that the
register mentions and the app doesn't have yet.

Usage:
    python import_quote_register.py path/to/Team_Quote_Register.xlsx            (preview only)
    python import_quote_register.py path/to/Team_Quote_Register.xlsx --apply    (make the changes)

How the register's columns land in the app:
    TMQ                     -> Quote number          (a row holding two quotes becomes two)
    Team Quote Owner        -> Owner                 (the initials, as typed)
    Date Quoted            -> Quote date            (else Date Request Received)
    Customer + Area        -> Customer              (see below)
    Customer Contact       -> Contact name
    Towi Ref               -> Your ref
    Description            -> Description
    Value                  -> Estimated value       ("TBC" and blanks become 0)
    Works Order Number     -> linked Works Order    (when it matches a job in the app;
                                                     otherwise kept as text in the quote)
    Quote Status           -> Status                Won -> Won, Lost -> Lost,
                                                    Submitted -> Sent,
                                                    Cancelled -> Cancelled, On Hold -> On Hold,
                                                    blank -> Won if it has a works order,
                                                    else Sent if it was quoted, else Draft
    Customer PO, Fixture / Part number, Supplier used, Type, Comments, and any
    original wording that had to be tidied  -> Notes

Customer: a quote linked to a works order takes that job's customer. Others use
the customer the app already uses for that register customer (learnt from the
quotes that DO have a works order - the app files Rolls Royce jobs under one
customer, so the register's site/area goes into the quote's Notes); a register customer that's new to
the app is created; anything that can't be placed goes under "Unknown Customer"
and is listed in the report for you to fix.

A preview writes quote_register_preview.csv to your TEAMworks360 data folder and
changes nothing. --apply takes a timestamped backup first and makes every change
in one go (all or nothing).
"""
import csv
import datetime as dt
import re
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location
import migrations

REPORT_NAME = "quote_register_preview.csv"

# Register customer code -> the customer name the app already uses.
CUSTOMER_ALIASES = {
    "dräger": "Dräger", "drager": "Dräger",
    "quill": "Quill International",
    "towne": "Towne", "towne lifting": "Towne",
    "itp": "ITP Aero - Hucknall",
    "telbrook": "SMW Autoblok Telbrook",
    "napier": "Napier Turbochargers",
    "ams": "AMS Hayley",
    "siemens": "Siemens",
    "smw": "SMW",
    "centromotion": "CentroMotion",
}
ROLLS_ROYCE_CODES = {"rr", "rolls royce", "rr subs"}

STATUS_MAP = {"won": "Won", "lost": "Lost", "submitted": "Sent", "cancelled": "Cancelled", "on hold": "On Hold"}


def clean(v):
    if v is None:
        return None
    s = re.sub(r"[ \t]+", " ", str(v).replace("\r", "")).strip()
    return s or None


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def parse_value(raw):
    """Returns (number, note). Non-numbers like 'TBC' give 0 plus a note."""
    if raw is None:
        return 0.0, None
    if isinstance(raw, (int, float)):
        return float(raw), None
    s = str(raw).strip()
    if not s:
        return 0.0, None
    t = s.replace("£", "").replace(" ", "")
    # '£4.166.66' / '£499.386.23': dots used as thousands separators
    if re.fullmatch(r"\d{1,3}(\.\d{3})+\.\d{2}", t):
        head, tail = t.rsplit(".", 1)
        return float(head.replace(".", "") + "." + tail), f"value tidied from '{s}'"
    t = t.replace(",", "")
    try:
        return float(t), None
    except ValueError:
        return 0.0, f"value in register: '{s}'"


def parse_date(raw):
    """A real date, or None if it can't be trusted. Returns (iso, note)."""
    if raw is None:
        return None, None
    if isinstance(raw, dt.datetime):
        d = raw.date()
    elif isinstance(raw, dt.date):
        d = raw
    else:
        s = str(raw).strip()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})", s)
        if not m:
            return None, f"date in register: '{s}'"
        day, mon, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100:
            yr += 2000
        try:
            d = dt.date(yr, mon, day)
        except ValueError:
            return None, f"date in register: '{s}'"
    if not (2020 <= d.year <= 2027):
        return None, f"date in register: '{d.isoformat()}'"
    return d.isoformat(), None


def read_register(path):
    import openpyxl
    warnings.simplefilter("ignore")
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb["Data"]
    out = []
    for i, r in enumerate(ws.iter_rows(min_row=3, max_col=17, values_only=True), start=3):
        if any(v not in (None, "") for v in r):
            out.append((i, r))
        if i > 20000:
            break
    return out


def split_quotes(row_no, r):
    """One register row can hold two quotes (TMQ-1289.1 / TMQ-1289.2, each with its
    own value). Returns a list of (quote_number, raw_value, note)."""
    raw = clean(r[0])
    if not raw:
        return []
    tokens = re.findall(r"TMQ-\d+(?:\.\d+)?[A-Za-z]?", raw)
    if not tokens:
        return [(raw, r[13], f"quote number as typed in register: '{raw}'")]
    vals = [v.strip() for v in str(r[13]).split("\n") if v.strip()] if isinstance(r[13], str) and "\n" in r[13] else None
    if len(tokens) > 1 and vals and len(vals) == len(tokens):
        return [(t, v, None) for t, v in zip(tokens, vals)]
    return [(tokens[0], r[13], None if len(tokens) == 1 else f"register row also lists {', '.join(tokens[1:])}")]


def wo_numbers(raw):
    return re.findall(r"TM-\d{3,5}", str(raw or ""))


class Importer:
    def __init__(self, conn):
        self.conn = conn
        self.changes = []      # (quote, customer, status, value, note)
        self.review = []       # (quote, why)
        self.counts = Counter()
        self.new_customers = {}
        self.cust_by_name = {r["customer_name"].lower(): r["customer_id"] for r in conn.execute("SELECT customer_id, customer_name FROM customers")}
        self.cust_name = {v: k for k, v in self.cust_by_name.items()}
        self.wo = {r["job_number"]: r for r in conn.execute("SELECT works_order_id, job_number, customer_id FROM works_orders")}
        self.existing = {r["quote_number"] for r in conn.execute("SELECT quote_number FROM quotes")}
        self.site_votes = defaultdict(Counter)   # (customer code, area) -> app customer ids
        self.code_votes = defaultdict(Counter)   # customer code -> app customer ids
        self.unknown_id = self.cust_by_name.get("unknown customer")

    def learn(self, rows):
        for _, r in rows:
            nums = wo_numbers(r[14])
            if len(nums) != 1 or nums[0] not in self.wo:
                continue
            key = (norm(clean(r[4])), norm(clean(r[5])))
            self.site_votes[key][self.wo[nums[0]]["customer_id"]] += 1
            self.code_votes[key[0]][self.wo[nums[0]]["customer_id"]] += 1

    def customer_for(self, qn, code, area, wo_row):
        if wo_row is not None:
            return wo_row["customer_id"]
        key = (norm(code), norm(area))
        votes = self.site_votes.get(key)
        if votes:
            return votes.most_common(1)[0][0]
        c = (code or "").lower()
        if c in ROLLS_ROYCE_CODES:
            # The app files Rolls Royce works orders under the customer it already
            # uses for them (the site goes in the quote's Notes), so quotes follow.
            votes = self.code_votes.get("rr")
            if votes:
                return votes.most_common(1)[0][0]
            self.review.append((qn, "Rolls Royce quote and no Rolls Royce works order in the app to copy the customer from - put under Unknown Customer"))
            return self.unknown_customer()
        if not c:
            self.review.append((qn, "no customer in the register - put under Unknown Customer"))
            return self.unknown_customer()
        target = CUSTOMER_ALIASES.get(c, clean(code))
        cid = self.cust_by_name.get(target.lower())
        if cid:
            return cid
        if c == "sc":
            self.review.append((qn, "customer 'SC' in the register isn't a recognisable customer - put under Unknown Customer"))
            return self.unknown_customer()
        cid = self.new_customers.get(target.lower())
        if cid is None:
            cur = self.conn.execute("INSERT INTO customers (customer_name) VALUES (?)", (target,))
            cid = cur.lastrowid
            self.new_customers[target.lower()] = cid
            self.cust_by_name[target.lower()] = cid
            self.counts["customers_added"] += 1
            self.review.append((qn, f"new customer '{target}' created (first quote seen for them)"))
        return cid

    def unknown_customer(self):
        if self.unknown_id is None:
            cur = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Unknown Customer')")
            self.unknown_id = cur.lastrowid
            self.cust_by_name["unknown customer"] = self.unknown_id
        return self.unknown_id

    def add(self, row_no, r):
        parts = split_quotes(row_no, r)
        if not parts:
            return
        desc = clean(r[10])
        if not desc and not clean(r[4]) and r[13] in (None, ""):
            self.counts["blank_placeholders"] += 1     # e.g. reserved numbers with nothing against them
            return
        for qn, raw_value, qnote in parts:
            if qn in self.existing:
                self.counts["already_in_app"] += 1
                continue
            self.existing.add(qn)
            notes = []
            if qnote:
                notes.append(qnote)
            value, vnote = parse_value(raw_value)
            if vnote:
                notes.append(vnote)
            recv, rnote = parse_date(r[2])
            quoted, dnote = parse_date(r[3])
            for n in (rnote, dnote):
                if n:
                    notes.append(n)
            quote_date = quoted or recv

            wos = wo_numbers(r[14])
            wo_row = self.wo.get(wos[0]) if len(wos) == 1 else None
            wo_text = clean(r[14]) if clean(r[14]) and clean(r[14]).upper() not in ("TM-", "TM") else None
            wo_id = wo_row["works_order_id"] if wo_row else None
            if wo_text and not wo_row:
                notes.append(f"works order in register: {wo_text}")
                self.counts["wo_text_only"] += 1

            reg_status = (clean(r[16]) or "").lower()
            if reg_status in STATUS_MAP:
                status = STATUS_MAP[reg_status]
                if reg_status in ("cancelled", "on hold"):
                    notes.append(f"status in register: {clean(r[16])}")
            elif wos:
                status = "Won"
            elif quote_date and quoted:
                status = "Sent"
                notes.append("no outcome recorded in the register")
            else:
                status = "Draft"
            if reg_status == "won" and not quote_date:
                self.review.append((qn, "won but no quote date in the register"))

            owner = clean(r[1])
            code, area = clean(r[4]), clean(r[5])
            cid = self.customer_for(qn, code, area, wo_row)
            for label, v in (("Customer PO", r[7]), ("Fixture / part", r[9]), ("Supplier", r[11]), ("Type", r[12]), ("Comments", r[15])):
                if clean(v):
                    notes.append(f"{label}: {clean(v)}")
            if area:
                notes.append(f"Area: {area}")
            if quote_date and not quoted:
                notes.append("quote date = date request received (no quoted date in register)")

            self.conn.execute(
                """INSERT INTO quotes (quote_number, customer_id, description, owner, status,
                       estimated_value, quote_date, notes, contact_name, your_ref, works_order_ref,
                       works_order_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (qn, cid, desc, owner, status, value, quote_date, "\n".join(notes) or None,
                 clean(r[6]), clean(r[8]), wo_text, wo_id),
            )
            self.counts["added"] += 1
            self.counts["status_" + status] += 1
            if wo_id:
                self.counts["linked_to_wo"] += 1
            self.changes.append((qn, self.cust_name.get(cid) or next((k for k, v in self.cust_by_name.items() if v == cid), cid),
                                 status, value, "; ".join(notes)))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv
    if not args:
        sys.exit("Usage: python import_quote_register.py path/to/Team_Quote_Register.xlsx [--apply]")
    xlsx = Path(args[0])
    if not xlsx.exists():
        sys.exit(f"Spreadsheet not found: {xlsx}")
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to import into.")

    print(f"Reading {xlsx} ...")
    rows = read_register(xlsx)
    print(f"Found {len(rows)} rows in the register.")

    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrations.widen_quote_statuses(conn)   # older databases don't have the On Hold / Cancelled statuses yet
    imp = Importer(conn)
    try:
        conn.execute("BEGIN")
        imp.learn(rows)
        for row_no, r in rows:
            imp.add(row_no, r)

        db_location.ensure_data_dir()
        report = Path(db_location.DATA_DIR) / REPORT_NAME
        with open(report, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["Quote", "Customer", "Status", "Value", "Notes"])
            wr.writerows(imp.changes)
            wr.writerow([])
            wr.writerow(["FOR YOUR REVIEW", "Why"])
            wr.writerows(imp.review)

        c = imp.counts
        print(f"""
{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}
  Quotes added:                 {c['added']}
    Won / Lost / Sent / Draft:  {c['status_Won']} / {c['status_Lost']} / {c['status_Sent']} / {c['status_Draft']}
    Linked to a works order:    {c['linked_to_wo']}
    Works order not in the app (kept as text in the quote): {c['wo_text_only']}
  Already in the app (skipped): {c['already_in_app']}
  Empty reserved numbers (skipped): {c['blank_placeholders']}
  New customers created:        {c['customers_added']}
  Items flagged for your review: {len(imp.review)}

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
