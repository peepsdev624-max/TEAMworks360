"""
Schema self-healing and one-off data migrations, run automatically every
time the app starts (see app.py's __main__ block). This exists because of
a real gap: once a database lives in the external data directory (see
db_location.py), replacing the app folder to install an update no longer
touches it at all - so a code update that expects a new column, or that
bundles new reference data to import, would otherwise never reach an
existing installation. Schema changes self-heal unconditionally; each data
migration runs at most once per database, tracked in the settings table,
so re-running the app never re-applies (or re-imports) the same thing twice.

To add a future migration: add the new column(s) to the dict in
ensure_schema_current(), and/or add a new function following the same
shape as migrate_supplier_list() (check _migration_applied, do the work,
call _mark_migration_applied), then call it from run_migrations().
"""
import os
import re


def ensure_schema_current(db):
    """Add any column that exists in schema.sql but not yet in this
    database, and create any whole new table that doesn't exist yet
    either. Safe to call on every startup - anything already present is
    left untouched."""
    # table -> list of (column_name, sql_type_and_default) to add if missing
    expected_columns = {
        "customers": [
            ("created_by", "TEXT"), ("created_date", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
        ],
        "suppliers": [
            ("supply_of", "TEXT"), ("reason_for_use", "TEXT"), ("authorised_by", "TEXT"),
            ("created_by", "TEXT"), ("created_date", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
        ],
        "works_orders": [
            ("created_by", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
            ("location", "TEXT"),
        ],
        "purchase_orders": [
            ("date_required", "TEXT"), ("account_number", "TEXT"), ("raised_by", "TEXT"),
            ("vat_rate", "REAL NOT NULL DEFAULT 20"),
            ("is_historic", "INTEGER NOT NULL DEFAULT 0"),
            ("register_job_ref", "TEXT"),
        ],
        "supplier_invoices": [
            ("created_by", "TEXT"), ("created_date", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
        ],
        "labour_entries": [
            ("created_by", "TEXT"), ("created_date", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
        ],
        "customer_invoices": [
            ("created_by", "TEXT"), ("created_date", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
        ],
        "quotes": [
            ("works_order_id", "INTEGER REFERENCES works_orders(works_order_id)"),
            ("created_by", "TEXT"), ("updated_by", "TEXT"), ("updated_date", "TEXT"),
        ],
        "contract_code_targets": [
            ("standard_value", "REAL"), ("standard_cost", "REAL"),
        ],
        "users": [
            ("must_change_password", "INTEGER NOT NULL DEFAULT 0"),
            ("totp_secret", "TEXT"),
            ("totp_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ],
        "despatch_notes": [
            ("signature_filename", "TEXT"), ("signature_name", "TEXT"),
            ("signature_date", "TEXT"), ("signature_captured_by", "TEXT"),
        ],
    }
    changed = False
    for table, columns in expected_columns.items():
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for col, col_type in columns:
            if col not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                print(f"Schema update: added {table}.{col}")
                changed = True

    # whole new tables that might not exist yet on an older database
    existing_tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "purchase_order_lines" not in existing_tables:
        db.execute("""
            CREATE TABLE purchase_order_lines (
                line_id             INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_order_id     INTEGER NOT NULL REFERENCES purchase_orders(purchase_order_id) ON DELETE CASCADE,
                qty                    REAL NOT NULL DEFAULT 1,
                part_number             TEXT,
                item_number               TEXT,
                description                 TEXT,
                unit_price                   REAL NOT NULL DEFAULT 0
            )
        """)
        db.execute("CREATE INDEX idx_po_lines_po ON purchase_order_lines(purchase_order_id)")
        print("Schema update: created table purchase_order_lines")
        changed = True

    if "goods_in" not in existing_tables:
        db.execute("""
            CREATE TABLE goods_in (
                goods_in_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_order_id   INTEGER NOT NULL REFERENCES purchase_orders(purchase_order_id),
                destination_type      TEXT NOT NULL CHECK (destination_type IN ('Works Order', 'Stock')),
                ship_to_id               INTEGER REFERENCES ship_to_addresses(ship_to_id),
                received_date              TEXT NOT NULL,
                quantity_status               TEXT NOT NULL CHECK (quantity_status IN ('Full', 'Partial')),
                quantity_received                REAL,
                received_by                         TEXT NOT NULL,
                comment                                TEXT,
                created_date                            TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_goods_in_po ON goods_in(purchase_order_id)")
        print("Schema update: created table goods_in")
        changed = True

    if "goods_in_documents" not in existing_tables:
        db.execute("""
            CREATE TABLE goods_in_documents (
                document_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                goods_in_id           INTEGER NOT NULL REFERENCES goods_in(goods_in_id) ON DELETE CASCADE,
                original_filename       TEXT NOT NULL,
                stored_filename           TEXT NOT NULL UNIQUE,
                content_type                 TEXT,
                file_size                       INTEGER,
                uploaded_by                         TEXT NOT NULL,
                uploaded_date                           TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_goods_in_documents_gi ON goods_in_documents(goods_in_id)")
        print("Schema update: created table goods_in_documents")
        changed = True

    if "despatch_notes" not in existing_tables:
        db.execute("""
            CREATE TABLE despatch_notes (
                despatch_note_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                despatch_number     TEXT UNIQUE NOT NULL,
                works_order_id        INTEGER NOT NULL REFERENCES works_orders(works_order_id),
                despatch_date            TEXT,
                delivery_address           TEXT,
                carrier                      TEXT,
                despatched_by                  TEXT,
                notes                             TEXT,
                status                              TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft', 'Despatched', 'Cancelled')),
                created_by        TEXT,
                created_date       TEXT DEFAULT (datetime('now')),
                updated_by          TEXT,
                updated_date          TEXT
            )
        """)
        db.execute("CREATE INDEX idx_despatch_wo ON despatch_notes(works_order_id)")
        print("Schema update: created table despatch_notes")
        changed = True

    if "despatch_note_lines" not in existing_tables:
        db.execute("""
            CREATE TABLE despatch_note_lines (
                line_id            INTEGER PRIMARY KEY AUTOINCREMENT,
                despatch_note_id     INTEGER NOT NULL REFERENCES despatch_notes(despatch_note_id) ON DELETE CASCADE,
                qty                     REAL NOT NULL DEFAULT 1,
                part_number               TEXT,
                description                 TEXT
            )
        """)
        db.execute("CREATE INDEX idx_despatch_lines_dn ON despatch_note_lines(despatch_note_id)")
        print("Schema update: created table despatch_note_lines")
        changed = True

    if "audit_log" not in existing_tables:
        db.execute("""
            CREATE TABLE audit_log (
                audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name    TEXT NOT NULL,
                record_id     INTEGER NOT NULL,
                field_name    TEXT NOT NULL,
                old_value     TEXT,
                new_value     TEXT,
                changed_by    TEXT,
                changed_date  TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_audit_log_record ON audit_log(table_name, record_id)")
        print("Schema update: created table audit_log")
        changed = True

    if "login_events" not in existing_tables:
        db.execute("""
            CREATE TABLE login_events (
                event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL,
                ip_address    TEXT,
                event_type    TEXT NOT NULL CHECK (event_type IN ('success','failure','lockout')),
                event_date    TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_login_events_date ON login_events(event_date)")
        print("Schema update: created table login_events")
        changed = True

    if "totp_backup_codes" not in existing_tables:
        db.execute("""
            CREATE TABLE totp_backup_codes (
                backup_code_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                code_hash       TEXT NOT NULL,
                created_date    TEXT DEFAULT (datetime('now')),
                used_date       TEXT
            )
        """)
        db.execute("CREATE INDEX idx_totp_backup_codes_user ON totp_backup_codes(user_id)")
        print("Schema update: created table totp_backup_codes")
        changed = True

    if "document_access_log" not in existing_tables:
        db.execute("""
            CREATE TABLE document_access_log (
                access_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                username       TEXT NOT NULL,
                document_type  TEXT NOT NULL,
                reference      TEXT,
                ip_address     TEXT,
                access_date    TEXT DEFAULT (datetime('now'))
            )
        """)
        db.execute("CREATE INDEX idx_document_access_log_date ON document_access_log(access_date)")
        print("Schema update: created table document_access_log")
        changed = True

    if "alert_acceptances" not in existing_tables:
        db.execute("""
            CREATE TABLE alert_acceptances (
                acceptance_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_key      TEXT NOT NULL UNIQUE,
                job_number     TEXT,
                alert_type     TEXT,
                message        TEXT,
                reason         TEXT NOT NULL,
                accepted_by    TEXT,
                accepted_date  TEXT DEFAULT (datetime('now'))
            )
        """)
        print("Schema update: created table alert_acceptances")
        changed = True

    if changed:
        db.commit()


def _migration_applied(db, key):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row is not None


def _mark_migration_applied(db, key):
    db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, '1')", (key,))
    db.commit()


# existing supplier_name -> new fuller name to rename to (None = keep as-is).
# Only includes matches confident enough to merge rather than create a
# duplicate that would split real purchase history across two records.
# See import_supplier_list.py (the standalone, re-runnable version of this
# same logic) for the full reasoning.
SUPPLIER_MERGE_MAP = {
    "EM Precision": None,
    "FPE Seals": None,
    "Fife Engineering": None,
    "Roemheld": None,
    "Cromwell": "Cromwell Tools",
    "Industrial Interface": "Industrial Interface systems",
    "Pollard": "Pollard Engineering",
    "Quill": "Quill International",
    "Stonebridge": "Stonebridge Testing",
    "Towne": "Towne Lifting",
    "WDS": "WDS Components",
    "Zouch": "Zouch Convertors",
}


def migrate_supplier_list(db, base_dir):
    """Reconciles the suppliers table against the business's supplier
    register every startup - deliberately NOT a run-once migration. The
    Order Book importer wipes and rebuilds the suppliers table from
    scratch on every run (it has to, to handle suppliers renamed or
    removed in a fresh export), which would otherwise silently undo this
    import the next time someone re-imports the Order Book. Re-running
    this is safe and cheap: matching by current name means an
    already-merged supplier (e.g. "Cromwell Tools") is recognised via the
    plain existing-name branch below and just gets its fields refreshed,
    never duplicated - so there's nothing to track or guard against
    re-running.

    A migration step is never allowed to stop the app from starting - if
    anything here goes wrong (most likely: openpyxl isn't installed, since
    it's only needed for this and the standalone import scripts, not for
    running the app day to day), this prints a clear one-line warning and
    lets startup continue, rather than the whole app crashing over a
    supplier-list refresh it can retry next time."""
    xlsx_path = os.path.join(base_dir, "data", "Supplier_List_14_9_26.xlsx")
    if not os.path.exists(xlsx_path):
        return

    try:
        import openpyxl
    except ImportError:
        print("Skipped supplier list update: the 'openpyxl' package isn't installed "
              "(run: pip install -r requirements.txt). Everything else is starting normally.")
        return

    try:
        _migrate_supplier_list_inner(db, xlsx_path)
    except Exception as e:
        print(f"Skipped supplier list update: {e}. Everything else is starting normally.")


def _migrate_supplier_list_inner(db, xlsx_path):
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb["Sheet1"]
    new_rows = {}
    for r in range(3, ws.max_row + 1):
        name = ws.cell(row=r, column=2).value
        if not name:
            continue
        name = str(name).strip()
        new_rows[name] = (ws.cell(row=r, column=3).value, ws.cell(row=r, column=4).value, ws.cell(row=r, column=5).value)

    existing = {row["supplier_name"]: row["supplier_id"] for row in db.execute("SELECT supplier_id, supplier_name FROM suppliers")}
    merge_targets = {}
    for existing_name, new_name in SUPPLIER_MERGE_MAP.items():
        if existing_name not in existing:
            continue
        lookup_name = new_name or existing_name
        merge_targets[lookup_name] = (existing[existing_name], new_name)

    merged_count, created_count, unchanged_count = 0, 0, 0
    for name, (supply_of, reason, auth) in new_rows.items():
        if name in merge_targets:
            supplier_id, rename_to = merge_targets[name]
            if rename_to:
                db.execute(
                    "UPDATE suppliers SET supplier_name=?, supply_of=?, reason_for_use=?, authorised_by=? WHERE supplier_id=?",
                    (rename_to, supply_of, reason, auth, supplier_id),
                )
            else:
                db.execute(
                    "UPDATE suppliers SET supply_of=?, reason_for_use=?, authorised_by=? WHERE supplier_id=?",
                    (supply_of, reason, auth, supplier_id),
                )
            merged_count += 1
        elif name in existing:
            db.execute(
                "UPDATE suppliers SET supply_of=?, reason_for_use=?, authorised_by=? WHERE supplier_id=?",
                (supply_of, reason, auth, existing[name]),
            )
            unchanged_count += 1
        else:
            db.execute(
                "INSERT INTO suppliers (supplier_name, supply_of, reason_for_use, authorised_by) VALUES (?,?,?,?)",
                (name, supply_of, reason, auth),
            )
            created_count += 1

    db.commit()
    if created_count:  # only worth a startup message when it actually added something new
        print(f"Supplier list reconciled: {created_count} created, {merged_count} merged, {unchanged_count} already present")


# contract_code -> (target_pct, standard_value, standard_cost)
FIXED_PRICE_CONTRACT_CODES = {
    "FS - L": (20, 179.79, 143.83),
    "FS - M": (20, 309.12, 247.29),
    "FS - H": (20, 579.60, 463.68),
    "FS - HQ": (20, 450.00, 360.00),
}


def seed_fixed_price_contract_codes(db):
    """Adds the four fixed-price FS tiers if they don't already exist -
    INSERT OR IGNORE, never overwriting a row that's already there, so an
    Administrator's own edit to one of these via Settings (a different
    target %, say) is never silently reverted by a later restart."""
    added = 0
    for code, (target_pct, value, cost) in FIXED_PRICE_CONTRACT_CODES.items():
        cur = db.execute(
            "INSERT OR IGNORE INTO contract_code_targets (contract_code, target_pct, standard_value, standard_cost) VALUES (?,?,?,?)",
            (code, target_pct, value, cost),
        )
        if cur.rowcount:
            added += 1
    if added:
        db.commit()
        print(f"Added {added} fixed-price contract code(s): {', '.join(FIXED_PRICE_CONTRACT_CODES.keys())}")


_JOB_NAME_LOCATION_PREFIX = re.compile(r"^\[([^\]]+)\]\s*")


def backfill_works_order_locations(db):
    """Fills the new works_orders.location column for any row that doesn't
    have one yet, by pulling it out of the "[Location] " prefix that
    build_job_name() in import_team_order_book.py has always stuck on the
    front of job_name whenever the source spreadsheet had a location for
    that row. This only touches rows where location IS NULL, so it's safe
    to run on every startup: a location set any other way (a fresh import
    that now stores it directly, or an Administrator editing the field by
    hand) is never overwritten. job_name itself is left exactly as-is -
    this only reads it, it never strips the prefix back out."""
    rows = db.execute(
        "SELECT works_order_id, job_name FROM works_orders WHERE location IS NULL OR location = ''"
    ).fetchall()
    updated = 0
    for row in rows:
        m = _JOB_NAME_LOCATION_PREFIX.match(row["job_name"] or "")
        if m:
            db.execute(
                "UPDATE works_orders SET location=? WHERE works_order_id=?",
                (m.group(1).strip(), row["works_order_id"]),
            )
            updated += 1
    if updated:
        db.commit()
        print(f"Backfilled location on {updated} works order(s) from their job name prefix")


def widen_quote_statuses(db):
    """Quotes gain two statuses, 'On Hold' and 'Cancelled'. SQLite can't change
    a CHECK rule in place, so the quotes table is rebuilt (the documented
    create-copy-drop-rename procedure) - once, only if it still has the old
    rule. Quotes brought in from the Quote Register earlier, which had to be
    filed as Lost / Sent because these statuses didn't exist, are then moved to
    the status the register actually gave them (the import left that in Notes)."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='quotes'").fetchone()
    if not row:
        return
    if "On Hold" not in row[0]:
        new_sql = re.sub(r"CHECK\s*\(\s*status\s+IN\s*\([^)]*\)\s*\)",
                         "CHECK (status IN ('Draft','Sent','Won','Lost','Expired','On Hold','Cancelled'))", row[0], count=1)
        if new_sql == row[0]:
            raise RuntimeError("could not find the status rule on the quotes table")
        new_sql = re.sub(r"CREATE TABLE\s+\"?quotes\"?", "CREATE TABLE quotes__new", new_sql, count=1)
        db.commit()
        db.execute("PRAGMA foreign_keys = OFF")
        try:
            db.execute("BEGIN")
            db.execute(new_sql)
            db.execute("INSERT INTO quotes__new SELECT * FROM quotes")
            db.execute("DROP TABLE quotes")
            db.execute("ALTER TABLE quotes__new RENAME TO quotes")
            db.execute("CREATE INDEX IF NOT EXISTS idx_quotes_customer ON quotes(customer_id)")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
        print("Schema update: quotes can now be 'On Hold' or 'Cancelled'")
    key = "migration_quote_register_statuses_v1"
    if not _migration_applied(db, key):
        db.execute("UPDATE quotes SET status='Cancelled' WHERE status='Lost' AND notes LIKE '%status in register: Cancelled%'")
        db.execute("UPDATE quotes SET status='On Hold' WHERE status='Sent' AND notes LIKE '%status in register: On Hold%'")
        _mark_migration_applied(db, key)


def allow_service_engineer_role(db):
    """Users gain a sixth role, 'Service Engineer'. The role list is a CHECK
    rule on the users table, which SQLite can't change in place, so the table
    is rebuilt (create-copy-drop-rename) - once, only if it lacks the role."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    if not row or "Service Engineer" in row[0]:
        return
    new_sql = re.sub(r"CHECK\s*\(\s*role\s+IN\s*\([^)]*\)\s*\)",
                     "CHECK (role IN ('Administrator','Project Manager','Procurement','Finance','Executive','Service Engineer'))",
                     row[0], count=1)
    if new_sql == row[0]:
        raise RuntimeError("could not find the role rule on the users table")
    new_sql = re.sub(r"CREATE TABLE\s+\"?users\"?", "CREATE TABLE users__new", new_sql, count=1)
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute("BEGIN")
        db.execute(new_sql)
        db.execute("INSERT INTO users__new SELECT * FROM users")
        db.execute("DROP TABLE users")
        db.execute("ALTER TABLE users__new RENAME TO users")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
    print("Schema update: users can now have the 'Service Engineer' role")


def relax_po_job_link(db):
    """Historic purchase orders imported from the old register don't belong to
    a job in the app, so purchase_orders.works_order_id must be allowed to be
    empty (it used to be mandatory, which is why those POs were parked on made-up
    HIST-PO-... jobs). SQLite can't relax a NOT NULL in place, so the table is
    rebuilt once - the same create-copy-drop-rename procedure used for quotes -
    only if it still has the old rule. Every PO, line and link is kept."""
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_orders'").fetchone()
    if not row:
        return
    cols = [r[1] for r in db.execute("PRAGMA table_info(purchase_orders)")]
    if "register_job_ref" not in cols:
        db.execute("ALTER TABLE purchase_orders ADD COLUMN register_job_ref TEXT")
        db.commit()
        cols.append("register_job_ref")
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_orders'").fetchone()
    info = {r[1]: r for r in db.execute("PRAGMA table_info(purchase_orders)")}
    if not info["works_order_id"][3]:          # notnull flag already off
        return
    new_sql = re.sub(r"(works_order_id\s+INTEGER)\s+NOT NULL", r"\1", row[0], count=1)
    if new_sql == row[0]:
        raise RuntimeError("could not find the works_order_id rule on the purchase_orders table")
    new_sql = re.sub(r"CREATE TABLE\s+\"?purchase_orders\"?", "CREATE TABLE purchase_orders__new", new_sql, count=1)
    col_list = ", ".join(cols)
    db.commit()
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        db.execute("BEGIN")
        db.execute(new_sql)
        db.execute(f"INSERT INTO purchase_orders__new ({col_list}) SELECT {col_list} FROM purchase_orders")
        db.execute("DROP TABLE purchase_orders")
        db.execute("ALTER TABLE purchase_orders__new RENAME TO purchase_orders")
        db.execute("CREATE INDEX IF NOT EXISTS idx_po_wo ON purchase_orders(works_order_id)")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys = ON")
    print("Schema update: historic purchase orders no longer need a job")


def tidy_historic_pos_once(db, base_dir):
    """One-time automatic clean-up of imported historic purchase orders (the same work
    as tidy_historic_pos.py): detaches them from the made-up HIST-PO jobs, strips
    register clutter, and removes the blank pre-numbered rows. A timestamped backup is
    taken first, and it only ever runs once per database."""
    key = "migration_tidy_historic_pos_v2"
    if _migration_applied(db, key):
        return
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='purchase_orders'").fetchone():
        return
    import sys
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    import sqlite3
    import tidy_historic_pos
    old_factory = db.row_factory
    db.row_factory = sqlite3.Row
    try:
        if tidy_historic_pos.needs_tidy(db):
            try:
                import db_location
                db_location.backup_db()
            except Exception:
                pass
            db.commit()
            db.execute("BEGIN")
            try:
                result = tidy_historic_pos.tidy(db)
                db.commit()
            except Exception:
                db.rollback()
                raise
            print(f"Tidied historic purchase orders: {len(result['rows'])} detached from stand-in jobs, "
                  f"{len(result['blanks'])} blank rows removed, {result['cleaned']} descriptions cleaned")
        _mark_migration_applied(db, key)
    finally:
        db.row_factory = old_factory


def run_migrations(db, base_dir):
    """Every individual migration step is wrapped so one failing can never
    stop the others from running, or stop the app from starting - the
    whole point of running these automatically on every startup (see the
    module docstring) is defeated if a single bad step blocks startup
    entirely. Add future migrations here the same way: their own
    try/except, a clear one-line message on failure, never a bare call."""
    try:
        ensure_schema_current(db)
    except Exception as e:
        print(f"Skipped a schema update: {e}. Everything else is starting normally.")
    try:
        widen_quote_statuses(db)
    except Exception as e:
        print(f"Skipped the quote status update: {e}. Everything else is starting normally.")
    try:
        allow_service_engineer_role(db)
    except Exception as e:
        print(f"Skipped the Service Engineer role update: {e}. Everything else is starting normally.")
    try:
        relax_po_job_link(db)
    except Exception as e:
        print(f"Skipped the purchase order update: {e}. Everything else is starting normally.")
    try:
        tidy_historic_pos_once(db, base_dir)
    except Exception as e:
        print(f"Skipped the historic purchase order tidy-up: {e}. Everything else is starting normally.")
    migrate_supplier_list(db, base_dir)  # already wraps its own errors internally
    try:
        seed_fixed_price_contract_codes(db)
    except Exception as e:
        print(f"Skipped adding fixed-price contract codes: {e}. Everything else is starting normally.")
    try:
        backfill_works_order_locations(db)
    except Exception as e:
        print(f"Skipped location backfill: {e}. Everything else is starting normally.")
