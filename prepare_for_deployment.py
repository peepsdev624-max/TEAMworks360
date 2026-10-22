"""
Pre-deployment clean-up: removes the built-in DEMO accounts and Simon's TEST records.

Usage:
    python prepare_for_deployment.py            (preview only - changes nothing)
    python prepare_for_deployment.py --apply    (make the changes)

It removes ONLY these specific things, and only when they still look like the
demo / test item they were (the name or description has to match too, so a
real record that happens to share a name is never touched):

  * the five demo sign-ins that ship with the app (admin, pmanager, procurement,
    finance, exec - the ones whose passwords are printed on the demo login screen)
  * the test works orders TEST and TM-10000, with everything attached to them
    (purchase orders, goods in, despatch notes, invoices, labour)
  * the test quote TMQ-3000
  * the two test stock items 123-4567 (Gasket Seals) and XC-123 (Test pieces)

It refuses to run if removing the demo accounts would leave no Administrator who
has two-factor sign-in set up (you would be locked out). Activity history -
login activity, the audit log and the document access log - is never deleted.

--apply takes a timestamped backup of the database first and makes every change
in one go (all or nothing).
"""
import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import db_location
import app as appmod

# username -> the full name the demo seed gave that account
DEMO_USERS = {
    "admin": "Alex Morgan",
    "pmanager": "Priya Shah",
    "procurement": "Owen Clarke",
    "finance": "Nadia Ibrahim",
    "exec": "Marcus Lee",
}
# job number -> start of the job name it was created with
TEST_JOBS = {"TEST": "Test hours and parts logging", "TM-10000": "10 Off Production line A"}
TEST_QUOTES = {"TMQ-3000": "10 Off production chucks"}
TEST_STOCK = {"123-4567": "Gasket Seals", "XC-123": "Test pieces"}


def log_removal(conn, table, record_id, what):
    conn.execute(
        "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) "
        "VALUES (?,?,?,?,?,?)",
        (table, record_id, "deployment clean-up", what, "(removed)", "prepare_for_deployment.py"))


def main():
    apply_changes = "--apply" in sys.argv
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to clean.")
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    done, skipped = [], []
    files_goods, files_sigs = [], []
    try:
        conn.execute("BEGIN")

        # ---- stock items first (their allocations go with them, which frees the test job to be deleted)
        for part, desc in TEST_STOCK.items():
            r = conn.execute("SELECT * FROM stock_items WHERE part_number=?", (part,)).fetchone()
            if not r:
                continue
            if not (r["description"] or "").startswith(desc):
                skipped.append(f"Stock item {part}: description is '{r['description']}', not the test item - left alone")
                continue
            n = conn.execute("SELECT COUNT(*) FROM stock_allocations WHERE stock_item_id=?", (r["stock_item_id"],)).fetchone()[0]
            log_removal(conn, "stock_items", r["stock_item_id"], f"{part} {r['description']}")
            conn.execute("DELETE FROM stock_items WHERE stock_item_id=?", (r["stock_item_id"],))
            done.append(f"Test stock item {part} ({r['description']}) and its {n} allocation(s)")

        # ---- test jobs
        for job, name in TEST_JOBS.items():
            r = conn.execute("SELECT * FROM works_orders WHERE job_number=?", (job,)).fetchone()
            if not r:
                continue
            if not (r["job_name"] or "").startswith(name):
                skipped.append(f"Works order {job}: name is '{r['job_name']}', not the test job - left alone")
                continue
            wid = r["works_order_id"]
            counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE works_order_id=?", (wid,)).fetchone()[0]
                      for t in ("purchase_orders", "supplier_invoices", "customer_invoices", "labour_entries", "despatch_notes")}
            g, s = appmod.delete_works_order_records(conn, wid)
            files_goods += g
            files_sigs += s
            log_removal(conn, "works_orders", wid, f"{job} {r['job_name']}")
            extras = ", ".join(f"{n} {t.replace('_', ' ')}" for t, n in counts.items() if n)
            done.append(f"Test works order {job} ({r['job_name']})" + (f" with {extras}" if extras else ""))

        # ---- test quotes
        for qn, desc in TEST_QUOTES.items():
            r = conn.execute("SELECT * FROM quotes WHERE quote_number=?", (qn,)).fetchone()
            if not r:
                continue
            if not (r["description"] or "").startswith(desc):
                skipped.append(f"Quote {qn}: description is '{r['description']}', not the test quote - left alone")
                continue
            log_removal(conn, "quotes", r["quote_id"], f"{qn} {r['description']}")
            conn.execute("DELETE FROM quotes WHERE quote_id=?", (r["quote_id"],))
            done.append(f"Test quote {qn} ({r['description']})")

        # ---- demo accounts
        for username, full_name in DEMO_USERS.items():
            r = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if not r:
                continue
            if (r["full_name"] or "") != full_name:
                skipped.append(f"User {username}: name is '{r['full_name']}', not the demo account - left alone")
                continue
            log_removal(conn, "users", r["user_id"], f"{username} ({r['role']})")
            conn.execute("DELETE FROM users WHERE user_id=?", (r["user_id"],))
            done.append(f"Demo account '{username}' ({r['role']})")

        safe_admins = conn.execute(
            "SELECT username FROM users WHERE role='Administrator' AND totp_enabled=1").fetchall()
        if not safe_admins:
            conn.rollback()
            sys.exit("STOPPED - nothing changed. Removing the demo accounts would leave no Administrator with "
                     "two-factor sign-in set up, so you would be locked out. Sign in as an Administrator once "
                     "and complete the two-factor set-up first, then run this again.")

        print(f"\n{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}")
        if done:
            print("Will remove:" if not apply_changes else "Removed:")
            for d in done:
                print("  -", d)
        else:
            print("  Nothing to remove - this database is already clean.")
        for s in skipped:
            print("  (skipped)", s)
        print("\nAdministrators who can still sign in: " + ", ".join(r["username"] for r in safe_admins))

        if apply_changes:
            conn.commit()
            for fn in files_goods:
                try:
                    os.remove(os.path.join(db_location.GOODS_IN_DOCS_DIR, fn))
                except OSError:
                    pass
            for fn in files_sigs:
                try:
                    os.remove(os.path.join(db_location.SIGNATURES_DIR, fn))
                except OSError:
                    pass
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
