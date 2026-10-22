"""
Seed the TEAMworks360 (formerly Job Profitability Manager) database with sample data:
5 customers, 5 suppliers, 10 works orders, 20 purchase orders,
15 supplier invoices, 30 labour entries, 10 customer invoices,
plus 5 sample users (one per role).

WARNING: this always drops and recreates every table (see schema.sql) -
running it against an existing database wipes it. The launcher scripts
only call this when no database exists yet; don't run it directly
against a real, in-use database.
"""
import os
import sys
import sqlite3
import random
import datetime
from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_location

db_location.migrate_legacy_files(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = db_location.DB_PATH
random.seed(42)


def run():
    if os.path.exists(DB_PATH):
        print(f"WARNING: {DB_PATH} already exists and is about to be wiped and recreated.")
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")).read())
    cur = conn.cursor()

    # ---------------- Users (one per role, password = role username, e.g. "admin"/"admin123") ----------------
    users = [
        ("admin", "admin123", "Alex Morgan", "Administrator", "admin@jpm.example"),
        ("pmanager", "pm123", "Priya Shah", "Project Manager", "priya.shah@jpm.example"),
        ("procurement", "proc123", "Owen Clarke", "Procurement", "owen.clarke@jpm.example"),
        ("finance", "fin123", "Nadia Ibrahim", "Finance", "nadia.ibrahim@jpm.example"),
        ("exec", "exec123", "Marcus Lee", "Executive", "marcus.lee@jpm.example"),
    ]
    for username, pwd, full_name, role, email in users:
        cur.execute(
            "INSERT INTO users (username, password_hash, full_name, role, email) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(pwd), full_name, role, email),
        )

    # ---------------- Customers ----------------
    customers = [
        ("Riverside Developments Ltd", "12 Riverside Rd, Manchester", "Sarah Jenkins", "sarah@riverside-dev.co.uk", "0161 555 0101"),
        ("Northfield Manufacturing", "8 Northfield Way, Leeds", "Tom Baker", "tom.baker@northfieldmfg.co.uk", "0113 555 0102"),
        ("Solent Marine Services", "3 Harbour View, Southampton", "Emma Clarke", "emma@solentmarine.co.uk", "023 555 0103"),
        ("Grampian Facilities Group", "45 Union St, Aberdeen", "Callum Ross", "callum.ross@grampianfg.co.uk", "01224 555 0104"),
        ("Chiltern Retail Estates", "17 High St, Amersham", "Olivia Grant", "olivia@chilternretail.co.uk", "01494 555 0105"),
    ]
    for c in customers:
        cur.execute(
            "INSERT INTO customers (customer_name, address, contact_name, email, phone) VALUES (?,?,?,?,?)",
            c,
        )

    # ---------------- Suppliers ----------------
    suppliers = [
        ("BuildRight Materials Ltd", "Unit 4 Trade Park, Bristol", "Dan Foster", "dan@buildright.co.uk", "GB123456789"),
        ("Apex Steel & Fabrication", "22 Ironworks Rd, Sheffield", "Karen White", "karen@apexsteel.co.uk", "GB234567890"),
        ("Voltage Electrical Supplies", "9 Power Lane, Birmingham", "Liam Murphy", "liam@voltageelec.co.uk", "GB345678901"),
        ("ProPlant Hire", "6 Depot Rd, Coventry", "Grace Kelly", "grace@proplanthire.co.uk", "GB456789012"),
        ("Precision Mechanical Ltd", "31 Gear St, Derby", "Faisal Ahmed", "faisal@precisionmech.co.uk", "GB567890123"),
    ]
    for s in suppliers:
        cur.execute(
            "INSERT INTO suppliers (supplier_name, address, contact_name, email, vat_number) VALUES (?,?,?,?,?)",
            s,
        )

    # ---------------- Works Orders ----------------
    pms = ["Priya Shah", "James Turner", "Fiona Reid", "David Cho"]
    job_names = [
        "Riverside Apartments Phase 2", "Northfield Line Upgrade", "Solent Dry Dock Refit",
        "Grampian HQ Refurbishment", "Chiltern Retail Fit-Out", "Riverside Car Park Extension",
        "Northfield Warehouse Automation", "Solent Vessel Overhaul", "Grampian Site Electrical Upgrade",
        "Chiltern Store Rollout - Phase 1",
    ]
    statuses = ["Open", "In Progress", "In Progress", "Completed", "Closed"]
    today = datetime.date.today()
    works_orders = []
    for i in range(10):
        job_number = f"WO-{1000 + i}"
        customer_id = (i % 5) + 1
        pm = pms[i % len(pms)]
        status = statuses[i % len(statuses)]
        start = today - datetime.timedelta(days=random.randint(30, 300))
        end = start + datetime.timedelta(days=random.randint(60, 240))
        contract_value = round(random.uniform(40000, 450000), 2)
        budget_cost = round(contract_value * random.uniform(0.65, 0.88), 2)
        cur.execute(
            """INSERT INTO works_orders
               (job_number, job_name, customer_id, project_manager, status, start_date, end_date, contract_value, budget_cost)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (job_number, job_names[i], customer_id, pm, status, start.isoformat(), end.isoformat(), contract_value, budget_cost),
        )
        works_orders.append(cur.lastrowid)

    # ---------------- Purchase Orders (20, spread across works orders) ----------------
    po_descriptions = [
        "Structural steel supply", "Electrical containment & cabling", "Plant hire - excavator",
        "Concrete & aggregates", "Mechanical fabrication works", "Scaffolding hire",
        "HVAC plant supply", "Site security services", "Groundworks subcontract", "Glazing package",
    ]
    purchase_orders = []
    po_statuses = ["Approved", "Issued", "Completed", "Submitted", "Draft"]
    for i in range(20):
        wo_id = works_orders[i % len(works_orders)]
        supplier_id = (i % 5) + 1
        po_number = f"PO-{2000 + i}"
        order_date = today - datetime.timedelta(days=random.randint(5, 250))
        po_value = round(random.uniform(3000, 60000), 2)
        status = po_statuses[i % len(po_statuses)]
        cur.execute(
            """INSERT INTO purchase_orders
               (po_number, works_order_id, supplier_id, description, order_date, po_value, status)
               VALUES (?,?,?,?,?,?,?)""",
            (po_number, wo_id, supplier_id, po_descriptions[i % len(po_descriptions)], order_date.isoformat(), po_value, status),
        )
        purchase_orders.append((cur.lastrowid, wo_id, supplier_id, po_value))

    # ---------------- Supplier Invoices (15, linked to a subset of POs) ----------------
    si_statuses = ["Approved", "Paid", "Pending", "Paid", "Approved"]
    chosen_pos = random.sample(purchase_orders, 15)
    for i, (po_id, wo_id, supplier_id, po_value) in enumerate(chosen_pos):
        invoice_number = f"SINV-{3000 + i}"
        invoice_date = today - datetime.timedelta(days=random.randint(1, 200))
        # Usually invoice slightly under PO value; occasionally slightly over to trigger an alert
        pct = random.uniform(0.5, 1.08)
        invoice_amount = round(po_value * pct, 2)
        status = si_statuses[i % len(si_statuses)]
        cur.execute(
            """INSERT INTO supplier_invoices
               (invoice_number, purchase_order_id, works_order_id, supplier_id, invoice_date, invoice_amount, status)
               VALUES (?,?,?,?,?,?,?)""",
            (invoice_number, po_id, wo_id, supplier_id, invoice_date.isoformat(), invoice_amount, status),
        )

    # ---------------- Labour Entries (30) ----------------
    employees = ["Ben Carter", "Sophie Adams", "Ryan Patel", "Chloe Wilson", "Mohammed Iqbal", "Lucy Evans"]
    trades_rates = [22.5, 27.0, 32.0, 45.0, 18.5]
    for i in range(30):
        wo_id = works_orders[i % len(works_orders)]
        employee = employees[i % len(employees)]
        work_date = today - datetime.timedelta(days=random.randint(1, 200))
        hours = round(random.uniform(4, 10), 1)
        rate = random.choice(trades_rates)
        cost = round(hours * rate, 2)
        cur.execute(
            """INSERT INTO labour_entries
               (works_order_id, employee_name, work_date, hours_worked, hourly_rate, labour_cost)
               VALUES (?,?,?,?,?,?)""",
            (wo_id, employee, work_date.isoformat(), hours, rate, cost),
        )

    # ---------------- Customer Invoices (10) ----------------
    ci_statuses = ["Paid", "Sent", "Paid", "Overdue", "Draft"]
    for i in range(10):
        wo_id = works_orders[i % len(works_orders)]
        invoice_number = f"CINV-{4000 + i}"
        invoice_date = today - datetime.timedelta(days=random.randint(1, 180))
        # Base invoice amount off the works order contract value (a partial or full billing)
        cur.execute("SELECT contract_value FROM works_orders WHERE works_order_id=?", (wo_id,))
        contract_value = cur.fetchone()[0]
        invoice_amount = round(contract_value * random.uniform(0.3, 0.95), 2)
        status = ci_statuses[i % len(ci_statuses)]
        cur.execute(
            """INSERT INTO customer_invoices
               (works_order_id, invoice_number, invoice_date, invoice_amount, status)
               VALUES (?,?,?,?,?)""",
            (wo_id, invoice_number, invoice_date.isoformat(), invoice_amount, status),
        )

    conn.commit()
    conn.close()
    print(f"Database seeded successfully -> {DB_PATH}")


if __name__ == "__main__":
    run()
