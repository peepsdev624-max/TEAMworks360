"""
Split Rolls Royce works orders by site, using the Site column of the Order Book.

Usage:
    python split_customers_by_site.py path/to/Team_Order_Book.xlsm            (preview only)
    python split_customers_by_site.py path/to/Team_Order_Book.xlsm --apply    (make the changes)

At the moment almost every Rolls Royce job sits under one customer,
"Rolls Royce - Derby", so reports by customer can't tell the sites apart. The
Order Book records the site of every job (Washington, DCDD Derby, CRF Annesley,
Bristol BTFM, ...). This moves each job to the matching site customer you already
have in the app - e.g. "Washington" -> "Rolls Royce - WMF" - using the table in
SITE_TO_CUSTOMER below.

Safe by design:
  * only jobs that are CURRENTLY under "Rolls Royce - Derby" are moved - a job you
    have already set to another customer by hand is never touched;
  * a site that isn't in the table (or whose customer doesn't exist) is left where
    it is and listed in the report;
  * quotes linked to a moved job follow it to the same customer;
  * nothing is deleted. The only thing it can create is the "Rolls Royce - Ansty"
    customer, for the Ansty site that has no customer of its own yet.
  * every move is recorded in the audit log.

A preview writes site_split_preview.csv to your TEAMworks360 data folder and
changes nothing. --apply takes a timestamped backup first and makes every change
in one go (all or nothing).
"""
import csv
import sqlite3
import sys
import warnings
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location
import import_team_order_book as imp

REPORT_NAME = "site_split_preview.csv"
FROM_CUSTOMER = "Rolls Royce - Derby"
RR_NAME = "Rolls Royce"

# Order Book "Site" -> the customer to move that site's jobs to.
SITE_TO_CUSTOMER = {
    "Washington": "Rolls Royce - WMF",
    "Fleet support": "Rolls Royce - FSF",
    "DCDD Derby": "Rolls Royce - DCDD",
    "DSFT Derby": "Rolls Royce - DSFT",
    "PCF Derby": "Rolls Royce - PCF",
    "CRF Annesley": "Rolls Royce - CRF",
    "BLSK Annesley": "Rolls Royce - CRF",             # blisk cell at the Annesley site
    "Bristol": "Rolls Royce - Bristol - Turbines Facility - BTFM",   # foundry / BTFM work
    "Bristol BTFM": "Rolls Royce - Bristol - Turbines Facility - BTFM",
    "Bristol 3BSM": "Rolls Royce - Bristol - Lift Works",
    "Bristol H/C 3BSM (FC)": "Rolls Royce - Bristol - Lift Works",
    "Bristol H/C Lift Fan (FC)": "Rolls Royce - Bristol - Lift Works",
    "Bristol Liftworks (FC)": "Rolls Royce - Bristol - Lift Works",
    "Bristol Lift Fan": "Rolls Royce - Bristol - Lift Works",
    "Lift Works": "Rolls Royce - Bristol - Lift Works",
    "Repairworks": "Rolls Royce - Bristol - Repair Works",
    "Ansty": "Rolls Royce - Ansty",                   # created if it doesn't exist yet
}
CREATE_IF_MISSING = {"Rolls Royce - Ansty"}
# Barnoldswick has two RR sites but the Order Book only says "Barnoldswick", so it can't be split
# automatically. List the jobs by hand here (they win over the site table), e.g.
#     "TM-7026": "Rolls Royce - Bankfield",
#     "TM-7100": "Rolls Royce - Ghyll Brow",
JOB_OVERRIDES = {
}
# Left under "Rolls Royce - Derby" on purpose: Derby, EOS Derby, SUBS Derby (they ARE Derby),
# and Barnoldswick (could be Bankfield or Ghyll Brow - add it above once you've decided),
# ABCF Rotherham, Controls & Data, PTF and blanks (too few jobs to need a customer of their own).


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply_changes = "--apply" in sys.argv
    if not args:
        sys.exit("Usage: python split_customers_by_site.py path/to/Team_Order_Book.xlsm [--apply]")
    xlsm = Path(args[0])
    if not xlsm.exists():
        sys.exit(f"Spreadsheet not found: {xlsm}")
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to update.")

    warnings.simplefilter("ignore")
    print(f"Reading {xlsm} ...")
    rows = imp.read_rows(xlsm)
    site_of = {}
    for r in rows:
        job = imp.clean_text(r["tm_no"]) or f"TM-{r['tm_id']}"
        if imp.clean_text(r["end_customer"]) == RR_NAME:
            site_of[job] = imp.clean_text(r["site"]) or "(blank)"

    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN")
        cust = {r["customer_name"]: r["customer_id"] for r in conn.execute("SELECT customer_id, customer_name FROM customers")}
        from_id = cust.get(FROM_CUSTOMER)
        if from_id is None:
            sys.exit(f"There is no customer called '{FROM_CUSTOMER}' in this database - nothing to split.")

        moves = Counter()            # (site, to-customer) -> jobs
        left = Counter()             # site -> jobs left under Derby
        created = []
        job_moves = []               # (works_order_id, job_number, to_id, to_name)
        for wo in conn.execute("SELECT works_order_id, job_number, customer_id FROM works_orders WHERE customer_id=?", (from_id,)).fetchall():
            site = site_of.get(wo["job_number"])
            if site is None:
                left["(not in the Order Book)"] += 1
                continue
            target = JOB_OVERRIDES.get(wo["job_number"]) or SITE_TO_CUSTOMER.get(site)
            if not target:
                left[site] += 1
                continue
            if target not in cust:
                if target in CREATE_IF_MISSING:
                    cust[target] = conn.execute("INSERT INTO customers (customer_name) VALUES (?)", (target,)).lastrowid
                    created.append(target)
                else:
                    left[f"{site} (customer '{target}' doesn't exist)"] += 1
                    continue
            job_moves.append((wo["works_order_id"], wo["job_number"], cust[target], target))
            moves[(site, target)] += 1

        for wid, job, to_id, to_name in job_moves:
            conn.execute("UPDATE works_orders SET customer_id=? WHERE works_order_id=?", (to_id, wid))
            conn.execute(
                "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) VALUES (?,?,?,?,?,?)",
                ("works_orders", wid, "customer", FROM_CUSTOMER, to_name, "split_customers_by_site.py"))
        quotes_moved = 0
        for wid, job, to_id, to_name in job_moves:
            quotes_moved += conn.execute(
                "UPDATE quotes SET customer_id=? WHERE works_order_id=? AND customer_id=?", (to_id, wid, from_id)).rowcount

        db_location.ensure_data_dir()
        report = Path(db_location.DATA_DIR) / REPORT_NAME
        with open(report, "w", newline="", encoding="utf-8") as f:
            wr = csv.writer(f)
            wr.writerow(["Job", "Order Book site", "Moved to"])
            for wid, job, to_id, to_name in sorted(job_moves, key=lambda m: m[1]):
                wr.writerow([job, site_of[job], to_name])
            wr.writerow([])
            wr.writerow(["Left under " + FROM_CUSTOMER, "Jobs"])
            for site, n in left.most_common():
                wr.writerow([site, n])

        print(f"\n{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}")
        print(f"  Jobs moved to a site customer: {len(job_moves)}")
        by_target = defaultdict(int)
        for (site, target), n in moves.items():
            by_target[target] += n
        for target, n in sorted(by_target.items(), key=lambda x: -x[1]):
            sites = ", ".join(f"{s} ({m})" for (s, t), m in sorted(moves.items(), key=lambda x: -x[1]) if t == target)
            print(f"    {n:>5}  {target}   <- {sites}")
        print(f"  Quotes that followed their job: {quotes_moved}")
        if created:
            print("  New customer created: " + ", ".join(created))
        print(f"  Left under '{FROM_CUSTOMER}': {sum(left.values())}")
        for site, n in left.most_common():
            print(f"    {n:>5}  {site}")
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
