"""
Import real order-book data from the TEAM "Order Book" spreadsheet into the
TEAMworks360 (formerly Job Profitability Manager) database, replacing all seed/demo data.

Usage:
    python import_team_order_book.py [path_to_xlsm]

Defaults to ./data/Team_Order_Book.xlsm if no path is given.

What this does
---------------
1. Wipes existing Customers, Suppliers, Works Orders, Purchase Orders,
   Supplier Invoices, Labour Entries and Customer Invoices (Users/logins
   are left untouched).
2. Reads the "Data" sheet of the workbook (row 3 = column headers, data
   starts row 4) and maps each order-book row onto one Works Order.
3. Derives Customers and Suppliers from the distinct values found in the
   "End Customer" / "Supplier 1" / "Supplier 2" columns.
4. Builds the cost side of each job from the spreadsheet's own figures so
   that Actual Cost reconciles with the spreadsheet's "Cost" column:
     - If a named supplier + a "Value of Brought Out" amount is given,
       that becomes a Purchase Order + Supplier Invoice for that supplier.
     - Else if a named supplier is given with no separate bought-out
       figure, the ENTIRE job cost is booked as a Purchase Order +
       Supplier Invoice for that supplier (assumed fully subcontracted).
     - Otherwise the job cost is booked as a single Labour Entry
       (in-house work), at an assumed £35/hr rate (documented below),
       against the job's "Owner" as the employee.
5. Creates one Customer Invoice per job for the "Invoice Value" so that
   Revenue reconciles with the spreadsheet's own Profit calculation
   (Invoice Value - Cost), regardless of whether it has actually been
   invoiced yet — this mirrors how the spreadsheet computes Profit for
   every row, not just invoiced ones.

Documented assumptions (spreadsheet does not give us better data):
  - Labour rate: £35.00/hr for any job cost booked as in-house labour.
  - Works Order Budget Cost is a flat 35% of Invoice Value for every job
    (the business's own explicit target) - the source spreadsheet's "TCP"
    column is not used, since it turned out to itself just be an automatic
    ~70%-of-invoice-value formula in most rows, not a figure anyone
    deliberately set.
  - "Transport Cost" is not booked separately — in the source data,
    "Cost" already appears to equal "Value of Brought Out" on fully
    subcontracted rows, so transport looks like an informational
    side-metric rather than a component of Cost.
  - Status codes: D/d -> Completed, A -> In Progress, C -> Cancelled,
    H -> Open, blank/unrecognised -> Draft.
"""
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, date
from pathlib import Path

import openpyxl
from dateutil import parser as dateparser

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import db_location

db_location.migrate_legacy_files(str(BASE_DIR))
DB_PATH = db_location.DB_PATH
DEFAULT_XLSM = BASE_DIR / "data" / "Team_Order_Book.xlsm"

LABOUR_RATE = 35.00  # documented assumption, see module docstring
DATA_FIRST_ROW = 4

COL = {
    'tm_no': 1, 'tm_id': 2, 'enquiry_date': 3, 'team_po': 6,
    'end_customer': 10, 'site': 11, 'site_area': 12, 'location': 13,
    'owner': 14, 'customer_po': 15, 'contract_code': 16, 'details': 17,
    'invoice_value': 18, 'cost': 19, 'delivery_req': 22, 'delivered_on': 23,
    'status': 25, 'tcp': 26, 'supplier1': 29, 'supplier2': 31,
    'bought_out_value': 33, 'date_invoiced': 39, 'team_quote_ref': 40,
    'coupa_ref': 46,
}

# Matches a TEAM order-book reference like "TEAM2485" anywhere in a messier
# cell value (e.g. "£16200 TEAM1927", "TEAM2085 TEAM2326", "TEAM2113/4/5").
# The negative lookbehind stops it matching a suffix of another word, e.g.
# "CNTEAM0006" must NOT be read as a TEAM ref.
TEAM_REF_PATTERN = re.compile(r'(?<![A-Za-z0-9])TEAM[\s\-]?(\d+)', re.IGNORECASE)


def extract_team_ref(*values):
    """Return the first 'TEAM<number>' reference found across the given
    cell values (checked in the order passed), or None if none contain one.
    Coupa TEAM Ref is the reliable source for this format; TEAM Quote Ref
    usually holds the customer's own PO number instead, but occasionally
    has a TEAM-format reference too, so it's checked as a fallback."""
    for v in values:
        if not v:
            continue
        m = TEAM_REF_PATTERN.search(str(v))
        if m:
            return f"TEAM{m.group(1)}"
    return None

STATUS_MAP = {'d': 'Completed', 'a': 'In Progress', 'c': 'Cancelled', 'h': 'Open'}

SUPPLIER_NORMALISE = {
    'fife': 'Fife Engineering',
    'cromwell tools': 'Cromwell',
    'roemhled': 'Roemheld',
}
SUPPLIER_IGNORE = {'n/a', 'na', ''}


def clean_text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def to_number(v):
    """Coerce a cell to float, tolerating currency symbols/commas and
    stray text (e.g. 'Cancelled', '#VALUE!', ' ') -> 0.0."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    s = s.replace('£', '').replace('$', '').replace(',', '')
    try:
        return float(s)
    except ValueError:
        return 0.0


def to_date(v):
    """Return an ISO date string or None. Tolerates datetime objects,
    UK-format strings, and garbage (#VALUE!, prose, blanks) -> None."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if not s or s.upper() in ('N/A', '#VALUE!', '#REF!', 'C', 'D'):
        return None
    try:
        dt = dateparser.parse(s, dayfirst=True, fuzzy=False)
        # Guard against wildly out-of-range parses from fuzzy prose
        if dt.year < 2015 or dt.year > 2035:
            return None
        return dt.date().isoformat()
    except (ValueError, OverflowError):
        return None


def normalise_supplier(name):
    if not name:
        return None
    key = name.strip().lower()
    if key in SUPPLIER_IGNORE:
        return None
    if key in SUPPLIER_NORMALISE:
        return SUPPLIER_NORMALISE[key]
    return name.strip()


def map_status(raw):
    if not raw:
        return 'Draft'
    key = str(raw).strip().lower()
    return STATUS_MAP.get(key, 'Draft')


def read_rows(xlsm_path):
    wb = openpyxl.load_workbook(xlsm_path, data_only=True, keep_vba=True)
    ws = wb['Data']
    rows = []
    # Reads to the sheet's real last row rather than a fixed number: the order
    # book grows through the year, and a hard-coded limit silently dropped
    # every job past it (the 2026 book now runs well past the old 1767).
    for r in range(DATA_FIRST_ROW, ws.max_row + 1):
        row = {k: ws.cell(row=r, column=c).value for k, c in COL.items()}
        if clean_text(row['details']) or clean_text(row['end_customer']):
            rows.append(row)
    return rows


CONTRACT_CODE_CANONICAL = {
    'NEW': 'NEW',
    'LNEW': 'LNew',
}


def normalise_contract_code(code):
    """Fold obvious case-only variants together (the source has both "New"
    and "NEW", both "Lnew" and "LNew" for the same code) so a report grouped
    by contract code doesn't split one real code into two rows."""
    if not code:
        return None
    key = code.strip().upper()
    return CONTRACT_CODE_CANONICAL.get(key, code.strip())


def build_job_name(row):
    loc = clean_text(row['location'])
    prefix = f"[{loc}] " if loc else ""
    details = clean_text(row['details']) or f"Job {row['tm_no']}"
    customer_po = clean_text(row['customer_po'])
    suffix = f" (Customer PO: {customer_po})" if customer_po else ""
    return f"{prefix}{details}{suffix}"


def main():
    xlsm_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_XLSM
    if not xlsm_path.exists():
        sys.exit(f"Spreadsheet not found: {xlsm_path}")

    print(f"Reading {xlsm_path} ...")
    rows = read_rows(xlsm_path)
    print(f"Found {len(rows)} real order rows.")

    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = OFF")

    # ---------------- Self-heal schema drift on existing databases ----------------
    # If this is being re-run against a jpm.db created before a column existed
    # (e.g. contract_code), add it rather than requiring a full schema rebuild
    # that would also wipe the users table.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(works_orders)")}
    if not existing_cols:
        conn.close()
        sys.exit("No works_orders table found - run 'python seed.py' first to create the schema, then re-run this import.")
    if "contract_code" not in existing_cols:
        cur.execute("ALTER TABLE works_orders ADD COLUMN contract_code TEXT")
    if "budget_is_estimated" not in existing_cols:
        cur.execute("ALTER TABLE works_orders ADD COLUMN budget_is_estimated INTEGER NOT NULL DEFAULT 0")
    if "location" not in existing_cols:
        cur.execute("ALTER TABLE works_orders ADD COLUMN location TEXT")

    # Quotes are NOT wiped below (they're a customer sales record, not
    # something this import owns) - but works_orders IS about to be wiped
    # and rebuilt with fresh auto-incremented IDs, which means any quote's
    # works_order_id is about to become meaningless: it'll still point to
    # *some* row after the rebuild, just not necessarily the same job as
    # before, since IDs get reused from scratch. Capture what job each
    # linked quote actually points to right now, by its stable job_number,
    # BEFORE the wipe below removes that link - so it can be correctly
    # re-linked once the new works_orders rows (with their own new IDs)
    # exist, rather than leaving a quote silently attached to a completely
    # different job that happened to land on the same numeric ID.
    quote_job_numbers = {
        quote_id: job_number
        for quote_id, job_number in cur.execute(
            """SELECT q.quote_id, wo.job_number FROM quotes q
               JOIN works_orders wo ON wo.works_order_id = q.works_order_id
               WHERE q.works_order_id IS NOT NULL"""
        )
    }

    # ---------------- Wipe existing operational data (keep users) ----------------
    # purchase_order_lines is wiped alongside purchase_orders because it's
    # meaningless without its parent PO - normally an ON DELETE CASCADE
    # would handle this automatically, but that only fires with foreign
    # keys turned ON, which this wipe deliberately doesn't use (see below).
    for table in ['customer_invoices', 'labour_entries', 'supplier_invoices',
                  'purchase_order_lines', 'purchase_orders', 'works_orders', 'suppliers', 'customers']:
        cur.execute(f"DELETE FROM {table}")
        cur.execute(f"DELETE FROM sqlite_sequence WHERE name=?", (table,))

    # ---------------- Customers ----------------
    customer_names = sorted({clean_text(r['end_customer']) for r in rows if clean_text(r['end_customer'])})
    if not customer_names:
        customer_names = ['Unknown Customer']
    customer_ids = {}
    for name in customer_names:
        cur.execute("INSERT INTO customers (customer_name) VALUES (?)", (name,))
        customer_ids[name] = cur.lastrowid
    fallback_customer_id = customer_ids.get(customer_names[0])

    # ---------------- Suppliers ----------------
    supplier_names = set()
    for r in rows:
        for key in ('supplier1', 'supplier2'):
            n = normalise_supplier(clean_text(r[key]))
            if n:
                supplier_names.add(n)
    supplier_ids = {}
    for name in sorted(supplier_names):
        cur.execute("INSERT INTO suppliers (supplier_name) VALUES (?)", (name,))
        supplier_ids[name] = cur.lastrowid

    # ---------------- Works Orders + related records ----------------
    wo_count = po_count = si_count = le_count = ci_count = 0
    ci_with_team_ref = 0
    total_revenue = total_cost = 0.0

    for r in rows:
        job_number = clean_text(r['tm_no']) or f"TM-{r['tm_id']}"
        job_name = build_job_name(r)
        customer_name = clean_text(r['end_customer'])
        customer_id = customer_ids.get(customer_name, fallback_customer_id)
        project_manager = clean_text(r['owner']) or ''
        status = map_status(r['status'])

        invoice_value = to_number(r['invoice_value'])
        cost = to_number(r['cost'])
        # Budget/target cost is a flat 35% of invoice value for every job.
        # (Previously used the source spreadsheet's own "TCP" figure where
        # present, falling back to 70% of invoice value otherwise - but TCP
        # turned out to itself just be an automatic formula in most rows,
        # not a figure anyone deliberately set, so there was never a real
        # "confirmed" budget behind it. This flat 35% rule replaces that
        # entirely, at the business's explicit request.)
        budget_cost = round(invoice_value * 0.35, 2)
        # No imported job has ever had a genuinely human-set budget - only
        # editing a Works Order through the app confirms one (see
        # update_works_order in app.py).
        budget_is_estimated = True

        start_date = to_date(r['enquiry_date'])
        end_date = to_date(r['delivered_on']) or to_date(r['delivery_req'])

        cur.execute(
            """INSERT INTO works_orders
               (job_number, job_name, customer_id, project_manager, status,
                start_date, end_date, contract_value, budget_cost, budget_is_estimated, contract_code, location)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_number, job_name, customer_id, project_manager, status,
             start_date, end_date, round(invoice_value, 2), round(budget_cost, 2), int(budget_is_estimated),
             normalise_contract_code(clean_text(r['contract_code'])), clean_text(r['location'])),
        )
        wo_id = cur.lastrowid
        wo_count += 1
        total_revenue += invoice_value

        # ---- Cost side: split into supplier (bought-out) vs labour (in-house) ----
        supplier1 = normalise_supplier(clean_text(r['supplier1']))
        bought_out = to_number(r['bought_out_value'])
        if supplier1 and bought_out <= 0 and cost > 0:
            bought_out = cost  # fully subcontracted, no separate bought-out figure given
        bought_out = min(bought_out, cost) if cost > 0 else 0.0
        labour_remainder = max(0.0, cost - bought_out)

        invoiced_status = 'Paid' if to_date(r['date_invoiced']) else (
            'Approved' if status == 'Completed' else 'Pending')

        if supplier1 and bought_out > 0:
            supplier_id = supplier_ids[supplier1]
            po_number = clean_text(r['team_po']) or f"PO-{job_number}"
            # PO numbers must be unique - disambiguate if this PO number was already used
            cur.execute("SELECT 1 FROM purchase_orders WHERE po_number=?", (po_number,))
            if cur.fetchone():
                po_number = f"{po_number}-{job_number}"
            cur.execute(
                """INSERT INTO purchase_orders
                   (po_number, works_order_id, supplier_id, description, order_date, po_value, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (po_number, wo_id, supplier_id, job_name[:200], start_date, round(bought_out, 2),
                 'Completed' if status == 'Completed' else 'Issued'),
            )
            po_id = cur.lastrowid
            po_count += 1
            cur.execute(
                """INSERT INTO supplier_invoices
                   (invoice_number, purchase_order_id, works_order_id, supplier_id, invoice_date, invoice_amount, status)
                   VALUES (?,?,?,?,?,?,?)""",
                (f"SINV-{job_number}", po_id, wo_id, supplier_id,
                 end_date or start_date, round(bought_out, 2), invoiced_status),
            )
            si_count += 1

        if labour_remainder > 0:
            hours = round(labour_remainder / LABOUR_RATE, 2)
            cur.execute(
                """INSERT INTO labour_entries
                   (works_order_id, employee_name, work_date, hours_worked, hourly_rate, labour_cost)
                   VALUES (?,?,?,?,?,?)""",
                (wo_id, project_manager or 'Unassigned', end_date or start_date,
                 hours, LABOUR_RATE, round(labour_remainder, 2)),
            )
            le_count += 1

        total_cost += cost

        # ---- Revenue side: one customer invoice per job ----
        inv_date = to_date(r['date_invoiced']) or end_date or start_date
        if status == 'Cancelled':
            ci_status = 'Cancelled'
        elif to_date(r['date_invoiced']):
            ci_status = 'Paid'
        elif end_date:
            ci_status = 'Sent'
        else:
            ci_status = 'Draft'
        # Prefer the TEAM order-book reference (Coupa TEAM Ref, falling back
        # to TEAM Quote Ref) so the invoice number matches what's used in
        # the source spreadsheet; falls back to a job-based number only for
        # the minority of rows with no TEAM ref recorded at all. Multiple
        # jobs can legitimately share one TEAM ref (one quote covering
        # several line items) so this is not forced unique.
        invoice_number = extract_team_ref(r['coupa_ref'], r['team_quote_ref']) or f"INV-{job_number}"
        if not invoice_number.startswith("INV-"):
            ci_with_team_ref += 1
        cur.execute(
            """INSERT INTO customer_invoices
               (works_order_id, invoice_number, invoice_date, invoice_amount, status)
               VALUES (?,?,?,?,?)""",
            (wo_id, invoice_number, inv_date, round(invoice_value, 2), ci_status),
        )
        ci_count += 1

    # Re-link quotes to their job by job_number now that works_orders has
    # been rebuilt with new IDs (see the capture above, before the wipe).
    # A quote whose job_number no longer appears in this import - the job
    # was removed from the source spreadsheet - gets its link cleared
    # rather than left pointing at an unrelated job that happens to share
    # the old numeric ID; Works Order Ref (separate free-text field, not
    # touched here) still shows on that quote's PDF regardless.
    if quote_job_numbers:
        job_number_to_new_id = dict(cur.execute("SELECT job_number, works_order_id FROM works_orders"))
        relinked, cleared = 0, 0
        for quote_id, job_number in quote_job_numbers.items():
            new_wo_id = job_number_to_new_id.get(job_number)
            cur.execute("UPDATE quotes SET works_order_id=? WHERE quote_id=?", (new_wo_id, quote_id))
            if new_wo_id is not None:
                relinked += 1
            else:
                cleared += 1
        print(f"Quotes linked to a Works Order: {relinked} re-linked to their same job under its new ID"
              + (f", {cleared} cleared (their job no longer appears in this import)" if cleared else ""))

    cur.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()

    print(f"""
Import complete.
  Customers:         {len(customer_ids)}
  Suppliers:         {len(supplier_ids)}
  Works Orders:      {wo_count}
  Purchase Orders:   {po_count}
  Supplier Invoices: {si_count}
  Labour Entries:    {le_count}
  Customer Invoices: {ci_count}
    ↳ using TEAM order-book reference (e.g. TEAM2485): {ci_with_team_ref}
    ↳ using fallback job-based number (no TEAM ref found): {ci_count - ci_with_team_ref}

  Sum of Invoice Value (spreadsheet "Revenue"): £{total_revenue:,.2f}
  Sum of Cost          (spreadsheet "Cost"):    £{total_cost:,.2f}
""")


if __name__ == "__main__":
    main()
