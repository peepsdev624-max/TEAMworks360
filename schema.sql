-- TEAMworks360 (formerly Job Profitability Manager) - Database Schema

DROP TABLE IF EXISTS customer_invoices;
DROP TABLE IF EXISTS labour_entries;
DROP TABLE IF EXISTS supplier_invoices;
DROP TABLE IF EXISTS goods_in;
DROP TABLE IF EXISTS despatch_note_lines;
DROP TABLE IF EXISTS despatch_notes;
DROP TABLE IF EXISTS purchase_order_lines;
DROP TABLE IF EXISTS purchase_orders;
DROP TABLE IF EXISTS ship_to_addresses;
DROP TABLE IF EXISTS stock_movements;
DROP TABLE IF EXISTS stock_allocations;
DROP TABLE IF EXISTS stock_items;
DROP TABLE IF EXISTS works_orders;
DROP TABLE IF EXISTS suppliers;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS quote_labour_lines;
DROP TABLE IF EXISTS quote_purchase_lines;
DROP TABLE IF EXISTS quotes;
DROP TABLE IF EXISTS rate_card;
DROP TABLE IF EXISTS contract_code_targets;
DROP TABLE IF EXISTS settings;
DROP TABLE IF EXISTS totp_backup_codes;
DROP TABLE IF EXISTS document_access_log;
DROP TABLE IF EXISTS alert_acceptances;

CREATE TABLE users (
    user_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT UNIQUE NOT NULL,
    password_hash  TEXT NOT NULL,
    full_name      TEXT NOT NULL,
    role           TEXT NOT NULL CHECK (role IN ('Administrator','Project Manager','Procurement','Finance','Executive','Service Engineer')),
    email          TEXT,
    created_date   TEXT DEFAULT (datetime('now')),
    must_change_password INTEGER NOT NULL DEFAULT 0,
    -- Two-factor authentication (TOTP, RFC 6238) - mandatory for every
    -- account. totp_secret is set the first time someone starts setup
    -- (see get_2fa_setup() in app.py) but totp_enabled only flips to 1
    -- once they've proven their authenticator app actually produces a
    -- matching code, so a mis-scanned/abandoned setup can never lock
    -- someone out - they just get the same QR code again next time.
    totp_secret    TEXT,
    totp_enabled   INTEGER NOT NULL DEFAULT 0
);

-- One-time recovery codes for signing in if a user loses access to their
-- authenticator app - issued 10 at a time the moment 2FA setup is
-- confirmed (see generate_backup_codes() in app.py), each usable exactly
-- once. Only the hash is ever stored, the same as password_hash above -
-- even a full copy of the database can't be used to sign in with them.
CREATE TABLE totp_backup_codes (
    backup_code_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    code_hash       TEXT NOT NULL,
    created_date    TEXT DEFAULT (datetime('now')),
    used_date       TEXT
);
CREATE INDEX idx_totp_backup_codes_user ON totp_backup_codes(user_id);

CREATE TABLE customers (
    customer_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name  TEXT NOT NULL,
    address        TEXT,
    contact_name   TEXT,
    email          TEXT,
    phone          TEXT,
    created_by     TEXT,
    created_date   TEXT DEFAULT (datetime('now')),
    updated_by     TEXT,
    updated_date   TEXT
);

CREATE TABLE suppliers (
    supplier_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_name  TEXT NOT NULL,
    supply_of        TEXT,
    reason_for_use    TEXT,
    authorised_by      TEXT,
    address        TEXT,
    contact_name   TEXT,
    email          TEXT,
    vat_number     TEXT,
    created_by     TEXT,
    created_date   TEXT DEFAULT (datetime('now')),
    updated_by     TEXT,
    updated_date   TEXT
);

CREATE TABLE works_orders (
    works_order_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_number       TEXT UNIQUE NOT NULL,
    job_name         TEXT NOT NULL,
    customer_id      INTEGER NOT NULL REFERENCES customers(customer_id),
    project_manager  TEXT,
    status           TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft','Open','In Progress','Completed','Closed','Cancelled')),
    start_date       TEXT,
    end_date         TEXT,
    contract_value   REAL NOT NULL DEFAULT 0,
    budget_cost      REAL NOT NULL DEFAULT 0,
    budget_is_estimated  INTEGER NOT NULL DEFAULT 0,
    contract_code    TEXT,
    location         TEXT,
    sharepoint_url   TEXT,
    created_by       TEXT,
    created_date     TEXT DEFAULT (datetime('now')),
    updated_by       TEXT,
    updated_date     TEXT
);

-- "Deliver to" addresses used on Purchase Orders, taken from the business's
-- PO master template(s) - starts with Derby, more sites get added from the
-- Settings screen as the business provides them. company_name/registered_no
-- are stored per-address since a future site could in principle be a
-- different registered entity, though today they'll usually match.
CREATE TABLE ship_to_addresses (
    ship_to_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT UNIQUE NOT NULL,
    company_name     TEXT,
    address_line1    TEXT,
    address_line2    TEXT,
    city             TEXT,
    postcode         TEXT,
    created_date     TEXT DEFAULT (datetime('now'))
);

CREATE TABLE purchase_orders (
    purchase_order_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    po_number            TEXT UNIQUE NOT NULL,
    works_order_id        INTEGER REFERENCES works_orders(works_order_id),   -- NULL only for historic POs imported from the old register
    supplier_id           INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    description           TEXT,
    order_date             TEXT,
    po_value               REAL NOT NULL DEFAULT 0,
    status                 TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft','Submitted','Approved','Issued','Completed','Cancelled')),
    sharepoint_url         TEXT,
    ship_to_id             INTEGER REFERENCES ship_to_addresses(ship_to_id),
    date_required          TEXT,
    account_number         TEXT,
    raised_by              TEXT,
    vat_rate               REAL NOT NULL DEFAULT 20,
    is_historic            INTEGER NOT NULL DEFAULT 0,
    register_job_ref       TEXT                                                  -- the old register's job reference, text only (historic POs)
);

-- Line items for the printable PDF (matching the business's real PO master
-- template - Qty/Part No./Item #/Description/Unit Price/Line Total). A PO
-- with no lines still generates a valid PDF - see po_pdf.py, which falls
-- back to a single line built from the PO's own Description and po_value.
-- When lines exist, po_value is kept as their sum (the subtotal, ex-VAT)
-- rather than a second, independently-editable figure that could drift
-- out of sync with what the lines actually add up to.
CREATE TABLE purchase_order_lines (
    line_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_order_id     INTEGER NOT NULL REFERENCES purchase_orders(purchase_order_id) ON DELETE CASCADE,
    qty                    REAL NOT NULL DEFAULT 1,
    part_number             TEXT,
    item_number               TEXT,
    description                 TEXT,
    unit_price                   REAL NOT NULL DEFAULT 0
);

CREATE INDEX idx_po_lines_po ON purchase_order_lines(purchase_order_id);

-- Goods In: acknowledgement that a delivery against a Purchase Order has
-- physically arrived. Deliberately open to every role, not gated to
-- Procurement like the rest of Purchase Orders - whoever's actually there
-- to sign for a delivery needs to be able to log it, not just whoever
-- normally raises the PO. Destination mirrors the same choice Inventory's
-- Move Stock already offers (a Works Order or a Site) but this table
-- doesn't itself move any stock or touch cost - it's a standalone
-- acknowledgement/audit record, kept deliberately separate so it can
-- never double up with a real stock allocation someone does separately.
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
);

CREATE INDEX idx_goods_in_po ON goods_in(purchase_order_id);

-- Every Goods In record must carry at least one uploaded document (a
-- delivery note, packing slip, or photo of the goods/paperwork) as proof
-- of what was actually received - enforced in the API, not here, since
-- SQLite can't express "at least one child row must exist" as a
-- constraint. Kept as its own table rather than a single file column on
-- goods_in so more than one document (e.g. a delivery note plus a photo)
-- can be attached, and so a document can be added later without
-- disturbing the goods_in row itself.
CREATE TABLE goods_in_documents (
    document_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    goods_in_id           INTEGER NOT NULL REFERENCES goods_in(goods_in_id) ON DELETE CASCADE,
    original_filename       TEXT NOT NULL,
    stored_filename           TEXT NOT NULL UNIQUE,
    content_type                 TEXT,
    file_size                       INTEGER,
    uploaded_by                         TEXT NOT NULL,
    uploaded_date                           TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_goods_in_documents_gi ON goods_in_documents(goods_in_id);

-- Despatch Notes: the outbound counterpart to Goods In - records goods
-- physically shipped to the customer against a Works Order, with its own
-- itemised lines (Qty/Part No./Description) for the printable PDF, built
-- on the same pdf_common.py branding as Quotes and Purchase Orders.
-- despatch_number is auto-assigned sequentially (TMD-4000, TMD-4001, ...)
-- the same way quote_number is - see get_next_despatch_number() in app.py.
-- delivery_address defaults from the linked customer's address but is a
-- plain editable field, not a foreign key, since a one-off delivery to a
-- different site shouldn't require a new Customer or Ship To record.
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
    updated_date          TEXT,
    -- Customer's on-screen signature, captured on a phone/tablet at the
    -- point of delivery (see /api/despatch-notes/<id>/signature in
    -- app.py) - a touch-friendly alternative to the old "print name and
    -- sign on the printed copy" approach, embedded into the PDF in place
    -- of the blank boxes once present. signature_filename stays NULL
    -- until signed; the other three columns are only meaningful once it
    -- isn't.
    signature_filename     TEXT UNIQUE,
    signature_name            TEXT,
    signature_date               TEXT,
    signature_captured_by           TEXT
);

CREATE TABLE despatch_note_lines (
    line_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    despatch_note_id     INTEGER NOT NULL REFERENCES despatch_notes(despatch_note_id) ON DELETE CASCADE,
    qty                     REAL NOT NULL DEFAULT 1,
    part_number               TEXT,
    description                 TEXT
);

CREATE INDEX idx_despatch_wo ON despatch_notes(works_order_id);
CREATE INDEX idx_despatch_lines_dn ON despatch_note_lines(despatch_note_id);

CREATE TABLE supplier_invoices (
    supplier_invoice_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number         TEXT NOT NULL,
    purchase_order_id       INTEGER NOT NULL REFERENCES purchase_orders(purchase_order_id),
    works_order_id           INTEGER NOT NULL REFERENCES works_orders(works_order_id),
    supplier_id              INTEGER NOT NULL REFERENCES suppliers(supplier_id),
    invoice_date              TEXT,
    invoice_amount            REAL NOT NULL DEFAULT 0,
    status                    TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Approved','Paid','Disputed','Cancelled')),
    created_by                TEXT,
    created_date               TEXT DEFAULT (datetime('now')),
    updated_by                  TEXT,
    updated_date                 TEXT
);

CREATE TABLE labour_entries (
    labour_entry_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    works_order_id     INTEGER NOT NULL REFERENCES works_orders(works_order_id),
    employee_name       TEXT NOT NULL,
    work_date            TEXT,
    hours_worked         REAL NOT NULL DEFAULT 0,
    hourly_rate           REAL NOT NULL DEFAULT 0,
    labour_cost           REAL NOT NULL DEFAULT 0,
    created_by             TEXT,
    created_date             TEXT DEFAULT (datetime('now')),
    updated_by                TEXT,
    updated_date                TEXT
);

CREATE TABLE customer_invoices (
    customer_invoice_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    works_order_id         INTEGER NOT NULL REFERENCES works_orders(works_order_id),
    invoice_number           TEXT NOT NULL,
    invoice_date              TEXT,
    invoice_amount            REAL NOT NULL DEFAULT 0,
    status                    TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft','Sent','Paid','Overdue','Cancelled')),
    created_by                TEXT,
    created_date               TEXT DEFAULT (datetime('now')),
    updated_by                  TEXT,
    updated_date                 TEXT
);

-- Quotes: pre-Works-Order stage. quote_number is auto-assigned sequentially
-- (TMQ-3000, TMQ-3001, ...) rather than typed in - see get_next_quote_number()
-- in app.py. In full deployment this is the point that would call the
-- SharePoint API to create the next numbered folder instead of just
-- incrementing a number locally; the same approach is intended to extend to
-- Works Order and Purchase Order numbering later.
--
-- works_order_ref (free text) vs works_order_id (a real link, below) serve
-- different purposes and both stay: works_order_ref is whatever reference
-- the quote PDF should print - often "TBC" or a customer's own reference,
-- since a quote is usually written before a Works Order exists at all -
-- while works_order_id is an optional link to an ACTUAL Works Order
-- already in the system, set once a quote's real job exists, purely so
-- quoted hours can be compared against what's actually been booked (see
-- the Hours Quoted & Booked page).
CREATE TABLE quotes (
    quote_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_number      TEXT UNIQUE NOT NULL,
    customer_id       INTEGER REFERENCES customers(customer_id),
    description       TEXT,
    owner             TEXT,
    status            TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft','Sent','Won','Lost','Expired','On Hold','Cancelled')),
    estimated_value   REAL NOT NULL DEFAULT 0,
    quote_date        TEXT,
    valid_until       TEXT,
    notes             TEXT,
    sharepoint_url    TEXT,
    contact_name      TEXT,
    your_ref          TEXT,
    works_order_ref   TEXT,
    works_order_id    INTEGER REFERENCES works_orders(works_order_id),
    scope_items       TEXT,
    delivery_terms    TEXT,
    created_by        TEXT,
    created_date      TEXT DEFAULT (datetime('now')),
    updated_by        TEXT,
    updated_date      TEXT
);

-- Cost buildup captured by the New Quote wizard - one row per rate-card
-- activity the user entered hours against. Rate is snapshotted at entry
-- time (copied from rate_card, not a live join) so a later rate card
-- change never silently rewrites the cost of a quote already raised.
CREATE TABLE quote_labour_lines (
    line_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id         INTEGER NOT NULL REFERENCES quotes(quote_id) ON DELETE CASCADE,
    category         TEXT,
    activity         TEXT,
    standard_rate    REAL NOT NULL DEFAULT 0,
    ot_rate          REAL NOT NULL DEFAULT 0,
    standard_hours   REAL NOT NULL DEFAULT 0,
    ot_hours         REAL NOT NULL DEFAULT 0
);

-- Purchases / brought-out costs captured by the wizard - mirrors the
-- "Purchases - Services & Brought Out Costs" section of the source
-- workbook. Free-form rows, not tied to a fixed catalogue like labour.
CREATE TABLE quote_purchase_lines (
    line_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id           INTEGER NOT NULL REFERENCES quotes(quote_id) ON DELETE CASCADE,
    invoice_no          TEXT,
    line_date            TEXT,
    supplier             TEXT,
    description           TEXT,
    quantity              REAL DEFAULT 0,
    purchase_amount       REAL DEFAULT 0,
    sales_value           REAL DEFAULT 0
);

CREATE INDEX idx_po_wo ON purchase_orders(works_order_id);
CREATE INDEX idx_si_wo ON supplier_invoices(works_order_id);
CREATE INDEX idx_si_po ON supplier_invoices(purchase_order_id);
CREATE INDEX idx_le_wo ON labour_entries(works_order_id);
CREATE INDEX idx_ci_wo ON customer_invoices(works_order_id);
CREATE INDEX idx_quotes_customer ON quotes(customer_id);
CREATE INDEX idx_qll_quote ON quote_labour_lines(quote_id);
CREATE INDEX idx_qpl_quote ON quote_purchase_lines(quote_id);

-- App-wide key/value settings (currently just the default margin target;
-- room to grow without a schema change for future simple settings).
CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Per-contract-code margin target overrides, managed from the Settings
-- screen. A code with no row here uses the 'default_margin_target' value
-- from the settings table above.
CREATE TABLE contract_code_targets (
    contract_code  TEXT PRIMARY KEY,
    target_pct     REAL NOT NULL,
    standard_value REAL,
    standard_cost  REAL
);

INSERT INTO settings (key, value) VALUES ('default_margin_target', '35');
INSERT INTO contract_code_targets (contract_code, target_pct) VALUES ('INT', 10.7);
-- Fixed-price tiers: Purchase Order Value and Budget Cost auto-populate
-- on the Works Order form the moment one of these is picked as Contract
-- Code, since both figures are standard for that tier - see
-- openWorksOrderForm() in app.js. All four are priced at exactly 20%
-- margin by construction (cost = 80% of value), which is also why the
-- target is set to 20% here rather than left at the 35% default.
INSERT INTO contract_code_targets (contract_code, target_pct, standard_value, standard_cost) VALUES
    ('FS - L', 20, 179.79, 143.83),
    ('FS - M', 20, 309.12, 247.29),
    ('FS - H', 20, 579.60, 463.68),
    ('FS - HQ', 20, 450.00, 360.00);

-- Seeded from the business's own PO master templates - see data/ for the
-- original files. Add further sites the same way (INSERT here for future
-- fresh installs, or from the Settings screen for an already-running app).
INSERT INTO ship_to_addresses (label, company_name, address_line1, address_line2, city, postcode)
VALUES ('Derby', 'Total Engineering Asset Management Limited', 'Unit 10, Enterprise Way', NULL, 'Derby', 'DE21 4BB');
INSERT INTO ship_to_addresses (label, company_name, address_line1, address_line2, city, postcode)
VALUES ('Phoenix', 'Total Engineering Asset Management Limited', 'Unit 5 Phoenix Road', 'Crowther Industrial Est', 'Washington', 'NE38 0AD');

-- Rate card: the fixed activity/hourly-rate list from the business's own
-- Master_Repair_Quote_Cost_Tracker.xlsx, used by the New Quote wizard's
-- labour cost buildup step. Only "Conventional Machining" and "CNC
-- Machining" were explicit category groupings in that source workbook;
-- the other category labels here (Design & Engineering, Fitting &
-- Fabrication, Inspection & Testing, Other) are a reasonable grouping of
-- the remaining activities by type, added for a usable wizard layout - not
-- something the source spreadsheet itself labelled.
CREATE TABLE rate_card (
    rate_card_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    category       TEXT NOT NULL,
    activity       TEXT NOT NULL,
    standard_rate  REAL NOT NULL DEFAULT 0,
    ot_rate        REAL NOT NULL DEFAULT 0,
    sort_order     INTEGER NOT NULL DEFAULT 0
);

INSERT INTO rate_card (category, activity, standard_rate, ot_rate, sort_order) VALUES
('Design & Engineering', 'Concept Design', 35, 52.5, 1),
('Design & Engineering', 'Detail Design', 30, 45, 2),
('Design & Engineering', 'FEMA Analysis', 30, 45, 3),
('Design & Engineering', 'Project Management', 26, 39, 4),
('Conventional Machining', 'Milling', 33, 49.5, 5),
('Conventional Machining', 'Grinding', 33, 49.5, 6),
('Conventional Machining', 'Turning', 33, 49.5, 7),
('Conventional Machining', 'Drilling', 33, 49.5, 8),
('Conventional Machining', 'Boring', 33, 49.5, 9),
('CNC Machining', '5 Axis Milling', 47.5, 71.25, 10),
('CNC Machining', 'NC Programming', 26, 39, 11),
('CNC Machining', 'EDM', 35.5, 53.25, 12),
('CNC Machining', 'Grinding', 49.5, 74.25, 13),
('CNC Machining', 'Turning', 45, 67.5, 14),
('CNC Machining', '5 Axis Drilling', 48.5, 72.75, 15),
('CNC Machining', 'Boring', 48.5, 72.75, 16),
('Fitting & Fabrication', 'Fitting / Assembly', 31, 46.5, 17),
('Fitting & Fabrication', 'Fabrication', 31, 46.5, 18),
('Inspection & Testing', 'Manual Inspection', 33, 49.5, 19),
('Inspection & Testing', 'Inspection', 35.5, 53.25, 20),
('Inspection & Testing', 'CMM', 35.5, 53.25, 21),
('Inspection & Testing', 'Testing', 26, 39, 22),
('Other', 'Team Transport', 20, 40, 23),
('Other', 'External', 45, 67.5, 24);

-- Inventory: a stock item catalogue, with each unit's whereabouts tracked
-- as an allocation to exactly one destination - a Ship To site (general
-- stock sitting at that location, not yet earmarked for a job) or a
-- Works Order (reserved/issued for that specific job). A stock item's
-- total quantity is the sum of its allocation rows, not a separate field,
-- so "how much do we have" and "where is it" can never drift apart.
CREATE TABLE stock_items (
    stock_item_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number      TEXT UNIQUE NOT NULL,
    description       TEXT,
    unit              TEXT NOT NULL DEFAULT 'each',
    purchase_cost     REAL NOT NULL DEFAULT 0,
    sale_price        REAL NOT NULL DEFAULT 0,
    reorder_level     REAL NOT NULL DEFAULT 0,
    supplier_id       INTEGER REFERENCES suppliers(supplier_id),
    notes             TEXT,
    created_date      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE stock_allocations (
    allocation_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_item_id     INTEGER NOT NULL REFERENCES stock_items(stock_item_id) ON DELETE CASCADE,
    quantity           REAL NOT NULL DEFAULT 0,
    ship_to_id          INTEGER REFERENCES ship_to_addresses(ship_to_id),
    works_order_id       INTEGER REFERENCES works_orders(works_order_id),
    notes                 TEXT,
    allocated_date         TEXT DEFAULT (datetime('now')),
    CHECK (
        (ship_to_id IS NOT NULL AND works_order_id IS NULL) OR
        (ship_to_id IS NULL AND works_order_id IS NOT NULL)
    )
);

CREATE INDEX idx_stock_alloc_item ON stock_allocations(stock_item_id);
CREATE INDEX idx_stock_alloc_shipto ON stock_allocations(ship_to_id);
CREATE INDEX idx_stock_alloc_wo ON stock_allocations(works_order_id);

-- Append-only audit log of every stock movement - a receipt (from = NULL,
-- brand new stock arriving), an issue (from a Site to a Works Order), a
-- transfer (Site to Site, or Works Order to Works Order), or any other
-- from/to combination. This is the record of what happened; the current
-- "where is it now" state still lives in stock_allocations, which each
-- movement updates as it's recorded - see move_stock() in app.py, the one
-- place both are ever written together.
CREATE TABLE stock_movements (
    movement_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_item_id        INTEGER NOT NULL REFERENCES stock_items(stock_item_id) ON DELETE CASCADE,
    quantity              REAL NOT NULL,
    from_ship_to_id        INTEGER REFERENCES ship_to_addresses(ship_to_id),
    from_works_order_id     INTEGER REFERENCES works_orders(works_order_id),
    to_ship_to_id             INTEGER REFERENCES ship_to_addresses(ship_to_id),
    to_works_order_id          INTEGER REFERENCES works_orders(works_order_id),
    moved_by                     TEXT,
    notes                         TEXT,
    movement_date                 TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_stock_move_item ON stock_movements(stock_item_id);

-- Append-only history of edits to the financially significant fields on
-- the records that drive profitability and invoicing (status and the
-- money/hours fields on works orders, quotes, purchase orders, and
-- customer/supplier invoices and labour entries). The row itself only
-- ever shows the latest value - this is what lets a disputed figure be
-- traced back to what it used to be, who changed it, and when. Written
-- by log_field_changes() in app.py, called from each of those records'
-- update endpoints; nothing reads old values back out via the API yet
-- except the Audit History panel on each record's edit form.
CREATE TABLE audit_log (
    audit_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name    TEXT NOT NULL,
    record_id     INTEGER NOT NULL,
    field_name    TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_by    TEXT,
    changed_date  TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_audit_log_record ON audit_log(table_name, record_id);

-- Durable record of every login attempt (success, failure, or lockout),
-- independent of the in-memory _LOGIN_ATTEMPTS dict in app.py (which only
-- ever drives the login throttling itself and resets on every restart).
-- Written by log_login_event(), called from the /api/auth/login route.
-- Read-only from the UI's point of view, via the admin-only
-- /api/login-events endpoint (Users page, "Login Activity"); never pruned
-- automatically.
CREATE TABLE login_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL,
    ip_address    TEXT,
    event_type    TEXT NOT NULL CHECK (event_type IN ('success','failure','lockout')),
    event_date    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_login_events_date ON login_events(event_date);

-- Durable record of every PDF download and CSV export (who, what document,
-- when) - distinct from login_events (who got in) and audit_log (what
-- field values changed). Written by log_document_access(), called from
-- every document-download/export route. Read-only from the UI's point of
-- view, via the admin-only /api/document-access-log endpoint (Users page,
-- "Document Access"); never pruned automatically.
CREATE TABLE document_access_log (
    access_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL,
    document_type  TEXT NOT NULL,
    reference      TEXT,
    ip_address     TEXT,
    access_date    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX idx_document_access_log_date ON document_access_log(access_date);

-- Alerts someone has looked at and accepted ("known, fine as it is"). Alerts
-- themselves are worked out live; this only remembers the acceptance. The key
-- includes the alert's wording (which carries the figures), so if the job's
-- numbers change the alert is a new one and shows again.
CREATE TABLE alert_acceptances (
    acceptance_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_key      TEXT NOT NULL UNIQUE,
    job_number     TEXT,
    alert_type     TEXT,
    message        TEXT,
    reason         TEXT NOT NULL,
    accepted_by    TEXT,
    accepted_date  TEXT DEFAULT (datetime('now'))
);
