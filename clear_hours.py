"""Clears the hours booked (Hours Quoted & Booked screen) so the app can start
capturing real hours from scratch.

Why this exists: the hours already in the app were not booked by anyone. When
the Order Book was imported, whatever part of a job's Cost was not bought-out
material was turned into one in-house "labour" line per job (cost / the labour
rate, in the project manager's name). This tool removes those lines - and any
other booked hours - and switches that automatic labour off, so a later Order
Book refresh does not put them back.

What it does NOT touch: jobs, purchase orders, supplier invoices, customer
invoices, goods in, despatch notes, quotes (including the labour lines inside a
quote), stock, customers, suppliers or users.

It shows a preview first and changes nothing unless run with --apply, which
takes a backup before it deletes anything. Close TEAMworks360 first.

    python clear_hours.py                    preview
    python clear_hours.py --apply            clear the booked hours
    python clear_hours.py --apply --include-quoted
                                             also clear the labour lines inside quotes
                                             (the 'hours quoted' side) - normally NOT wanted,
                                             because that edits the content of real quotes
"""
import sqlite3
import sys
from pathlib import Path

import db_location


def main():
    apply_changes = "--apply" in sys.argv
    include_quoted = "--include-quoted" in sys.argv
    if not Path(db_location.DB_PATH).exists():
        sys.exit(f"No database found at {db_location.DB_PATH} - nothing to clear.")

    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        n, hours, cost, jobs = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hours_worked),0), COALESCE(SUM(labour_cost),0), "
            "COUNT(DISTINCT works_order_id) FROM labour_entries").fetchone()
        by_hand = conn.execute(
            "SELECT COUNT(*) FROM labour_entries WHERE created_by IS NOT NULL AND created_by != ''").fetchone()[0]
        q_lines, q_hours, q_quotes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(standard_hours + ot_hours),0), COUNT(DISTINCT quote_id) "
            "FROM quote_labour_lines").fetchone()

        print(f"""
{'APPLYING' if apply_changes else 'PREVIEW - nothing has been changed'}

Hours booked
  Entries to be cleared:        {n:,}   (of which typed in by a person: {by_hand})
  Hours:                        {hours:,.1f}
  Labour cost they add to jobs: £{cost:,.2f}   across {jobs:,} jobs
  -> After clearing, those jobs show £{cost:,.2f} LESS cost (and that much MORE profit)
     until real hours are booked in the app.

Hours quoted (labour lines inside quotes)
  {q_lines:,} line(s), {q_hours:,.1f} hours, on {q_quotes:,} quote(s)
  {'Will ALSO be cleared (--include-quoted).' if include_quoted else 'Left alone - they are part of the quotes themselves. Add --include-quoted to clear them too.'}
""")
        if not apply_changes:
            print("Nothing was changed. Run again with --apply to clear the hours.")
            return
    finally:
        conn.close()

    db_location.backup_db()
    conn = sqlite3.connect(db_location.DB_PATH, timeout=10)
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM labour_entries")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='labour_entries'")
        if include_quoted:
            conn.execute("DELETE FROM quote_labour_lines")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('order_book_labour', 'off')")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("Done. Hours booked have been cleared, and the Order Book will no longer create labour lines.")
    print("A backup was taken first (in the backups folder inside your TEAMworks360 data folder).")


if __name__ == "__main__":
    main()
