"""
Safely update Works Orders from a newer copy of the TEAM Order Book.

Unlike import_team_order_book.py - which WIPES every customer, supplier, works
order, purchase order and invoice and rebuilds them from the spreadsheet - this
only adds and refreshes. Nothing is deleted from your database except the
in-house labour line the importer itself generated for a job, when the
spreadsheet now shows that job has no in-house cost.

Usage:
    python update_order_book.py path/to/Team_Order_Book.xlsm            (preview only)
    python update_order_book.py path/to/Team_Order_Book.xlsm --apply    (make the changes)

What it does, matched on the job number (TM No):
  * A job in the spreadsheet that isn't in the app yet is ADDED, built exactly
    the way the original import builds a job (customer, works order, bought-out
    purchase order + supplier invoice or in-house labour, customer invoice).
  * A job already in the app has its works order details refreshed from the
    spreadsheet (status, dates, value, owner, customer, contract code, details),
    and the records the import generated for it (customer invoice, bought-out PO
    and supplier invoice, labour line) brought into line with the new figures.
  * Anything that has been touched by hand in the app is left exactly as it is
    and listed in the report for you to review: a job (or one of its
    generated records) created or edited by a person rather than the import, and
    any works order's budget once someone has confirmed it. Jobs that are in the
    app but not in the spreadsheet (e.g. test jobs) are never touched.
  * An existing job's customer is never changed. A new job is given the customer the
    app already uses for that customer/site (the app splits e.g. Rolls Royce by
    site); only genuinely new customers and suppliers are created, and existing
    ones keep every detail you've added (addresses, contacts).

A preview writes a CSV listing every change to your TEAMworks360 data folder and
changes nothing. --apply takes a timestamped backup of the database first, then
makes all the changes in one go (all or nothing).
"""
import csv
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location
import import_team_order_book as imp
import migrations

REPORT_NAME = "order_book_update_preview.csv"


def money_differs(a, b):
    return abs((a or 0) - (b or 0)) >= 0.005


def build_plan(row):
    """What the original import would build for this spreadsheet row. Mirrors
    import_team_order_book.main() exactly, so a job added or refreshed here
    ends up identical to one the full import would have produced."""
    job_number = imp.clean_text(row["tm_no"]) or f"TM-{row['tm_id']}"
    job_name = imp.build_job_name(row)
    status = imp.map_status(row["status"])
    invoice_value = imp.to_number(row["invoice_value"])
    cost = imp.to_number(row["cost"])
    start_date = imp.to_date(row["enquiry_date"])
    end_date = imp.to_date(row["delivered_on"]) or imp.to_date(row["delivery_req"])
    date_invoiced = imp.to_date(row["date_invoiced"])

    supplier1 = imp.normalise_supplier(imp.clean_text(row["supplier1"]))
    # The supplier register merge renames some suppliers to their fuller names
    # (e.g. "Towne" -> "Towne Lifting"); use the same names here so a job isn't
    # given a duplicate supplier.
    if supplier1:
        supplier1 = migrations.SUPPLIER_MERGE_MAP.get(supplier1) or supplier1
    bought_out = imp.to_number(row["bought_out_value"])
    if supplier1 and bought_out <= 0 and cost > 0:
        bought_out = cost
    bought_out = min(bought_out, cost) if cost > 0 else 0.0
    labour_remainder = max(0.0, cost - bought_out)

    if status == "Cancelled":
        ci_status = "Cancelled"
    elif date_invoiced:
        ci_status = "Paid"
    elif end_date:
        ci_status = "Sent"
    else:
        ci_status = "Draft"
    invoice_number = imp.extract_team_ref(row["coupa_ref"], row["team_quote_ref"]) or f"INV-{job_number}"

    project_manager = imp.clean_text(row["owner"]) or ""
    return {
        "job_number": job_number,
        "customer_name": imp.clean_text(row["end_customer"]),
        "site": imp.clean_text(row["site"]),
        "site_area": imp.clean_text(row["site_area"]),
        "wo": {
            "job_name": job_name,
            "project_manager": project_manager,
            "status": status,
            "start_date": start_date,
            "end_date": end_date,
            "contract_value": round(invoice_value, 2),
            "contract_code": imp.normalise_contract_code(imp.clean_text(row["contract_code"])),
            "location": imp.clean_text(row["location"]),
        },
        "budget_cost": round(round(invoice_value, 2) * 0.35, 2),
        "supplier": supplier1,
        "bought_out": round(bought_out, 2),
        "labour": round(labour_remainder, 2),
        "po_number": imp.clean_text(row["team_po"]) or f"PO-{job_number}",
        "po_status": "Completed" if status == "Completed" else "Issued",
        "si_status": "Paid" if date_invoiced else ("Approved" if status == "Completed" else "Pending"),
        "inv_date": date_invoiced or end_date or start_date,
        "ci_status": ci_status,
        "ci_number": invoice_number,
        "invoice_value": round(invoice_value, 2),
    }


class Updater:
    def __init__(self, conn):
        self.conn = conn
        self.cur = conn.cursor()
        self.changes = []  # (job, area, what, old, new)
        self.review = []   # (job, why)
        self.counts = {
            "jobs_added": 0, "jobs_updated": 0, "jobs_unchanged": 0, "jobs_left_alone": 0,
            "customers_added": 0, "suppliers_added": 0,
            "invoices_changed": 0, "supplier_records_changed": 0, "labour_changed": 0,
        }
        self.customer_ids = {n.lower(): i for i, n in self.cur.execute("SELECT customer_id, customer_name FROM customers")}
        self.supplier_ids = {n.lower(): i for i, n in self.cur.execute("SELECT supplier_id, supplier_name FROM suppliers")}
        self.po_numbers = {r[0] for r in self.cur.execute("SELECT po_number FROM purchase_orders")}

    # -- customers ---------------------------------------------------------
    # In the app, customers have been split by site (e.g. the spreadsheet's
    # "Rolls Royce" at Derby is "Rolls Royce - Derby"). That choice is learned
    # from the jobs already in the app - the same (customer, site, area) always
    # went to the same app customer - and used for NEW jobs. Existing jobs keep
    # whatever customer they already have.
    def learn_customers(self, plans, existing):
        from collections import Counter, defaultdict
        full, by_site = defaultdict(Counter), defaultdict(Counter)
        for plan in plans:
            wo = existing.get(plan["job_number"])
            if wo is None:
                continue
            full[(plan["customer_name"], plan["site"], plan["site_area"])][wo["customer_id"]] += 1
            by_site[(plan["customer_name"], plan["site"])][wo["customer_id"]] += 1
        self.map_full = {k: v.most_common(1)[0][0] for k, v in full.items() if len(v) == 1}
        self.map_site = {k: next(iter(v)) for k, v in by_site.items() if len(v) == 1}

    def customer_for_new_job(self, plan):
        job = plan["job_number"]
        name = plan["customer_name"]
        if not name:
            self.review.append((job, "no customer in the spreadsheet - put under 'Unknown Customer', please set the right one"))
            return self.customer_id("Unknown Customer", job)
        key = (name, plan["site"], plan["site_area"])
        if key in self.map_full:
            return self.map_full[key]
        if (name, plan["site"]) in self.map_site:
            return self.map_site[(name, plan["site"])]
        if name.lower() in self.customer_ids:
            return self.customer_ids[name.lower()]
        # A customer/site combination the app hasn't seen before: name it the
        # way the app names the others and flag it so it can be checked.
        new_name = f"{name} - {plan['site']}" if plan["site"] else name
        self.review.append((job, f"new customer/site combination - assigned to '{new_name}', please check"))
        return self.customer_id(new_name, job)

    def customer_id(self, name, job):
        name = name or "Unknown Customer"
        cid = self.customer_ids.get(name.lower())
        if cid is None:
            self.cur.execute("INSERT INTO customers (customer_name) VALUES (?)", (name,))
            cid = self.cur.lastrowid
            self.customer_ids[name.lower()] = cid
            self.counts["customers_added"] += 1
            self.changes.append((job, "customer", "new customer", "", name))
        return cid

    def supplier_id(self, name, job):
        sid = self.supplier_ids.get(name.lower())
        if sid is None:
            self.cur.execute("INSERT INTO suppliers (supplier_name) VALUES (?)", (name,))
            sid = self.cur.lastrowid
            self.supplier_ids[name.lower()] = sid
            self.counts["suppliers_added"] += 1
            self.changes.append((job, "supplier", "new supplier", "", name))
        return sid

    def unique_po_number(self, po_number, job):
        if po_number in self.po_numbers:
            po_number = f"{po_number}-{job}"
        self.po_numbers.add(po_number)
        return po_number

    # -- adding a job the app has never seen --------------------------------
    def add_job(self, plan):
        job = plan["job_number"]
        w = plan["wo"]
        cid = self.customer_for_new_job(plan)
        self.cur.execute(
            """INSERT INTO works_orders
               (job_number, job_name, customer_id, project_manager, status, start_date, end_date,
                contract_value, budget_cost, budget_is_estimated, contract_code, location)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (job, w["job_name"], cid, w["project_manager"], w["status"], w["start_date"], w["end_date"],
             w["contract_value"], plan["budget_cost"], w["contract_code"], w["location"]),
        )
        wo_id = self.cur.lastrowid
        self.counts["jobs_added"] += 1
        self.changes.append((job, "works order", "new job", "", f"{w['status']}, £{w['contract_value']:,.2f}"))
        self.insert_supplier_side(wo_id, plan)
        self.insert_labour(wo_id, plan)
        self.insert_customer_invoice(wo_id, plan)

    def insert_supplier_side(self, wo_id, plan):
        if not (plan["supplier"] and plan["bought_out"] > 0):
            return
        job = plan["job_number"]
        sid = self.supplier_id(plan["supplier"], job)
        po_number = self.unique_po_number(plan["po_number"], job)
        w = plan["wo"]
        self.cur.execute(
            """INSERT INTO purchase_orders
               (po_number, works_order_id, supplier_id, description, order_date, po_value, status)
               VALUES (?,?,?,?,?,?,?)""",
            (po_number, wo_id, sid, w["job_name"][:200], w["start_date"], plan["bought_out"], plan["po_status"]),
        )
        po_id = self.cur.lastrowid
        self.cur.execute(
            """INSERT INTO supplier_invoices
               (invoice_number, purchase_order_id, works_order_id, supplier_id, invoice_date, invoice_amount, status)
               VALUES (?,?,?,?,?,?,?)""",
            (f"SINV-{job}", po_id, wo_id, sid, w["end_date"] or w["start_date"], plan["bought_out"], plan["si_status"]),
        )
        self.counts["supplier_records_changed"] += 1
        self.changes.append((job, "supplier cost", "added bought-out PO + invoice", "", f"{plan['supplier']} £{plan['bought_out']:,.2f}"))

    def insert_labour(self, wo_id, plan):
        if plan["labour"] <= 0:
            return
        w = plan["wo"]
        self.cur.execute(
            """INSERT INTO labour_entries
               (works_order_id, employee_name, work_date, hours_worked, hourly_rate, labour_cost)
               VALUES (?,?,?,?,?,?)""",
            (wo_id, w["project_manager"] or "Unassigned", w["end_date"] or w["start_date"],
             round(plan["labour"] / imp.LABOUR_RATE, 2), imp.LABOUR_RATE, plan["labour"]),
        )
        self.counts["labour_changed"] += 1
        self.changes.append((plan["job_number"], "labour cost", "added in-house labour", "", f"£{plan['labour']:,.2f}"))

    def insert_customer_invoice(self, wo_id, plan):
        self.cur.execute(
            """INSERT INTO customer_invoices
               (works_order_id, invoice_number, invoice_date, invoice_amount, status)
               VALUES (?,?,?,?,?)""",
            (wo_id, plan["ci_number"], plan["inv_date"], plan["invoice_value"], plan["ci_status"]),
        )
        self.counts["invoices_changed"] += 1

    # -- refreshing a job that already exists ---------------------------------
    def update_job(self, wo, plan):
        job = plan["job_number"]
        # Created or edited by a person in the app: the app copy is the more
        # trustworthy one, so leave it entirely alone and flag it.
        if wo["created_by"] or wo["updated_by"]:
            self.counts["jobs_left_alone"] += 1
            self.review.append((job, "created/edited in the app - left exactly as it was"))
            return

        before = (self.counts["invoices_changed"], self.counts["supplier_records_changed"], self.counts["labour_changed"])
        changed = False
        w = plan["wo"]
        wo_id = wo["works_order_id"]
        # The customer on an existing job is never changed (see learn_customers).
        sets, args = [], []
        for field, new in w.items():
            old = wo[field]
            differs = money_differs(old, new) if field == "contract_value" else ((old or None) != (new or None))
            if differs:
                sets.append(f"{field}=?")
                args.append(new)
                self.changes.append((job, "works order", field.replace("_", " "), old, new))
        # Budget follows the invoice value only while it's still the import's
        # own 35% estimate - once someone has confirmed a budget in the app it
        # is theirs and stays.
        if wo["budget_is_estimated"] and money_differs(wo["budget_cost"], plan["budget_cost"]):
            sets.append("budget_cost=?")
            args.append(plan["budget_cost"])
            self.changes.append((job, "works order", "budget cost", wo["budget_cost"], plan["budget_cost"]))
        if sets:
            self.cur.execute(f"UPDATE works_orders SET {', '.join(sets)} WHERE works_order_id=?", args + [wo_id])
            changed = True

        self.refresh_customer_invoice(wo_id, plan)
        self.refresh_supplier_side(wo_id, plan)
        self.refresh_labour(wo_id, plan)

        after = (self.counts["invoices_changed"], self.counts["supplier_records_changed"], self.counts["labour_changed"])
        if changed or after != before:
            self.counts["jobs_updated"] += 1
        else:
            self.counts["jobs_unchanged"] += 1

    def name_of_customer(self, cid):
        row = self.cur.execute("SELECT customer_name FROM customers WHERE customer_id=?", (cid,)).fetchone()
        return row[0] if row else cid

    def generated(self, table, wo_id, extra="", params=()):
        # A record the import generated: nobody has created or edited it by hand.
        return self.cur.execute(
            f"SELECT * FROM {table} WHERE works_order_id=? AND created_by IS NULL AND updated_by IS NULL {extra} ORDER BY 1",
            (wo_id, *params),
        ).fetchall()

    def refresh_customer_invoice(self, wo_id, plan):
        job = plan["job_number"]
        rows = self.generated("customer_invoices", wo_id)
        if not rows:
            any_inv = self.cur.execute("SELECT COUNT(*) FROM customer_invoices WHERE works_order_id=?", (wo_id,)).fetchone()[0]
            if any_inv:
                self.review.append((job, "customer invoice was created/edited by hand - not changed"))
            else:
                self.insert_customer_invoice(wo_id, plan)
                self.changes.append((job, "customer invoice", "added", "", f"£{plan['invoice_value']:,.2f}"))
            return
        inv = rows[0]
        sets, args = [], []
        for col, new in (("invoice_number", plan["ci_number"]), ("invoice_date", plan["inv_date"]),
                         ("invoice_amount", plan["invoice_value"]), ("status", plan["ci_status"])):
            old = inv[col]
            differs = money_differs(old, new) if col == "invoice_amount" else ((old or None) != (new or None))
            if differs:
                sets.append(f"{col}=?")
                args.append(new)
                self.changes.append((job, "customer invoice", col.replace("_", " "), old, new))
        if sets:
            self.cur.execute(f"UPDATE customer_invoices SET {', '.join(sets)} WHERE customer_invoice_id=?",
                             args + [inv["customer_invoice_id"]])
            self.counts["invoices_changed"] += 1

    def refresh_supplier_side(self, wo_id, plan):
        job = plan["job_number"]
        si_rows = self.generated("supplier_invoices", wo_id, "AND invoice_number = ?", (f"SINV-{job}",))
        want = plan["supplier"] and plan["bought_out"] > 0
        if not si_rows:
            if want:
                existing = self.cur.execute(
                    "SELECT COUNT(*) FROM supplier_invoices WHERE works_order_id=? AND invoice_number=?",
                    (wo_id, f"SINV-{job}")).fetchone()[0]
                if existing:
                    self.review.append((job, "bought-out supplier invoice was edited by hand - not changed"))
                else:
                    self.insert_supplier_side(wo_id, plan)
            return
        si = si_rows[0]
        if not want:
            if money_differs(si["invoice_amount"], 0):
                self.review.append((job, f"spreadsheet no longer shows bought-out cost (app has £{si['invoice_amount']:,.2f}) - not changed"))
            return
        po = self.cur.execute("SELECT * FROM purchase_orders WHERE purchase_order_id=?", (si["purchase_order_id"],)).fetchone()
        sid = self.supplier_id(plan["supplier"], job)
        w = plan["wo"]
        si_new = {"supplier_id": sid, "invoice_date": w["end_date"] or w["start_date"],
                  "invoice_amount": plan["bought_out"], "status": plan["si_status"]}
        sets, args = [], []
        for col, new in si_new.items():
            old = si[col]
            differs = money_differs(old, new) if col == "invoice_amount" else ((old or None) != (new or None))
            if differs:
                sets.append(f"{col}=?")
                args.append(new)
                shown_old = self.name_of_supplier(old) if col == "supplier_id" else old
                shown_new = plan["supplier"] if col == "supplier_id" else new
                self.changes.append((job, "supplier invoice", col.replace("_", " "), shown_old, shown_new))
        if sets:
            self.cur.execute(f"UPDATE supplier_invoices SET {', '.join(sets)} WHERE supplier_invoice_id=?",
                             args + [si["supplier_invoice_id"]])
            self.counts["supplier_records_changed"] += 1
        # The PO behind it, only if it's still the import's own (not raised by a person).
        if po is not None and not po["raised_by"]:
            po_new = {"supplier_id": sid, "po_value": plan["bought_out"], "status": plan["po_status"]}
            psets, pargs = [], []
            for col, new in po_new.items():
                old = po[col]
                differs = money_differs(old, new) if col == "po_value" else ((old or None) != (new or None))
                if differs:
                    psets.append(f"{col}=?")
                    pargs.append(new)
                    self.changes.append((job, "purchase order", col.replace("_", " "), old, new))
            if psets:
                self.cur.execute(f"UPDATE purchase_orders SET {', '.join(psets)} WHERE purchase_order_id=?",
                                 pargs + [po["purchase_order_id"]])
                self.counts["supplier_records_changed"] += 1

    def name_of_supplier(self, sid):
        row = self.cur.execute("SELECT supplier_name FROM suppliers WHERE supplier_id=?", (sid,)).fetchone()
        return row[0] if row else sid

    def refresh_labour(self, wo_id, plan):
        job = plan["job_number"]
        rows = self.generated("labour_entries", wo_id)
        w = plan["wo"]
        if plan["labour"] <= 0:
            for r in rows:
                self.cur.execute("DELETE FROM labour_entries WHERE labour_entry_id=?", (r["labour_entry_id"],))
                self.counts["labour_changed"] += 1
                self.changes.append((job, "labour cost", "removed in-house labour (spreadsheet shows none)", r["labour_cost"], 0))
            return
        hours = round(plan["labour"] / imp.LABOUR_RATE, 2)
        if not rows:
            self.insert_labour(wo_id, plan)
            return
        r = rows[0]
        new = {"employee_name": w["project_manager"] or "Unassigned", "work_date": w["end_date"] or w["start_date"],
               "hours_worked": hours, "labour_cost": plan["labour"]}
        sets, args = [], []
        for col, val in new.items():
            old = r[col]
            differs = money_differs(old, val) if col in ("hours_worked", "labour_cost") else ((old or None) != (val or None))
            if differs:
                sets.append(f"{col}=?")
                args.append(val)
                if col == "labour_cost":
                    self.changes.append((job, "labour cost", "in-house labour cost", old, val))
        if sets:
            self.cur.execute(f"UPDATE labour_entries SET {', '.join(sets)} WHERE labour_entry_id=?",
                             args + [r["labour_entry_id"]])
            self.counts["labour_changed"] += 1


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv
    if not args:
        sys.exit("Usage: python update_order_book.py path/to/Team_Order_Book.xlsm [--apply]")
    xlsm = Path(args[0])
    if not xlsm.exists():
        sys.exit(f"Spreadsheet not found: {xlsm}")
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to update.")

    print(f"Reading {xlsm} ...")
    rows = imp.read_rows(xlsm)
    print(f"Found {len(rows)} order rows in the spreadsheet.")
    plans = [build_plan(r) for r in rows]

    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    existing = {r["job_number"]: r for r in conn.execute("SELECT * FROM works_orders")}
    up = Updater(conn)
    try:
        conn.execute("BEGIN")
        up.learn_customers(plans, existing)
        seen = set()
        for plan in plans:
            job = plan["job_number"]
            if job in seen:
                up.review.append((job, "appears more than once in the spreadsheet - later row ignored"))
                continue
            seen.add(job)
            if job in existing:
                up.update_job(existing[job], plan)
            else:
                up.add_job(plan)
        in_app_only = sorted(set(existing) - seen)

        report = Path(db_location.DATA_DIR) / REPORT_NAME
        db_location.ensure_data_dir()
        with open(report, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["Job", "Area", "What", "Was", "Now"])
            wr.writerows(up.changes)
            wr.writerow([])
            wr.writerow(["FOR YOUR REVIEW (not changed)", "Why"])
            wr.writerows(up.review)
            wr.writerow([])
            wr.writerow(["In the app but not in the spreadsheet (not touched)"])
            for j in in_app_only:
                wr.writerow([j])

        c = up.counts
        print(f"""
{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}
  New jobs added:               {c['jobs_added']}
  Existing jobs updated:        {c['jobs_updated']}
  Existing jobs already correct:{c['jobs_unchanged']:>5}
  Jobs left alone (edited in app): {c['jobs_left_alone']}
  New customers / suppliers:    {c['customers_added']} / {c['suppliers_added']}
  Customer invoices changed:    {c['invoices_changed']}
  Supplier invoice/PO changes:  {c['supplier_records_changed']}
  Labour entries changed:       {c['labour_changed']}
  In the app but not in the spreadsheet (untouched): {len(in_app_only)}
  Items flagged for your review: {len(up.review)}

Full list of changes: {report}
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
    # Backup happens before anything else in --apply mode, outside the
    # transaction above, using the same backup routine the app uses at startup.
    if "--apply" in sys.argv and Path(db_location.DB_PATH).exists():
        db_location.backup_db()
        print(f"Backup of your database taken in {db_location.BACKUP_DIR}")
    main()
