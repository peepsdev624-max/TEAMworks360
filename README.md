# TEAMworks360

*(formerly Job Profitability Manager — renamed as the app grew from a profitability
calculator into the full quote-to-invoice system it is today; see "Deploying updates
safely" below for what the rename means for an existing install.)*

A working web application for tracking Works Orders (Jobs), Purchase Orders, Labour Costs,
Supplier Invoices and Customer Invoices, with real-time job profitability and margin
calculations. Built for construction, engineering, manufacturing, facilities management
and service-based project businesses.

**This copy is loaded with real data** imported from `1__Team_Order_Book_1st_Jan_-_31st_Dec_2026.xlsm`
(1,735 jobs, Jan 2025–2026) — see "Real-data import" below for how it was mapped in, and
how to re-run the import after you export an updated Order Book.

## Tech stack

- **Backend:** Python 3 + Flask (REST API architecture)
- **Database:** SQLite (relational, file-based — `jpm.db`). The schema (`schema.sql`) is
  standard SQL and will port to PostgreSQL/MySQL with only minor type tweaks if you outgrow SQLite.
- **Frontend:** Vanilla HTML/CSS/JavaScript single-page app (no build step required), charts via Chart.js
- **Auth:** Session-based login with 5 role-based accounts

## Quick start

**Easiest way:** double-click `start_mac_linux.command` (Mac/Linux) or `start_windows.bat`
(Windows) in this folder. First run installs dependencies and sets up the database
automatically; every run after that skips straight to launching, and your browser opens
by itself. (First time on a Mac, right-click → Open instead of double-clicking, to get
past the "unidentified developer" warning — a one-time step for any downloaded script.)

**A proper app icon to launch from** is also included, so you don't need to hunt for the
right file in this folder every time — see "App icon / launcher" below.

**Multiple people who need to share the same data** should use
`start_shared_host.bat` / `start_shared_host.command` instead — see "Shared hosting"
below. The plain launchers above are single-user: each person who runs one gets their
own private database, even from a shared drive.

**Want to try something out, or let a few people test, without touching real data?**
Use `start_test_shared_host.bat` / `start_test_shared_host.command` — a completely
separate, isolated copy with its own database, seeded once from a real snapshot and
then fully independent. See "Test copy (sandbox)" below.

**Manual way**, if you'd rather run it yourself from a terminal:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2a. To load your real Order Book data (already done for the database this ships with):
python import_team_order_book.py                      # uses data/Team_Order_Book.xlsm
python import_team_order_book.py path/to/updated.xlsm  # or point at a fresh export

# 2b. To reset to small demo/sample data instead:
python seed.py

# 3. Run the app
python app.py

# 4. Open your browser
http://127.0.0.1:5000
```

Set `NO_BROWSER=1` before either the script or `python app.py` if you don't want the
browser tab opened automatically.

## App icon / launcher

The app can be started from a proper icon carrying TEAMworks360's own mark, rather than
always digging into this folder for `start_windows.bat` / `start_mac_linux.command` — the
icon itself lives in `app_icon/` (`icon.ico`, `icon.icns`, `icon.png`, plus the
`icon_master_1024.png` they're all generated from). It's the same red/blue ring-and-"360"
mark used in the app's own sidebar (`static/img/teamworks360-mark.svg`), given a filled
navy background so it reads correctly as a standalone icon rather than the sidebar's
transparent version.

- **Windows** — the first time `start_windows.bat` runs, it automatically creates a
  **"TEAMworks360"** shortcut on the Desktop with the app's own icon, via a
  small PowerShell helper (`create_shortcut.ps1`) run once and never again (it checks
  whether the shortcut already exists first). That shortcut still runs the same
  `start_windows.bat` underneath — same dependency check, same database setup — it's
  purely a nicer way to launch it. The Desktop location comes from
  `[Environment]::GetFolderPath("Desktop")` rather than the more obvious-looking
  `%USERPROFILE%\Desktop`, since the latter is wrong on any machine where the Desktop
  folder has been redirected elsewhere (common with IT-managed OneDrive Known Folder
  Move). If shortcut creation fails for any reason, the app starts normally regardless —
  you just won't see the "created a shortcut" message — and a `shortcut_creation.log`
  file appears in this folder with the real reason, so a failure here is something that
  can actually be diagnosed rather than a silent no-op.
- **Mac** — **`TEAMworks360.app`** in this folder is a real, double-clickable
  app bundle with the app's own icon. It doesn't duplicate any startup logic — its one job is
  to open Terminal and run the existing `start_mac_linux.command`, so dependency
  installs, database setup, and everything else behaves exactly as it always has. Drag it
  to the Dock or Applications folder like any other Mac app if you'd like it to live
  there permanently; it'll still find this folder correctly wherever it's launched from,
  since it locates its own bundle path at runtime rather than assuming a fixed location.
  (Same one-time "right-click → Open" step as the plain `.command` file applies here too,
  the first time — macOS treats any downloaded, unsigned executable this way.)
- **Linux** — **`TEAMworks360.desktop`** provides the same, though desktop
  entry behaviour varies more across distributions/file managers than the other two
  platforms, so treat this as best-effort: some file managers need it marked as trusted
  ("Allow Launching") before first use, and if the icon doesn't appear, editing the
  `Icon=` line in the file to the full path of `app_icon/icon.png` resolves it.

## Demo accounts

| Username      | Password   | Role            |
|---------------|-----------|------------------|
| `admin`       | admin123  | Administrator    |
| `pmanager`    | pm123     | Project Manager  |
| `procurement` | proc123   | Procurement      |
| `finance`     | fin123    | Finance          |
| `exec`        | exec123   | Executive        |

Change these passwords before using this anywhere beyond local evaluation (see "Security"
below for the secret key, which is generated automatically, not something to edit by hand).
`import_team_order_book.py` does not touch the `users` table, so your logins survive
re-imports.

Two-factor authentication is mandatory for every account, these demo ones included — the
first time you sign into any of them, you'll be walked through 2FA setup before you can do
anything else. See "Two-factor authentication" under "Security" below.

## Real-data import

`import_team_order_book.py` reads the "Data" sheet of the Order Book (headers on row 3,
data from row 4), wipes Customers/Suppliers/Works Orders/Purchase Orders/Purchase Order
Lines/Supplier Invoices/Labour Entries/Customer Invoices, and rebuilds them from the
spreadsheet. Of 4,485 rows in that sheet, 1,735 contain real order data — the rest are
blank template rows auto-numbered by the workbook's formulas and are skipped.

**This is a different thing from installing an app update, and only this — never a
plain code update — resets any of the tables above.** Re-running this script is a
deliberate, manual refresh from a new Order Book export; it's not part of a normal
update, and nothing about installing a new version of the app ever triggers it
automatically. **Users, Quotes, and Inventory are never wiped by it either way** —
Quotes are a sales record this import doesn't own, and Inventory is tracked separately
from anything in the Order Book.

**A Quote linked to a Works Order (see "Quoted vs Booked hours" below) survives a
re-import correctly, not just by luck.** Works Orders get fully rebuilt with new IDs on
every run, which would otherwise leave a linked Quote pointing at whatever job happens to
land on its old numeric ID — silently the *wrong* job, not just a missing one. Before the
wipe, every linked Quote's job is captured by its stable `job_number`; after the rebuild,
each one is re-linked to that same job under its new ID, or has the link cleared (never
left pointing at the wrong job) if that job no longer appears in the new import. Purchase
Order Line items are simpler: they're wiped alongside their parent Purchase Order, since
a line item is meaningless without it — this doesn't happen automatically via the
database's own cascade behaviour, because foreign key enforcement is deliberately off
during this wipe (see the code comment on the `PRAGMA foreign_keys = OFF` line for why),
so it's handled explicitly instead.

**Mapping used, and why:**
- One **Works Order** per row. `job_number` = TM No (already unique in the source),
  `contract_value` = Invoice Value, `budget_cost` = **35% of Invoice Value** for every
  job — the business's own explicit target, applied uniformly. (Earlier versions of this
  importer used the source spreadsheet's "TCP" column where present, falling back to 70%
  of Invoice Value otherwise — but TCP turned out to itself just be an automatic
  ~70%-of-invoice-value formula in most rows, not a figure anyone had deliberately set,
  so there was never really a "confirmed" budget behind it. The flat 35% rule replaced
  that entirely.)
- **Customers** = the 8 distinct values found in "End Customer" (dominated by Rolls Royce
  at 1,684 of 1,735 jobs). **Suppliers** = the values in "Supplier 1"/"Supplier 2",
  lightly deduplicated for obvious typos/variants (e.g. "Roemhled" → "Roemheld").
- **Cost split** (so Actual Cost reconciles with the spreadsheet's own "Cost" column):
  - Named supplier + a "Value of Brought Out" figure → a Purchase Order + Supplier
    Invoice for that amount.
  - Named supplier with no separate bought-out figure → the *entire* job cost is booked
    to that supplier (assumed fully subcontracted).
  - No named supplier → the job cost becomes one Labour Entry against the job's "Owner",
    at an assumed **£35/hr** (the source sheet has no per-job timesheet data — only a
    lump cost figure — so hours are back-calculated from that rate rather than invented).
- **Revenue**: one Customer Invoice per job for the full Invoice Value, so Revenue and
  Profit reconcile with the spreadsheet's own Profit = Invoice Value − Cost, whether or
  not that job has actually been invoiced yet (their sheet computes Profit the same way
  for Active and Delivered jobs alike). Status: `Paid` if Date Invoiced is populated,
  `Sent` if delivered but not yet invoiced, `Draft` otherwise, `Cancelled` if the job's
  own status is Cancelled.
- **Invoice # matches the TEAM order-book reference** (e.g. `TEAM2485`) rather than an
  internal number. It's read from "Coupa TEAM Ref" (the reliable source — matches this
  format on ~89% of jobs), falling back to "TEAM Quote Ref" for the handful of rows where
  only that column has one (that column is otherwise usually the *customer's* PO number,
  not a TEAM reference, so it's only used when it actually matches the TEAM-number
  pattern). The extraction copes with messy source cells (extra text, more than one ref
  jammed into a cell) and pulls out the first valid one. About 9% of jobs (158 of 1,735)
  have no TEAM reference recorded in either column in the source data at all — those keep
  a fallback `INV-<job number>` so they're still identifiable. **Note:** TEAM refs are not
  unique per job in the source data — one TEAM quote can cover several line items, so the
  same reference (e.g. `TEAM1984`) legitimately appears on multiple invoices; that's
  carried through faithfully rather than forced unique.
- **Contract Code** (the "type of work" column, e.g. `FS`, `INT`, `NEW`) is stored on each
  Works Order as its own field and can be reported on via the Reporting page's By Contract
  Code view. Obvious case-only duplicates in the source ("New" vs "NEW", "Lnew" vs "LNew")
  are folded into one during import; the Reporting page decodes each code against the
  Order Book's own "Info Sheet" legend (e.g. `FS` → Fixture Service) for readability. It's
  also a dropdown on the New/Edit Works Order form now, not just import-only — sourced
  from that same legend (`CONTRACT_CODE_INFO` in `static/js/app.js`), so a Works Order
  created directly in the app can have its code set the same way an imported one does.
- **Not imported**: Transport Cost (tracked as a side metric in the source — cost figures
  already reconcile without it), TOWI/Coupa/Quote reference numbers, and the workbook's
  pivot/dashboard sheets (Info Sheet, Monthly Order Intake, PivotSummary, Insights) — this
  app's own Dashboard and Reporting page recreate those views live from the imported data.

**Known side-effect of this being real, messy operational data:** because `budget_cost`
is *estimated* (35% of Invoice Value, applied to every imported job — none of them carry
a figure anyone at the business actually set), the Cost Above Estimated Target alert
fires often — 389 of 1,755 jobs at last count. Treat it as directional, not a hard
control total, until real budgets are entered per job (editing a Works Order's Budget
Cost through the app marks it as confirmed — see "Budget confidence" below).

## What's included

- **Database schema** (`schema.sql`) — Users, Customers, Suppliers, Quotes, Works Orders,
  Purchase Orders, Supplier Invoices, Labour Entries, Customer Invoices
- **Real-data importer** (`import_team_order_book.py`) + the source workbook
  (`data/Team_Order_Book.xlsm`) — see above
- **Demo seed data** (`seed.py`) — 5 customers, 5 suppliers, 10 works orders, 20 purchase
  orders, 15 supplier invoices, 30 labour entries, 10 customer invoices, 5 user accounts
- **REST API** (`app.py`) — full CRUD for every entity, plus `/api/dashboard`,
  `/api/reporting`, `/api/reporting/export`, `/api/reporting/spend-by-supplier`,
  `/api/reporting/monthly-income-spend`, `/api/reporting/income-by-contract-code`
  (each report also has its own `/export` CSV endpoint)
- **Role-based access control** — enforced server-side on every write endpoint, and
  reflected in the UI (buttons/nav hidden where a role has no permission)
- **Profitability engine** — recalculated live on every request, never cached
- **Dashboard** — KPI cards (Revenue, Costs, Profit, Average Margin, Active Jobs,
  Loss-Making Jobs), a **Works Orders & Stock** row (Live Works Orders, Due This Month/2
  Months/3 Months, Total Stock Held — see below), profit-by-job / margin-by-job / monthly
  revenue / monthly cost charts, and a live alerts feed
- **Reporting page** — four reports behind one **report-type toggle** at the top (Job
  Profitability, Spend by Supplier, Monthly Income & Spend, Income by Contract Code — see
  the dedicated section below for all four). The original Job Profitability report is
  sortable, searchable, paginated, exportable to CSV; an overall profitability summary bar
  at the top, and a By Job / By Customer / By Contract Code toggle to switch between a
  per-job list and totals rolled up per customer or per contract-work-type code. The
  Loss-Making Jobs card in that summary bar is clickable — it jumps straight to a
  worst-losses-first list of just those jobs, with a chip to clear the filter again.
  **Customer and Contract Code filter dropdowns** narrow the report to one customer, one
  code, or both at once (combined with the search box and each other), independent of
  which grouping view you're in — e.g. filter to one customer, then switch to By Contract
  Code to see just their own code breakdown.
- **Filter dropdowns on every list page** — Quotes, Works Orders, Purchase Orders, Goods
  In, Despatch Notes, Inventory, Hours Quoted & Booked, Supplier Invoices and Customer
  Invoices all have the same kind of filter toolbar as Reporting: Customer and Contract
  Code everywhere a job underpins the record, plus whichever "raised by" field fits that
  page (Owner on Quotes, Project Manager on Works Orders, Raised By on Purchase Orders,
  Received By on Goods In, Despatched By on Despatch Notes, Employee on Hours Quoted &
  Booked) and a Supplier dropdown on Purchase Orders, Goods In, Supplier Invoices and
  Inventory. Every dropdown's own list of options always stays the full list — picking one
  filter never shrinks what another dropdown offers — and all the filters on a page combine
  together, and with the search box, the same way the Reporting page's already did.
- **Quotes** — a new tab between Dashboard and Works Orders for tracking quotes before
  they become Works Orders. Quote numbers are assigned automatically and sequentially
  (`TMQ-3000`, `TMQ-3001`, ...) rather than typed in, and can't be changed once created —
  see the roadmap note below.
- **SharePoint Folder links** — Quotes, Works Orders and Purchase Orders each have an
  editable link field, shown as a 🔗 icon next to the record wherever it appears — manual
  for now, ready to be auto-populated once real SharePoint folder creation exists (see
  roadmap note).
- **Ship To addresses** — Purchase Orders have a Ship To field, managed from Settings, so
  new delivery sites can be added without a code change (see the dedicated section below).
- **New Quote wizard** — a 4-step cost-buildup wizard (labour hours against a rate card,
  purchases, live profit/margin summary) behind the Quotes tab's + New Quote button (see
  the dedicated section below).
- **Quote PDF generation** — every quote can produce a customer-facing PDF quote letter
  matching the business's own real template, logo and all (see the dedicated section below).
- **Purchase Order PDF generation** — every PO, itemised or not, produces a PDF matching
  the business's own real PO template (see the dedicated section below).
- **Goods In** — acknowledge a delivery against a Purchase Order, open to every role, with
  a goods received document (delivery note, packing slip, or photo) required on every new
  record for audit purposes, and drag-and-drop to attach it as an alternative to the
  operating system's own file-picker dialog (see the dedicated section below).
- **Despatch Note signatures** — a Despatched note can be signed for on-screen, on the
  despatching staff member's own device at the point of delivery, with the captured name,
  date and signature embedded into the Despatch Note PDF (see the dedicated section below).
- **Who created or changed a record** — Customers, Suppliers, Works Orders, Quotes, and
  both kinds of Invoices all track who created and last edited them, automatically from
  the logged-in session (see the dedicated section below).
- **Quoted vs Booked hours comparison** — link a quote to its Works Order once one
  exists, and the Hours Quoted & Booked page can show quoted hours next to what's
  actually been booked (see the dedicated section below).
- **Fixed-price contract codes** — four Contract Codes (FS - L/M/H/HQ) auto-fill Purchase
  Order Value and Budget Cost on the Works Order form with their standard figures, all
  priced at 20% margin (see the dedicated section below).
- **Inventory** — a stock catalogue with allocations to a Site or a Works Order, enforced
  as an either/or at the database level (see the dedicated section below).
- **203 suppliers**, extended with Supply Of / Reason For Use / Authorised By from the
  business's own supplier register (see the dedicated section below).
- **Automatic alerts** — Budget Exceeded (red, only for a confirmed budget), Cost Above
  Estimated Target (amber, for an import-estimated budget — see below), Margin Risk
  (amber), Over-Invoiced PO (approval-required)
- **Per-contract-code margin targets** — the Margin Risk alert, status colours, and the
  Reporting page's Target % column normally compare against a **35% default**, but specific
  contract codes can have their own target instead. Currently set: **Integration (INT):
  10.7%**; **FS - L, FS - M, FS - H, FS - HQ: 20%** each (see "Fixed-price contract codes"
  below). Managed from **Settings** in the sidebar (Administrator only) — no code changes
  needed to add, change, or remove a target.
- **UK date formatting throughout** — every displayed date reads DD/MM/YYYY (e.g.
  05/01/2026), including table columns, the Quote PDF, and CSV exports. `<input
  type="date">` fields still use the browser's native picker, which renders in your OS's
  own locale — that's standard HTML behaviour and outside the app's control, but the value
  it produces round-trips correctly either way. Currency already used UK formatting
  (`en-GB`, £ with comma thousands) from the start, so this was specifically a date fix.
- **Wide tables stay scrollable from the top of the page** — a table's own height is now
  capped, with its own internal scrollbar, instead of growing to fit every row and pushing
  its horizontal scrollbar off past the bottom of a tall page. The column headers stay
  visible while you scroll through rows, too.

## Reporting

The Reporting page (sidebar) holds four reports, switched between with a toggle at the
top of the page — they're one "Reporting" nav item, not four separate screens, since
they're all different cuts of the same financial data:

- **Job Profitability** — the original Reporting view (see "Reporting page" above):
  per-job Revenue/Cost/Profit/Margin, with By Job / By Customer / By Contract Code
  groupings, filters, and CSV export.
- **Spend by Supplier** — total non-cancelled Supplier Invoice spend per supplier, with
  an invoice count and the date of their most recent invoice. Every supplier is listed,
  even one with no invoices yet (useful for spotting a supplier that's been set up but
  never actually used) — a toggle button hides those by default to keep the list focused
  on suppliers you're actually spending with, and an **All years** dropdown narrows to
  one year of invoices at a time.
- **Monthly Income & Spend** — the same Income/Spend figures as the Dashboard's own
  "this year to date" chart, but as a standalone report that can look at any year with
  data, not just the current one: a line chart plus a month-by-month table (Income,
  Spend, Net), with a running total row at the bottom. Income is non-cancelled Customer
  Invoices; Spend is non-cancelled Supplier Invoices plus Labour Entries — the exact same
  definitions the Dashboard chart uses, so the two can never disagree with each other.
- **Income by Contract Code** — non-cancelled Customer Invoice income, rolled up by
  Contract Code instead of by job, with a **Project Manager filter** — since Simon asked
  for this one to be filterable "by person," and Project Manager is the field already
  used as the "person" filter everywhere else a job underpins a record (Quotes' Owner,
  Purchase Orders' Raised By, Despatch Notes' Despatched By, and so on). Pick a Project
  Manager to see just their own jobs' income broken down by contract code, or leave it on
  **All project managers** for the whole business; Customer and Contract Code filters and
  a search box are there too.

Every report is sortable by column, paginated, and exportable to its own CSV — open to
every role (the same as the original Reporting page), since Finance, a Project Manager,
or an Administrator might reasonably want to check any of these.

## Profitability calculations

For every Works Order:

```
Revenue           = Sum(Customer Invoices, excluding Cancelled)
Actual Cost        = Sum(Supplier Invoices) + Sum(Labour Costs) + Sum(Stock Allocated, at Unit Cost)
Outstanding
 Commitment         = max(0, Sum(Purchase Orders) - Sum(Supplier Invoices))
Forecast Cost       = Actual Cost + Outstanding Commitment
Profit              = Revenue - Actual Cost
Forecast Profit     = Revenue - Forecast Cost
Margin %            = (Profit / Revenue) * 100        (0% if Revenue is 0)
Forecast Margin %   = (Forecast Profit / Revenue) * 100 (0% if Revenue is 0)
```

**Design note:** Outstanding Commitment is floored at zero. If invoiced amounts on a PO
exceed the PO value, that overage is already captured in Actual Cost — it shouldn't also
create a *negative* future commitment that inflates the forecast. It's also flagged as an
"Over-Invoiced PO" alert so it gets human attention.

Divide-by-zero (jobs with no revenue yet) is handled by returning 0% margin rather than
raising an error.

## Dashboard chart area

The Dashboard's three charts (below the KPI cards) all answer "how is this year going,"
not "how has this job ever done" — a different, narrower cut of the data from everything
else in the app (Works Orders, Reporting), which always shows a job's whole-life total
regardless of when the activity happened:

- **Top 15 Jobs by Profit & Loss-Making Jobs** — the 15 highest-profit jobs **this year**
  (never a job currently in loss, however few profitable jobs exist), alongside every job
  actually losing money this year, worst first, capped at 15 but never padded past the
  jobs genuinely in loss — a job making a tiny-but-positive profit never fills up this list
  just to reach a round number. Click a bar to jump straight to that job.
- **Profit vs Margin %** — one bubble per job active this year, sized by Revenue
  (area-proportional, so twice the revenue reads as twice the area, not four times it) and
  coloured the same red/amber/green as everywhere else in the app (loss-making / below its
  margin target / at-or-above target). Replaces a plain "Margin % by Job" bar chart, which
  couldn't distinguish a £50 job at 100% margin from a £500k job at 20% margin — both read
  as identical bars. Click a bubble to jump to that job.
- **Monthly Revenue, Cost & Profit** — one combined chart (same £ axis, so Revenue, Cost
  and Profit are always directly comparable) instead of two separate, disconnected Revenue
  and Cost charts — the Profit trend line is the one you'd otherwise have to work out by
  eye. January through the current month, this year only, zero-filled so a quiet month
  still shows on the axis.

**The year is always the current calendar year — there's no date-range picker, and this
is deliberate, not a missing feature.** A Dashboard is a snapshot of how things stand
right now; comparing this year to a previous one, or the same month across different
years, is a genuinely different question with a different answer shape (two periods side
by side in a table), and belongs on a dedicated Reporting view if that's ever needed, not
a toggle bolted onto this chart area.

**Known simplification:** these three charts leave Inventory/stock cost out of a job's
cost figure, unlike the lifetime Actual Cost shown everywhere else. `stock_allocations`
records where a unit of stock currently sits, not a dated cost event, so there's no sound
way to attribute it to one calendar year — a job's lifetime total (Works Orders,
Reporting, the PDF documents) still includes it in full; this only affects what these
three Dashboard charts show.

## Dashboard operational metrics (Works Orders & Stock)

- **Live Works Orders** — a count of Works Orders with status **Open** or **In Progress**
  (the same definition already used for the Active Jobs card above it — Draft, Completed,
  Closed and Cancelled jobs don't count as "live"). **Both this card and Active Jobs are
  clickable** (whenever the count is above zero) — they jump straight to the Works Orders
  page, filtered to exactly that same set. "Active" isn't a real status value stored
  anywhere; it's shorthand the Works Orders filter and `/api/works-orders?status=Active`
  both understand as "Open or In Progress," so the count on the tile and the list you land
  on are always the same jobs, not two separately-maintained definitions of "active" that
  could quietly drift apart. Same clickable-card pattern as Reporting's Loss-Making Jobs
  card (see the User roles section below).
- **Due — [Month] / [Next Month] / [Month After]** — how many *live* Works Orders have an
  End Date falling in each of the next three calendar months, labelled with the actual
  month names (e.g. "Due — September 2026") so they're never ambiguous about which month
  they mean. These are **discrete, non-overlapping buckets** — each Works Order lands in
  at most one of the three, whichever calendar month its End Date falls in, never counted
  twice. A Works Order overdue from an *earlier* month doesn't roll into the first bucket —
  it simply isn't in any of these three, since none of them is the month it was actually
  due. A Works Order with no End Date set, or one due more than three months out, can't
  contribute to any of the three, but both are still counted in Live Works Orders.
  **Each of these three cards is clickable too** (whenever its count is above zero) —
  same jump-to-Works-Orders pattern as Active Jobs/Live Works Orders, filtered to Active
  status *and* an End Date in that specific month. The exact year and month each card
  means is decided once, on the backend (`due_month_keys` in `get_dashboard`), and the
  frontend just links to it — never recomputed independently on the way there, which is
  what keeps the count on the card and the list you land on always exactly agreeing, even
  right at a month boundary. A small "due in September 2026 · clear" note appears on the
  Works Orders page while this filter is active, since unlike the plain status dropdown,
  there's no persistent control for it to show as selected.
- **Total Stock Held** — the total value of everything currently in the Inventory system,
  at Purchase Cost, added up across every site and every Works Order allocation combined.
  The same figure shown as "Total Stock Value (Cost)" on the Inventory tab's Items view —
  this is that same number, surfaced on the Dashboard too, not a separate calculation.
  **Also clickable** — takes you straight to Inventory's Items view (the one this figure
  is drawn from), regardless of whether the total is currently zero.

## Version number and update check

The version of the app is shown at the foot of the sidebar ("Version 1.2.0"; hover for the build date). It lives in one place, `version.py` — change `APP_VERSION` and the date there when you build a new release.

- **Protects your data from an old copy.** The database remembers the newest version that has opened it. If an older copy of the app is started against it (for example someone restores an old app folder), the app stops with a plain message and changes nothing, instead of risking damage. Install the newer version and start again. (Only for a deliberate exception: set `TEAMWORKS360_ALLOW_OLDER=1`.)
- **Version changes are logged.** Each time the version changes, the audit log records the old and new version.
- **Optional "Update available" notice.** Off unless whoever hosts the app sets `TEAMWORKS360_UPDATE_URL` to an `https://` address of a small file such as `{"version": "1.1.0", "notes": "What's new"}`. Administrators then see an amber "Update available" line under the version. It checks at most every 6 hours, fails silently, and the app never depends on it. It cannot be set from inside the app.

## Alerts: open the job, accept with a reason

On the Dashboard's Alerts panel and in the alerts pop-up (top right), each alert's job
number is a link: click it and you land on that job (Works Orders, narrowed to the job, with
its profitability open), or on the quote for a "Quote Past Validity" alert. Next to each
alert is an **Accept** button: type a short reason ("variation order agreed") and the alert
stops showing everywhere, with your name and the date recorded. **View accepted alerts**
lists them, with the reason, and **Undo** puts one back.

An acceptance applies to that alert *as worded*, and the wording carries the figures. If
the job's cost or margin later moves, it is a new alert and shows again on its own. There
is no separate "reject": an alert you don't accept simply stays, and the job link takes you
to where you fix it. Administrators and Project Managers can accept; Executives can see
accepted alerts but not change them. The Go-Live reset clears them along with the jobs.

## User roles & permissions

| Role             | Permissions |
|------------------|-------------|
| Administrator    | Full access — all records, users, settings |
| Project Manager  | Create/manage Works Orders and Quotes, enter Labour, view profitability (read access elsewhere) |
| Procurement      | Create/approve Purchase Orders, manage Inventory, view Suppliers (read access elsewhere) |
| Finance          | Enter Supplier & Customer Invoices, run Reports (read access elsewhere) |
| Executive        | Dashboard and Reporting only, fully read-only elsewhere |
| Service Engineer | Works Orders, Purchase Orders, Goods In, Despatch Notes, Inventory and Hours Quoted & Booked — can create and edit in those six areas, and sees nothing else (no Dashboard, Quotes, Invoices, Customers, Suppliers, Reporting, Alerts, Users or Settings). Cannot delete anything, and **never sees job money**: no purchase order value, budget, cost, profit, margin or alerts on Works Orders (not even sent to their browser), and the job's value/budget stay untouched when they edit a job |

**Service Engineer is locked down by the server, not just the menu.** Anything outside
their six areas is refused even if someone types the address by hand (`SERVICE_ENGINEER_API_PREFIXES`
in `app.py`). Existing databases pick up the new role automatically on the next start.

**Goods In is the one deliberate exception** — every role, Executive included, can create
and edit Goods In records (see the dedicated section below), since acknowledging a
delivery needs to be doable by whoever's actually there to sign for it, not gated to one
department. Deleting a Goods In record is still Administrator-only.

Server-side enforcement lives in `app.py` (`ROLE_PERMISSIONS` + the `@permission_required`
decorator on every write route) — the front-end hides buttons for roles that can't use
them, but the API is the actual gatekeeper.

## Security

This app went through a hardening pass — here's exactly what's protected, what you still
need to do, and why.

**Fixed in the code:**
- **Secret key**: no longer hardcoded. On first run, `app.py` generates a random 64-character
  key and saves it to `.secret_key` in the app's data directory (see "Deploying updates
  safely" below — owner-read-only file permissions), or reads one from a
  `SECRET_KEY` environment variable if you set one. Previously this was a fixed string baked
  into the source — anyone who read the code could have forged a login session for any role,
  including Administrator, without ever needing a password. **Keep `.secret_key` private** —
  don't commit it, email it, or include it if you ever share this codebase; delete it and
  restart the app to force everyone to log in again.
- **Debug mode is off by default.** Flask's debug mode includes an interactive
  in-browser debugger that allows arbitrary code execution if it's ever reachable — it must
  never be enabled outside your own machine. Opt in for local development only with
  `FLASK_DEBUG=1 python app.py`; you'll see a warning printed when you do.
- **Binds to `127.0.0.1` (localhost) by default** — not reachable from other devices on your
  network unless you explicitly set `FLASK_HOST=0.0.0.0`.
- **Login throttling**: 8 failed attempts for a given username+IP locks that combination out
  for 5 minutes, slowing down password-guessing. Timing is also equalized between "wrong
  password" and "no such user" so login responses can't be used to enumerate valid usernames.
- **Session cookies** are `HttpOnly` (JavaScript can't read them, limiting what a
  cross-site-scripting bug could steal), `SameSite=Lax` (not sent on cross-site requests),
  and expire automatically after 8 hours of issue. Set `FLASK_HTTPS=1` once this is served
  over HTTPS so the cookie is also marked `Secure` (never sent over plain HTTP).
- **Security headers** (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Permissions-Policy`) are set on every response.
- **All database queries are parameterized** (no string-built SQL anywhere) — not vulnerable
  to SQL injection. All user-supplied text is HTML-escaped before being rendered in the UI —
  not vulnerable to stored cross-site scripting.
- **Malformed input can't crash a request** — numeric fields are safely coerced with a
  fallback instead of raising an unhandled exception (which, combined with debug mode, would
  have leaked a stack trace).
- **Role permissions are enforced server-side** on every write endpoint (see the "User roles
  & permissions" section above) — the UI hides buttons a role can't use, but the API is the
  actual gatekeeper, verified by direct API testing, not just UI behaviour.
- **Deleting the last Administrator account is blocked**, so user management can't
  accidentally lock everyone out. The same protection applies to editing a user's role —
  you can't demote the last remaining Administrator either.
- **Users are editable** (role, email, password) from the Users page — full name and
  username are locked once an account is created and can't be changed through the UI or
  the API, even if a request tries to sneak them through.
- **Content-Security-Policy header** on every response, restricting what the browser will
  ever execute or load: scripts only from this app's own origin plus the one CDN Chart.js
  is loaded from, no plugins/objects, no framing by another site. The HTML/JS here never
  builds an inline `<script>` tag or an inline `onclick=`-style attribute (every handler is
  wired with `addEventListener` in `app.js`) — so even if a future bug let an attacker's
  `<script>` tag slip past the HTML-escaping that already blocks stored XSS, the browser
  would refuse to run it. (Inline `style="..."` attributes are allowed — `app.js` uses them
  throughout for dynamic layout, a far lower-risk concession than allowing inline scripts.)
- **CSRF protection on every write.** `SameSite=Lax` on the session cookie already blocks
  the classic cross-site-form-POST attack in any modern browser, but as a second,
  independent layer every `POST`/`PUT`/`DELETE`/`PATCH` must also carry a per-session
  security token (`X-CSRF-Token` header) that only this app's own JavaScript can read —
  an attacker's page, on a different origin, has no way to read or forge it, so a request
  forged from elsewhere has nothing valid to send and is rejected with a 403. A session
  that was already open when this shipped (no token yet) heals itself automatically on its
  very next request — nobody needs to be forced to log out and back in for this to take
  effect.
- **Login activity is now durably logged** (`login_events` table), not just the in-memory
  throttling counter that resets on every restart. An Administrator can see who tried to
  sign in, from what IP, and whether it succeeded, failed, or was locked out, from **Users
  → Login Activity** — e.g. to check whether a run of failed attempts against an account
  was real.
- **Minimum password length (8 characters)** is enforced server-side whenever an
  Administrator types an explicit password for a new or existing user — not just on the
  "change your own password" screen. An auto-generated one-time password (the default when
  none is typed) is already far stronger than this minimum, so it's unaffected.
- **Two-factor authentication (2FA) is mandatory for every account** — see "Two-factor
  authentication" below for the full picture (setup, Microsoft Authenticator, backup codes,
  and what an Administrator can do if someone loses their phone).
- **Deleting a user, or changing their role, takes effect immediately** — on that account's
  very next request, not whenever their existing browser session happens to expire (sessions
  last up to 8 hours). Previously, a session that was already open when an account got
  deleted (or demoted out of Administrator) kept working with its old access for the rest of
  that window; every request now re-checks the account against the database first.
- **Every PDF download and CSV export is durably logged** (`document_access_log` table) —
  who, which document, and when. An Administrator can review it from **Users → Document
  Access**, the same way **Users → Login Activity** already covers sign-ins.

**Still your responsibility:**
- **Change the demo passwords** (`admin123`, `pm123`, etc.) before this holds real data for
  real use — see "Demo accounts" above. These are intentionally simple for local evaluation.
- **This is a local single-machine tool by default.** If you want to reach it from another
  computer or put it on the internet, you need a real production WSGI server (`gunicorn` or
  `waitress`, not `python app.py`) behind a reverse proxy (nginx/Caddy) doing TLS
  termination (HTTPS) — the built-in Flask dev server is explicitly unsuited for that and
  says so in its own startup banner, and now also prints a loud warning of its own if you
  set `FLASK_HOST` to anything other than `127.0.0.1`/`localhost`. See "Securing shared
  hosting with HTTPS" below for a concrete recipe.
- **Treat your database and the `data/` folder as sensitive.** They hold your real customer,
  supplier and job financial data. Your database is backed up automatically (see "Deploying
  updates safely" below), but also back it up like any other business record to storage you
  control, and don't put any of it somewhere publicly accessible (e.g. a public file share
  or an unauthenticated cloud bucket).
- **OS-level file permissions**: the app now locks these down itself on every startup
  (`harden_file_permissions()` in `db_location.py`) — the data directory, the database, the
  backups folder and every file in it, and the secret key are all restricted to owner-only
  (`0700`/`0600`). This is a real, meaningful restriction on **Mac/Linux**. On **Windows**
  (where most installs of this app actually run) it does much less — Python's permission
  calls there only toggle the read-only attribute and can't touch Windows' own per-user
  access control list (NTFS permissions), so another account on the same Windows machine
  isn't actually kept out by this alone. On a shared Windows PC, the real protection is a
  Windows user account only the right people can log into, or setting NTFS permissions on the
  `TEAMworks360` folder by hand (right-click → Properties → Security → Edit, and remove
  access for anyone who shouldn't have it).
- **Encrypting the data at rest.** `jpm.db` and its backups are plain, unencrypted SQLite
  files — fine if the machine itself is physically secured and single-user, but if it's a
  shared machine, a laptop that could be lost or stolen, or backups get copied to cloud
  storage, that's real customer and financial data sitting in the clear. The practical fix
  is whole-disk encryption on the machine itself — Windows **BitLocker** or Mac
  **FileVault**, both built in and free to turn on — rather than anything at the app level,
  which would complicate every backup and every direct look at the database for little real
  gain at this scale.
- **Keeping dependencies current and checked for known vulnerabilities.** See "Checking
  dependencies for known vulnerabilities" below.

### Two-factor authentication

Every account — every role, no exceptions — needs a second factor beyond their password to
sign in. This is enforced server-side on every single endpoint, not just at the login screen,
so there's no way to skip it by, say, bookmarking a different page.

**Setting it up (first time only, per account):**
1. Sign in as normal with your username and password.
2. You'll land on a "Set Up Two-Factor Authentication" screen showing a **QR code** and, below
   it, the same secret as a **setup key** — a short block of letters and numbers — in case you'd
   rather type it in or your phone can't scan the screen.
3. Open an authenticator app on your phone — **Microsoft Authenticator**, Google
   Authenticator, Authy, or any other TOTP app — and choose **Add account**, then either point
   the camera at the QR code or look for **"Enter a setup key"** / **"Enter code manually"** and
   type in the key exactly as shown (spaces are just for readability, they don't matter). Both
   options set up the exact same secret, so it makes no difference which one you use.
4. Your app will immediately start showing a 6-digit code that changes every 30 seconds. Type
   the current one into TEAMworks360 and confirm.
5. You'll be shown **10 backup codes**, once, as a safety net — see below. Save them somewhere
   safe, then continue into the app.

From then on, every time you sign in, you'll be asked for the current 6-digit code from your
authenticator app, in addition to your password.

**How the QR code is generated.** It's rendered on the server (`qr_png.py`) using the QR
encoder bundled inside `reportlab` — already a pinned dependency of this app, used elsewhere for
the quote/PO/despatch-note PDFs — so this adds nothing new to `pip install`. The setup key stays
alongside it (not replaced by it) because it's the one guaranteed to work even if an image
somehow fails to load, and because some people simply find typing a short key more reliable
than lining up a phone camera.

**Backup codes** are for when you don't have your authenticator app to hand — a new phone, a
factory reset, no signal. Each of the 10 codes works exactly once; use one instead of a
6-digit code (there's an "Use a backup code instead" link on the sign-in screen) and it's
consumed immediately. If you use them all up, or never saved them in the first place, ask an
Administrator to reset your 2FA (next paragraph) — you'll go through setup again from
scratch, with a new key and a fresh set of 10 codes.

**For Administrators — Users → Reset 2FA:** if someone loses their phone and has no backup
codes left, open **Users**, find their account (accounts with 2FA enabled show a "Reset 2FA"
button), and reset it. This clears their current setup key and backup codes; their next
sign-in walks them through setup again from the start. It doesn't touch their password.

### Securing shared hosting with HTTPS

"Shared hosting" mode (`serve_shared.py`) is explicitly for a trusted office LAN, and says
so on every startup — there's no HTTPS, so traffic (including login passwords) crosses the
network in plain text, which is an accepted, low-risk tradeoff behind your own
router/firewall. If you want to close that gap properly — a larger office, a network you
trust less, or compliance reasons — put a reverse proxy doing TLS termination in front of
`serve_shared.py` rather than exposing it directly. The simplest option is
[Caddy](https://caddyserver.com/) (a single static binary, no config language to learn):

```
# Caddyfile - one line is usually enough
your-internal-hostname.local {
    reverse_proxy 127.0.0.1:5000
}
```

With a real public domain pointed at the host, Caddy provisions a trusted Let's Encrypt
certificate automatically. On a LAN with no public domain, add `tls internal` to have Caddy
mint its own locally-trusted certificate instead (everyone connecting will need to trust
that certificate once, the same one-time step as any internal/self-signed CA). Either way,
once a proxy is terminating HTTPS in front of it, set `FLASK_HTTPS=1` when starting
`serve_shared.py` so the session cookie is also marked `Secure` (never sent back over plain
HTTP, in case someone still reaches the unencrypted port directly).

## Shared hosting (multiple people, one shared database)

**The default setup (`start_windows.bat` / `start_mac_linux.command`) is single-user by
design.** Its database lives in *your own* home directory (`~/TEAMworks360/`),
and the server only listens on `127.0.0.1` — "this machine only." Putting the app folder
on a shared network drive does **not** make it multi-user: everyone who runs it from
there still gets their own separate, private database in their own home directory, and
nobody can reach anyone else's copy over the network even if they wanted to. That's the
explanation if different people seem to be seeing different Customers, Suppliers, or
Users — they're each looking at their own database, not a shared one.

**`start_shared_host.bat` / `start_shared_host.command` is the real fix.** One person —
whoever runs this — becomes the server; everyone else just opens a browser to the address
it prints. No install, no copy of the app folder, nothing else needed on their end. This
uses [waitress](https://docs.pylonsproject.org/projects/waitress/), a real production
WSGI server, instead of Flask's own development server (the one the single-user launcher
uses, and which prints its own warning that it isn't meant to serve anyone beyond that).

- **One shared database, not several.** Because only the host machine runs a server
  process, there's exactly one `jpm.db`, in the host's own home directory — everyone
  connecting to it sees the same live Customers, Suppliers, Users, Works Orders, and
  everything else, the moment anyone changes it, the same way the single-user version
  already worked for one person.
- **`serve_shared.py`** is the actual entry point (`app.py` is unchanged and still runs
  Flask's dev server when started directly, e.g. from `start_windows.bat`). It runs the
  same startup routine as the single-user launcher — legacy-database migration, backup,
  schema migrations — then serves the same Flask `app` object through waitress, bound to
  `0.0.0.0` (every network interface) rather than `127.0.0.1`, on 8 threads so several
  people can be using it at once. `WAITRESS_THREADS` overrides the thread count and
  `FLASK_PORT` the port, as environment variables, if 5000 isn't free or 8 threads isn't
  enough for how many people are using it at once.
- **Finding the address**: the launcher auto-detects the host machine's own LAN IP (the
  one other devices on the same network actually use to reach it — not `127.0.0.1`,
  which only ever means "this machine") and prints it directly, e.g.
  `http://192.168.1.42:5000`. Anyone on the same network types that into their own
  browser. It changes if the host's IP changes (e.g. after a DHCP lease renewal or moving
  networks), so re-check it if colleagues suddenly can't connect after the host machine
  was restarted or moved.
- **The host machine has to stay on and keep that window open.** Closing it stops the app
  for everyone currently using it, not just the host.
- **Trusted local network only.** There's no HTTPS here, so traffic — including login
  passwords — travels unencrypted on the local network. That's an ordinary, low-risk
  tradeoff on an office LAN behind its own router/firewall; it is not safe to forward the
  host's port 5000 to the wider internet. (`SESSION_COOKIE_SECURE` is already wired to
  `FLASK_HTTPS=1` in `app.py` for the day this runs behind a real HTTPS reverse proxy —
  not needed for ordinary LAN use.)
- **Concurrent writes are handled properly**, not just tolerated: SQLite's WAL (Write-Ahead
  Logging) journal mode is turned on for every connection, so reads are never blocked by
  a write in progress, and every connection has a 10-second busy timeout, so two people
  saving at almost the same instant wait briefly for each other rather than one of them
  getting a "database is locked" error. Verified directly against a real concurrent-write
  test (15 simultaneous writes from separate sessions, all succeeding) before shipping
  this, not just assumed.

### "The host machine can use it fine, but a colleague can't connect"

Almost always the host machine's own firewall silently blocking the connection — not a
real network problem, and not something either machine will show an error for; the
connection just times out. `start_shared_host.bat` already tries to open the port through
Windows Firewall automatically (`netsh advfirewall firewall add rule ...`, removing any
rule of the same name first so re-running this never piles up duplicates) — but that
needs Administrator rights to succeed, and fails silently if it doesn't have them. If
colleagues can't connect:

1. Right-click `start_shared_host.bat` and choose **Run as administrator** once, so the
   automatic rule can actually be added.
2. Or add the rule yourself: Windows Defender Firewall → Advanced Settings → Inbound
   Rules → New Rule → Port → TCP → the port shown when the app starts (5000 by default).
3. On Mac, check System Settings → Network → Firewall.
4. **A VPN active on the host machine can also make the printed address wrong** — if it
   routes all traffic through a tunnel, the auto-detected "LAN IP" may be the VPN's
   internal address instead of the one colleagues on the office network can actually
   reach. Disconnect the VPN on the host while hosting, if this is happening.

## Test copy (sandbox)

A separate, **isolated** copy of the app for trying things out, training, or letting a
few colleagues poke around — with its own database, completely walled off from the real
one. Nothing entered here ever reaches production, and nothing added to production
afterwards ever appears here.

- **Starting it**: double-click `start_test_shared_host.bat` (Windows) or
  `start_test_shared_host.command` (Mac/Linux) — mirrors `start_shared_host.bat`/
  `.command` (same waitress-based shared server, same "everyone just opens a browser"
  model) but points at a separate data folder and a separate port, so it's safe to run
  on the very same machine as the real host at the same time without the two ever
  colliding or mixing data.
- **Its own data folder**: `~/TEAMworks360-Test/` (i.e. `C:\Users\<you>\TEAMworks360-Test`
  on Windows), not `~/TEAMworks360/` — a completely separate `jpm.db`, with its own
  backups, Goods In documents and signatures subfolders. This is the `TEAMWORKS360_DATA_DIR`
  environment variable at work (see `db_location.py`): every file path the app uses
  follows it, so there is no code path by which the test copy can read or write the real
  database.
- **Its own port**: 5050 by default (the real shared host uses 5000), so both can run at
  once on the same machine. Colleagues connect exactly the way they do for the real shared
  host — they're just given a different address, e.g. `http://192.168.1.42:5050`, printed
  by the launcher the same way.
- **Seeded once, from a real snapshot, then fully independent.** The first time
  `start_test_shared_host.bat`/`.command` runs, it copies `test_seed_data/jpm.db` — a
  one-time snapshot of the real database as it stood on 2026-10-04, then brought up to date with the 2026 Order Book on 2026-10-07 — into the new test
  data folder. Every run after that leaves the test copy's database alone, so from that
  point on it's a genuinely separate system: edits, new jobs, deleted suppliers, anything
  — in either copy — never affect the other. See `test_seed_data/README.txt` for exactly
  how to pull a fresh snapshot later (replace that file, delete the existing
  `~/TEAMworks360-Test` folder, run the launcher again).
- **Always visibly marked.** Every screen — including the login screen, before anyone
  signs in — shows an orange "⚠ TEST COPY — not the live system" banner across the top,
  the browser tab title is prefixed `TEST COPY —`, and the sidebar carries a small "TEST
  COPY" pill next to the logo. Nobody using it, including someone who's only ever used the
  real system, can mistake which one they're in.
- **One known gap**: the seed only carries the database itself, not the actual uploaded
  files it refers to. Goods In documents and Despatch Note signatures that existed in the
  real system as of the snapshot date will show up as records in the test copy (the works
  orders, the metadata) but clicking to view the actual document or signature image will
  fail, since that file was never copied — only the database, which is all the seed
  contains. This isn't a bug in the test copy; there's no live-file-copy mechanism to go
  with the one-time database snapshot.
- **Same security posture as real shared hosting**: trusted local office network only, no
  HTTPS, so don't forward this port to the internet either — see "Shared hosting" above
  for the full detail, all of which applies here unchanged.

## Checking dependencies for known vulnerabilities

`requirements.txt` pins every dependency to an exact version rather than "whatever's newest
and compatible" (the old `Flask>=3.0` style) — so two people setting this up a year apart
land on identical versions, instead of silently different ones that make a bug report like
"it works for me" ambiguous. Pinning exactly means those versions don't update themselves,
though, so periodically check them against known vulnerabilities and re-pin deliberately:

```bash
pip install pip-audit
pip-audit -r requirements.txt
```

This checks every pinned version against public vulnerability databases and tells you
plainly whether anything needs bumping — rather than guessing from how recent a version
looks. If it flags something, update that one line in `requirements.txt`, re-run the
[automated tests](#automated-tests) to confirm nothing broke, and re-run `pip-audit` to
confirm it's actually clear.

## Deploying updates safely

Your database lives **outside** this app folder entirely, in a stable location tied to your
user account, not to wherever this code happens to be unzipped:
- **Windows**: `C:\Users\<you>\TEAMworks360\`
- **Mac/Linux**: `~/TEAMworks360/`

This means **installing an update is as simple as replacing this whole folder with a new
one** — delete the old folder (or rename it, if you'd rather keep it around a while), unzip
the new version in its place, and run the launcher as usual. Your data is never inside the
folder being replaced, so there's nothing to remember, no files to carefully avoid
overwriting, and no risk of a code update silently resetting your business data back to
whatever was in the zip.

**Automatic backups.** Every time the app starts, it copies the current database to a
timestamped file in a `backups` folder alongside it (e.g.
`~/TEAMworks360/backups/jpm-20260914-090000.db`) before serving any requests, and
keeps the most recent 20 automatically pruning older ones. If anything ever goes wrong —
a bad update, an accidental deletion, a mistake you want to undo — the fix is to close the
app, copy the backup you want back over `jpm.db`, and restart.

**Uploaded Goods In documents live here too**, in a `goods_in_documents` folder alongside
`jpm.db` and `backups` — so, like the database, they survive a plain app-folder update
untouched. They aren't included in the automatic database backups above (they're plain
files, not part of `jpm.db`), so if you back this machine up yourself, back up the whole
`TEAMworks360` data folder, not just `jpm.db`.

**Upgrading from an older version of this app** (one that still kept `jpm.db` next to the
code) works automatically too: the first time you run the updated launcher, it detects a
database still sitting in the app folder and moves it — not copies it — to the new external
location, printing a message confirming this happened. This is a one-time move; after that,
the app folder never holds your data again. **This one thing matters for that specific
transition**: make sure your existing database is still present in the app folder the first
time you run the *updated* code — extract the update into the same folder as your current
install (letting it overwrite the code files) rather than deleting the old folder first, just
this once. Every update after that is safe to install however you like, including a full
delete-and-replace, since your data will already be living outside the app folder by then.

**Renamed from Job Profitability Manager to TEAMworks360** works the same way, one more
time: if you're updating from a copy still called Job Profitability Manager, the first
time you run the new launcher it finds your existing database, secret key, and backups
under the old `~/JobProfitabilityManager/` folder and moves the whole lot into
`~/TEAMworks360/` — including the secret key, deliberately, so nobody's already-open
browser session gets logged out just because the app was renamed. This, too, is a
one-time move; nothing is left behind in the old folder afterwards (it's safe to delete
once you've confirmed the app starts normally), and a brand new install that never had
the old folder just skips straight past this.

**Schema and reference-data updates reach your live database automatically too.**
Protecting your data from code updates (above) created a real gap the first time it
mattered: a code update can add a new database column, or bundle new reference data to
import (like an updated supplier list) — but once your database lives outside the app
folder, a plain code update has no way to reach it on its own. `migrations.py` closes that
gap: every time the app starts, it (1) adds any column that's new in this version but
missing from your database, and (2) re-applies each bundled data import, matched by name
so it only ever adds or updates — never duplicates — regardless of how many times it runs.
This is why, the first time you start the app after an update that changes the schema or
bundles new reference data, you may see a line or two printed to the console (e.g.
`Schema update: added suppliers.supply_of`, or `Supplier list reconciled: 187 created, 12
merged`) before the "Using database at..." line — that's this running, once, automatically.
Nothing to do on your end; it's the same "just replace the folder and run the launcher"
process as any other update.

**A migration step can never stop the app from starting.** Every step in `migrations.py`
runs inside its own error handling, so if one fails for any reason — most plausibly, a
required package like `openpyxl` isn't installed yet on a setup that predates it needing
one — it prints a clear one-line message explaining what it skipped and why, and the rest
of startup continues completely normally. This came from a real incident: an early version
of this system let a missing `openpyxl` package (needed only for reading the bundled
supplier list, not for running the app day to day) crash the whole app on startup with a
Python traceback, on a setup that had never needed that package before. Two things fixed
it: the resilience described here, so a failed migration step degrades instead of
crashing, and adding `openpyxl` to `requirements.txt` and both launchers' dependency
checks, so it's actually installed automatically going forward rather than just failing
gracefully forever. If you ever do see a "Skipped..." message on startup, running the
launcher again after confirming your internet connection (so `pip install` can fetch
anything missing) is usually all that's needed.

## Updating Works Orders from a newer Order Book

When the Order Book spreadsheet has moved on (new jobs, jobs delivered or invoiced, cost
figures filled in), use **`update_order_book.bat`** (Windows) or
**`update_order_book.command`** (Mac/Linux) - close TEAMworks360 first. Drag the new
spreadsheet onto the window, read the **preview**, and type `YES` to apply it.

This is deliberately **not** `import_team_order_book.py`, which wipes every customer,
supplier, works order, purchase order and invoice and rebuilds them from the spreadsheet
(right for a first set-up, destructive on a system people are already using). The update
matches on job number (TM No) and:

- **adds** jobs that aren't in the app yet, built the same way the full import builds a job;
- **refreshes** existing jobs from the spreadsheet - status, dates, value, owner, contract
  code, details - and the records the import generated for them (the customer invoice, the
  bought-out purchase order and supplier invoice, the in-house labour line) so revenue and
  cost follow the new figures;
- **never changes an existing job's customer.** The app splits customers by site (e.g.
  "Rolls Royce - Derby"); new jobs are given the customer the app already uses for that
  customer/site, and anything it hasn't seen before is flagged for you to check;
- **leaves alone, and lists for review,** any job - or generated record - that a person has
  created or edited in the app, and any budget someone has confirmed;
- **never touches** jobs that are in the app but not in the spreadsheet (e.g. test jobs),
  nor Goods In, Despatch Notes, Quotes or hand-raised Purchase Orders;
- uses the supplier register's fuller names (so "Towne" doesn't create a duplicate of
  "Towne Lifting").

The preview changes nothing and writes a CSV of every individual change (and the review
list) to your TEAMworks360 data folder, `order_book_update_preview.csv`. Applying takes a
timestamped backup first and makes all the changes in one go. Running it again straight
afterwards changes nothing.

Run against the 4 October snapshot with the 2026 Order Book (1,909 jobs), it added 154 jobs,
refreshed 984, found 770 already correct, and left 1 alone; afterwards invoice and cost
totals matched the spreadsheet to the penny, apart from the one job left alone.

## Go-live: resetting job data

Settings has a **"Go-Live: Reset Job Data"** button (Administrator only) for the moment you
want to stop testing/trialling the app and start running real jobs through it from a
completely clean slate — useful if the data currently in the app is demo data, imported
test data, or an earlier trial period you don't want carried forward.

It permanently deletes every **job transaction**: Quotes, Works Orders, Purchase Orders,
Goods In records (and their uploaded documents), Despatch Notes, Supplier Invoices,
Customer Invoices, Labour Entries, and any stock currently allocated to one of those jobs.
It deliberately leaves everything else exactly as it is:
- **Reference data** — Customers, Suppliers, Contract Codes, the Rate Card, these Settings
  (including Ship To addresses) — since that's setup work you'd have to redo, not job
  history.
- **User accounts** — nobody needs to be re-created or re-invited.
- **Every audit/security trail** — Login Activity, Document Access, and the field-change
  audit history are never touched, so there's a permanent record that a reset happened
  (logged as its own entry) even though the job data it reset is gone.

Because of how disruptive this is, it's deliberately hard to trigger by accident: typing
the exact confirmation phrase shown in the dialog is required, and a timestamped backup of
the current database is taken automatically first (the same mechanism described in
"Deploying updates safely" above) — so even a reset run by mistake can be undone by
restoring that backup file the normal way. Quote and Despatch Note numbering (TMQ-/TMD-)
automatically restarts from the beginning once their tables are empty, exactly as if the
app had just been freshly installed.

## Budget confidence (Budget Exceeded vs Cost Above Estimated Target)

Every Works Order's `budget_cost` is either a **confirmed** figure (something a person
typed in, via the New/Edit Works Order form) or an **estimated** one (the import's flat
35%-of-invoice-value target, applied uniformly to every imported job — see "Real-data
import" above). The alert engine treats these very differently:

- **Confirmed budget, cost exceeds it** → red **Budget Exceeded** — a real overrun against
  a number someone actually set.
- **Estimated budget, cost exceeds it** → amber **Cost Above Estimated Target** — cost is
  running above the 35% target, but there's no evidence anyone set that as a real budget
  for this specific job, so it's flagged as worth a look, not a confirmed violation.

**Currently, all 1,755 imported jobs carry an estimated budget** — the flat 35% rule is
applied to every one with no exceptions (an earlier version of the importer treated a
minority of jobs as "confirmed" based on the source spreadsheet's own TCP column, but
that column turned out to itself just be an automatic formula in most rows, not a figure
anyone had deliberately set — so that distinction was retired along with the 70% rule it
came from). At the 35% target, **389 of 1,755 jobs (22%) currently show Cost Above
Estimated Target** — noticeably more than under the old 70% target (which flagged far
fewer, since a higher cost allowance is easier to stay under), because 35% is a
deliberately stricter bar. This is expected, not a bug: it directly reflects the
business's own decision to set the target there, not a change in how any job's real
revenue or cost is calculated. Treat it as directional, not a hard control total, until
real budgets are entered and confirmed per job.

**Opening a Works Order's Edit form and saving it marks its budget as confirmed** — a
clear amber hint appears next to the Budget Cost field first if it's currently estimated,
so this is never silent. That's a deliberate design: it gives a path to improve alert
accuracy over time as real budgets get reviewed and entered, without needing a separate
"confirm this" workflow. Newly created Works Orders are always confirmed, since a person
chose that number directly. **Note:** re-running the importer (e.g. after a fresh Order
Book export) resets every imported job back to the flat 35% estimate, since it fully
rebuilds Works Orders from the spreadsheet each time — a confirmed budget only survives
re-import if you re-confirm it afterwards.

## Quote PDF generation

Every quote — from the wizard or the Edit form — can produce a customer-facing PDF quote
letter, matching the layout of the business's own real quote (`Quote_Template_Example.docx`,
bundled in `data/`, one actually sent to a customer). **PDF** on the Quotes list, or
**Download Quote PDF** on the Breakdown view, generates it on the spot — nothing pre-built
or cached, always reflects the quote's current data.

**What's on it:** TEAM logo, recipient address block, reference lines (Your Ref, Quote Ref
with an optional linked Works Order reference, Date), the quote description as a bold
subject line, "For the attention of / Dear [name]" if a contact name is set, a bulleted
scope of work (the description plus any Scope Items entered, one per line, as sub-bullets),
Price and Delivery terms, a sign-off using the quote's Owner, and the same footer as the
real template — both office addresses, phone, email, website, company registration number,
and the ISO 9001/UKAS, SafeContractor, SafePQQ and CE Marking Association badges.

**New fields**, all optional, added to both the wizard (Step 1) and the Edit Quote form
specifically to feed this PDF — a quote created without filling them in still produces a
clean, valid PDF, just a shorter one (no "Dear X" line without a contact name, etc.):
- **Contact Name** — auto-fills from the customer's own contact name if one's on file,
  editable per quote since the right person to address isn't always the customer's default
- **Your Ref** — the customer's own reference (shows "TBC" if left blank, matching the
  real template's own convention)
- **Works Order Ref** — free text (e.g. `TM-9098`), not a linked record, since a quote
  often predates the Works Order existing in the system at all
- **Scope Items** — one per line, rendered as sub-bullets under the main description
- **Delivery Terms** — defaults to "Invoiced in Arrears." if left blank

**Two things simplified on purpose, not oversights:**
- The real template has two more reference lines (TOWI Ref, and a code like "Ref: AP-6")
  whose meaning wasn't clear enough to reproduce accurately — rather than guess and risk
  a wrong label on a real customer-facing document, they're left off entirely.
- The subject line reads `<description> - <quote number>`, not the real template's
  "Supplier Support - TMQ-XXXX – Re-Active Repair & Service." wording, since that phrasing
  encodes a job-type category the app doesn't currently track.

**Storing it in SharePoint** is still a manual step — download the PDF, then upload it into
the folder linked by that quote's SharePoint Folder URL field. This fits the same "manual
until real SharePoint integration exists" pattern as the rest of the app (see the roadmap
note below) — automatic upload is a natural next step once that integration exists.

## Purchase Order PDF generation

Every Purchase Order — new or existing — can produce a PDF matching the layout of the
business's own real PO master templates (`TEAM_Purchase_Order_MASTER_Derby.xlsx` and
`..._Phoenix.xlsx`, bundled in `data/`). **PDF** on the Purchase Orders list generates it
on the spot, always from the PO's current data.

**What's on it:** TEAM logo, the site's own address (matching whichever Ship To address
the PO uses) alongside Date/P.O. No./Works Order No./Account No., Supplier and Deliver To
side by side, Date Required, an itemised table (Qty/Part No./Item #/Description/Works
Order No./Unit Price/Line Total), Invoice To, Subtotal/VAT/Total, and Raised By — plus the
same official footer as the Quote PDF (both office addresses, certifications, etc.). VAT
is calculated at the PO's own VAT Rate field (20% by default, matching the real template's
own fixed formula, but editable per PO).

**Itemised lines are new** — the New/Edit Purchase Order form now has an Items section
(Qty, Part No., Item #, Description, Unit Price), add or remove rows freely. **PO Value
becomes read-only and totals automatically from these the moment any item is added** — it
only stays a plain editable field for a PO with no items at all (a simple, non-itemised
PO), so there's never a case where PO Value and the itemised total could silently
disagree. A PO with no items still produces a complete, correctly-formatted PDF — it
falls back to a single line built from the PO's own Description and PO Value, which is
exactly how every Purchase Order created before this feature existed still works,
unchanged.

**The two documents deliberately share one set of assets.** `pdf_common.py` holds the TEAM
logo and the official footer (both addresses, certifications) used by both `quote_pdf.py`
and `po_pdf.py` — any future branding change (a new certification, an address update) only
ever needs to happen in that one file, not separately in each document generator.

## Goods In

Acknowledges that a delivery against a Purchase Order has physically arrived — its own
tab, right after Purchase Orders in the sidebar. **Deliberately open to every role**,
including Executive (otherwise fully read-only everywhere else) — whoever's actually
there to sign for a delivery needs to be able to log it, not just whoever raised the PO.
Deleting a record is still Administrator-only, same safety pattern used everywhere else
in the app for removing something from an audit trail.

Captures: **Purchase Order** (a dropdown of existing POs), **Destination** — either
**Works Order** (the job the PO was raised against) or **Stock** (a Site, for stock going
into general inventory rather than straight to that job) — **Date Received**, **Quantity**
(Full or Partial, plus an optional actual quantity figure), **Received By** (defaults to
whoever's logged in, editable in case someone else physically signed for it), and a free
**Comment**.

**A goods received document is required.** Every new Goods In record must have at least
one file attached — a scanned or photographed delivery note, packing slip, or photo of the
goods/paperwork (PDF, Word, or an image) — before it can be saved; the form blocks saving
without one, both in the browser and, independently, on the server (so this can't be
bypassed by skipping the UI). This replaces the old manual step of filing the paper
delivery note in a separate folder — it's now attached directly to the record it belongs
to. More documents can be added later from Edit (e.g. a follow-up photo), and an
Administrator can remove a wrongly-attached one, except the last remaining one on a
record — a replacement must be uploaded first. Every document download is logged to the
same **Document Access** log (Users → Document Access, Administrator only) as PDF
downloads and CSV exports. The files themselves are stored outside the app folder, next to
the database (see "Deploying updates safely"), so replacing the app folder on an update
never loses them, and they're included when backing up the `TEAMworks360` data folder.
Records created before this feature existed are left as they are — not retroactively
blocked — but show "No document on file" so they're easy to spot and back-fill if needed.

**This is a standalone acknowledgement record, not a stock movement.** Saving a Goods In
record does not touch `stock_allocations`, `stock_movements`, or a Works Order's Actual
Cost — it's a paper-trail confirmation that something arrived, kept deliberately separate
from Inventory's own Move Stock mechanism (see below) so the two can never silently
double-count the same delivery. If stock also needs formally receiving into Inventory,
that's still done the normal way, via Inventory → Allocations → Move Stock.

**The attachment box also accepts drag-and-drop**, as an alternative to clicking it and
using the operating system's own file-picker dialog. This matters beyond convenience: on
some Windows machines, that OS dialog can itself hang or go blank (a browser/OS-level issue
with no connection to this app's own code — it's the same dialog Windows uses everywhere,
not something a web page can control or fix) — see "A frozen file-picker dialog" below.
Dragging a file in from an open folder window bypasses that dialog entirely, so it keeps
working even if the native picker is misbehaving. Dropping a second file adds to whatever's
already selected rather than replacing it.

## A frozen file-picker dialog

If clicking **Choose Files** opens a blank or "(Not Responding)" window instead of the
usual file browser, that's Windows' own Open File dialog hanging — the same dialog every
other Windows program uses, not anything specific to TEAMworks360. A web page has no way to
control, customise, or break that window; it just asks Windows to show it. In practice this
is usually one of: a stuck browser process (fully close and reopen the browser, not just the
tab), a cloud-sync tool like OneDrive mid-sync on the folder the dialog defaults to (pause
syncing and retry), or a browser/Windows bug that a restart or an update clears up. If it
keeps happening, **drag the file onto the upload box instead** (see "Goods In" above) —
that skips the broken dialog completely, since drag-and-drop never has to call it.

## Despatch Note signatures

A Despatch Note that's been marked **Despatched** can be signed for on-screen — a
**Sign** button appears in its row once it's in that status. This is meant to be used
on the despatching staff member's **own already-logged-in phone or tablet** at the
point of delivery, not by the customer on a separate device or link: whoever's handing
the goods over opens TEAMworks360 on their own device, finds the note, taps **Sign**,
and gets a dedicated full-screen signing screen (no sidebar, large touch targets) with
the customer's name, today's date, and a signature pad drawn with a finger or stylus.
Saving stores the captured name, date and signature image against that despatch note,
and the **Sign** button is replaced with a **Signed ✓** link (hover/long-press to see
who signed and when; click to view the captured image).

The signature then appears on the Despatch Note PDF in place of the old blank "Print
Name / Date / Signature" boxes — those blank boxes are still what's printed for a note
that's never digitally signed, so a despatch note raised before this feature existed,
or one still signed on paper, works exactly as before.

**Where the file goes, today.** The signature image is saved as a PNG on this
computer, in the same external `TEAMworks360` data folder as the database and Goods In
documents (see "Deploying updates safely") — so it survives an app update and is
included when you back up that folder. **There is no live SharePoint upload yet.**
Getting the signed PDF into the right SharePoint folder today is a manual step: download
it from the despatch note's **PDF** button (now showing the signature) and upload it to
SharePoint by hand, the same as any other document today. A live SharePoint hand-off
(automatically uploading the signed PDF to the job's folder) is a natural next step once
a SharePoint API connection exists for the app generally — see "Quotes, and the
SharePoint roadmap" — but building that out hasn't been done as part of this, to keep
this change small and avoid a new authentication surface area for the first version of
on-screen signing. Re-signing a despatch note (e.g. a mis-drawn signature) replaces the
previous image and name/date rather than keeping both; only one signature is kept per
despatch note.

Technically: the signing screen draws onto an HTML5 `<canvas>` via pointer events (so it
works the same with a finger, stylus, or mouse), sized for the device's actual pixel
density so the line looks crisp rather than blurry on a phone screen. On save, the
drawing is sent to the server as a PNG, validated, and written to disk — the same
`secrets.token_hex`-named-file-outside-the-app-folder pattern Goods In documents use,
and every view of a stored signature is logged to the same **Document Access** log
(Users → Document Access, Administrator only). Signing requires the same permission as
creating/editing despatch notes (`despatch_notes:write` — Administrator or Project
Manager); only an Administrator can delete a despatch note at all, which also removes
its signature file from disk.

## New Quote wizard (cost buildup)

The **+ New Quote** button on the Quotes tab now opens a 4-step wizard instead of a plain
form, built from the business's own `Master_Repair_Quote_Cost_Tracker.xlsx` (bundled in
`data/`):

1. **Quote Details** — customer, owner, description, dates, notes (same fields as before)
2. **Labour Costs** — a fixed rate card of 24 activities (grouped by category, e.g.
   Conventional Machining, CNC Machining) with standard and overtime hourly rates already
   filled in from the source workbook. You only enter hours for whichever activities
   apply — everything else stays at zero and is skipped, not stored as a blank line.
3. **Purchases** — a free-form list of brought-out costs: Cost (what the supplier charges
   you) and **Price to Customer** (what you'll charge for it) per line — add or remove
   rows as needed, starts empty. Both columns total live at the bottom of the step.
4. **Summary** — Total Labour Cost, Total Purchases (Cost) and Total Cost are calculated
   live; you enter the **Price to Customer** (what you're quoting the customer overall),
   and Profit/Loss and Profit % update as you type. If any Purchase line has a Price to
   Customer figure, a suggested total appears next to the field — labour at cost plus
   the purchases' priced-to-customer total — with a **Use this** button to accept it as a
   starting point; you can still freely override it either way. Clicking **Create Quote**
   asks "Have all purchases and hours been recorded for this quote?" as a last check
   before saving — answering no just returns you to the summary with nothing lost. Saving
   creates the quote with that Price to Customer figure as its Estimated Value, plus the
   full cost breakdown stored alongside it.

Every quote in the list has a **Breakdown** button showing the labour and purchase lines
that were entered, with the same profit/margin figures — including quotes created before
this feature existed, which just show "no cost buildup recorded" rather than an error.

**Editing an existing quote** (the Edit button, a plain form — it doesn't re-open the
wizard) asks the same "Have all purchases and hours been recorded for this quote?"
question before saving, for consistency with quote creation.

**Two things worth knowing:**
- Only two of the rate card's category groupings (Conventional Machining, CNC Machining)
  were explicitly labelled in the source spreadsheet. The other groupings (Design &
  Engineering, Fitting & Fabrication, Inspection & Testing, Other) are a sensible
  grouping of the remaining activities by type, added to make the wizard's table
  navigable — not something the source file itself specified. The actual activity names
  and hourly rates are taken directly from the workbook, unchanged.
- Rates are **snapshotted onto the quote** at the moment hours are entered, not looked up
  live each time. If the rate card changes later (see `rate_card` table in `app.py`/the
  database), quotes already raised keep the rate they were costed at — only new quotes
  see the new rate. There's no Settings screen for editing the rate card yet; it can be
  edited directly in `schema.sql` (for future installs) or the `rate_card` table (for
  this database) — happy to add a proper management screen for it if that's wanted next.

## Quoted vs Booked hours comparison

A quote's own **Quote Details** (in the wizard) and **Edit** form both have an optional
**Linked Works Order** dropdown — separate from **Works Order Ref**, which is free text
purely for what prints on the PDF. Works Order Ref usually gets filled in even before a
real job exists (often just "TBC"); Linked Works Order is for connecting a quote to an
*actual* Works Order already in the system, once one exists, purely so the hours quoted
for a job can be compared against what's actually been booked against it. Most quotes
won't have this set — that's expected, since a quote is normally written before its job
does — and it's fine to come back and set it later once the job's created.

The **Hours Quoted & Booked** page has an **Entries / Quoted vs Booked** toggle. The
second view lists every Works Order that has either a linked quote with labour hours, or
booked hours, or both, with Quoted Hours, Booked Hours, Variance (negative and
highlighted once booked hours exceed quoted), and both sides' cost. Quoted hours are
summed across every quote linked to that Works Order (so an original quote plus a later
variation both count), using each quote's own snapshotted rates — the same rates that
appear on that quote's own Breakdown. A Works Order with quoted hours but nothing booked
yet shows correctly with Booked at zero, not an error; one with no linked quote at all
simply doesn't appear on this view (though its entries are still on the Entries view, as
always).

## Automated tests

`tests/test_profitability.py` covers the money-calculating logic directly — the
Revenue/Cost/Profit/Margin arithmetic (`compute_job_metrics`), the alert rules (Budget
Exceeded, Cost Above Estimated Target, Margin Risk, Over-Invoiced PO), the Reporting
export (`build_report_rows`), the quote-validity rules (`default_valid_until`,
`is_past_validity`), and the Dashboard chart area's year-scoped figures
(`job_profit_margin_for_year`, and the Top/Loss-Making split in `/api/dashboard` itself) —
plus the security hardening covered above: the CSP header, CSRF enforcement on writes,
durable login-event logging (success/failure/lockout), the minimum-password-length check on
admin-set passwords, two-factor authentication (setup, verification, backup codes, attempt
lockout, and the Administrator-only reset), instant session cut-off on account deletion or a
role change, the document access log, file-permission hardening, Goods In's required-document
upload (and Administrator-only document deletion), the Go-Live reset endpoint
(`/api/admin/reset-job-data` — confirmation phrase, backup-before-wipe, and that reference
data/audit trails survive it), Despatch Note on-screen signature capture (permission and
status gating, input validation, the file landing on and being removed from disk, and the
signed PDF), and the three newer Reporting views — Spend by Supplier, Monthly Income &
Spend, and Income by Contract Code (Cancelled records excluded from every total, the year
and Project Manager filters, the "always offer the current year" fallback, and that every
role can reach them), and that `/api/reference` actually returns every field a page's
filter dropdowns need from it (the regression test for a real bug — see the changelog
note below). `TestDataDirOverride` checks the `TEAMWORKS360_DATA_DIR` override behind
the isolated test copy (`start_test_shared_host.bat`/`.command` — see "Test copy
(sandbox)" below): that every path the app uses for its data — the database itself,
backups, Goods In documents, signatures — follows the override when it's set, that an
unset or empty override falls back to the normal `~/TEAMworks360` exactly as before, and
does this by reloading `db_location.py` around each change, the same way a freshly
started process would compute it, rather than just setting the environment variable and
assuming. The hand-rolled TOTP algorithm in `totp.py` is also checked directly
against the official RFC 4226 test vectors, independent of anything in `app.py`. Most of
these run through the real Flask request pipeline (`app.test_client()`), not direct
function calls, since what they're checking — `before_request`/`after_request` hooks — is
exactly what calling a view function directly would skip straight past. 164 tests in
total. Every test builds its own throwaway SQLite database from `schema.sql` in a temp
file and deletes it afterwards, so running them never touches your real `jpm.db`.

**Bug fixed:** the Works Orders page's **Owner** filter dropdown only ever offered "All
owners," with no actual names to pick from. The cause was in `/api/reference`, not in the
filter code itself: that endpoint's `works_orders` query never selected the
`project_manager` column, so the Owner dropdown (which builds its list of names from this
reference data, not from the page's own current rows) always came up empty, on every
installation, with no error anywhere to point at it. Fixed by adding that column to the
query; `test_works_orders_reference_rows_include_project_manager` in
`tests/test_profitability.py` is a regression test specifically for this.

Uses only Python's built-in `unittest` — no extra install needed. Run with:

```bash
python3 -m unittest discover -s tests -v
```

Run this after making any change to the profitability/alerts logic in `app.py`, and before
installing an update, as a quick check that nothing in the core calculations moved.
`test_cancelled_po_excluded_from_over_invoiced_check` is worth knowing about specifically:
writing it caught a real bug (now fixed) where a cancelled Purchase Order could still
trigger an "Over-Invoiced PO" alert if a non-cancelled Supplier Invoice referenced it —
every other total in the same function already excluded Cancelled records, this one query
had been missed.

## Who created or changed a record

Customers, Suppliers, Works Orders, Quotes, Supplier Invoices, Labour Entries, and
Customer Invoices all record **who created them and who last edited them** —
`created_by`/`created_date`/`updated_by`/`updated_date` on each table, stamped
automatically from the logged-in session (`current_user_name()` in `app.py`), never from
anything the client sends. Every write endpoint already requires a valid login before it
runs at all, so this is never a guess. Shown as a small "Created by X on Y · Last edited
by Z on W" hint on each record's Edit form (`auditHint()` in `app.js`) — present on New
only once a record has actually been saved, since there's nothing to show yet.

This matters more now that shared hosting (above) makes genuine multi-user, concurrent
use real: several people can be entering data on the same live database at once, and
this is what makes a change traceable to a person rather than just "the app."

Purchase Orders' Raised By and Goods In's Received By are deliberately separate from
this — those are editable business fields (who's *responsible*, which may not be whoever
is physically typing, e.g. entering a delivery on someone else's behalf), not an
immutable audit trail. Both mechanisms coexist: a Purchase Order has both an editable
Raised By and, from now on, the same `created_by`/`updated_by` audit fields as everything
else.

**A database that predates this feature gets it automatically** — the self-healing
migration adds the new columns on next startup, the same way every other schema change
in this app rolls out. Existing rows have no history to backfill, so they show blank
(genuinely unknown, not a fabricated guess); only records created or edited from that
point on carry a name. One subtlety worth knowing: a fresh install picks up `created_date`
via each table's own column default, while a database that gets these columns through the
migration doesn't carry that default (SQLite's `ALTER TABLE` can't attach one retroactively)
— so every INSERT sets `created_date` explicitly in the SQL itself (`datetime('now')`)
rather than relying on the column default, precisely so this behaves identically either
way. Verified directly, not just assumed: tested against a simulated pre-migration
database and confirmed a newly created record gets a real `created_date`, not a blank one.

## Fixed-price contract codes

Four Contract Codes are fixed-price, standard-cost tiers rather than free-form
classifications: **FS - L, FS - M, FS - H, FS - HQ**. Each has a **Standard Value** and
**Standard Cost** set against it in `contract_code_targets` (Settings → Per-contract-code
margin targets, same screen as any other code's margin target, just with two more
optional fields). All four are priced at exactly **20% margin** by construction (cost is
80% of value in every case) — Purchase Order Value / Budget Cost pairs are `179.79 /
143.83` (L), `309.12 / 247.29` (M), `579.60 / 463.68` (H), and `450.00 / 360.00` (HQ) —
which is also why each one's own margin target is set to 20% rather than left at the 35%
default.

**The moment one of these four is picked as Contract Code on the New/Edit Works Order
form, Purchase Order Value and Budget Cost auto-fill with that tier's standard figures**
(`wo-contract-code`'s change handler in `app.js`) — both fields stay perfectly ordinary,
freely-editable inputs after that, so a figure can still be adjusted by hand for a
one-off exception without touching the standard pricing itself. Any *other* Contract
Code (one with no Standard Value/Cost set) leaves both fields untouched, exactly as
before this feature existed.

**Adding a fifth fixed-price tier** (or turning any other existing code into one) needs
nothing beyond the Settings screen — enter a Standard Value and Standard Cost there and
the same auto-fill starts working for it immediately, no code change required. Clearing
either field back to blank turns auto-fill back off for that code.

### The standard cost recipe behind each tier

Each tier's Standard Cost isn't an arbitrary number — it's built from real hours at real
rate-card rates, plus consumables, and the app shows this breakdown as a hint wherever
it's useful rather than leaving it as tribal knowledge:

| Tier | Fitting / Assembly | CMM | Team Transport | Consumables (from Inventory) | Total |
|---|---|---|---|---|---|
| FS - L | 4h @ £31 | — | 0.5h @ £20 | ~£9.63 | £143.83 |
| FS - M | 5h @ £31 | 2h @ £35.50 | 0.5h @ £20 | ~£11.29 | £247.29 |
| FS - H | 9h @ £31 | 3h @ £35.50 | 1h @ £20 | ~£58.18 | £463.68 |
| FS - HQ | 6.3h @ £31 | 3h @ £35.50 | — | ~£58.18 | £360.00 |

All four rates (Fitting / Assembly, CMM, Team Transport) are already standard entries on
the rate card — nothing needed adding there. **This recipe is a hint, not a
mechanism**: an engineer still logs their own real hours on the Hours Quoted & Booked
page and issues whatever consumables were actually used from Inventory, the normal way —
the recipe is there so they know what a typical job at that price point looks like, not
something the app enforces or auto-fills on their behalf.

Two places show it: the New/Edit Works Order form (`FS_TIER_RECIPES` + `updateRecipeHint`
in `app.js`), when one of the four codes is selected, and the New/Edit Booked Hours form
(same constant, `updateWoRecipeHint` inline in `openLabourForm`), the moment a Works
Order using one of these codes is picked — since that's the point someone's actually
about to log time against it. Both read from one shared `FS_TIER_RECIPES` constant, so
the wording only ever needs updating in one place.

## Inventory

A stock catalogue with a simple rule enforced at the database level: every unit of stock
is allocated to exactly one place — a **Site** (general stock sitting at Derby, Phoenix,
or wherever else gets added) or a **Works Order** (reserved or issued for that specific
job) — never both, never neither. A stock item's Total Quantity is always the sum of its
allocations, not a separately-tracked number, so the two can't drift apart. Four views,
switched with a toggle at the top of the Inventory tab:

- **Items** — the stock catalogue: Part Number (unique), Description, Unit
  (each/kg/m/box/...), **Purchase Cost** and **Sale Price** (tracked separately — what the
  item costs the business vs what it would be charged out at), Reorder Level, an optional
  linked Supplier, and Notes. When creating a new item, an optional **Initial Quantity** and
  **Held At (Site)** let you record stock you already have on hand right there, in the same
  step — no need to save the item and then separately go move stock into it. Four summary
  KPI cards up top (Stock Items, Total Stock Value at cost, Potential Value at sale price,
  Low Stock Items) so the gap between what's tied up in stock and what it could be worth is
  visible at a glance. The Total Qty column highlights amber once quantity drops to or below
  the Reorder Level (only when a reorder level above zero is set).
- **By Site** — every site currently holding stock, broken down to the item and quantity
  level (not just a count) — grouped by site, each with a per-site subtotal, sorted
  highest-value site first. This is the place to answer "how much stock do we hold, and
  where is it." Exportable to CSV.
- **By Works Order** — the same breakdown, but grouped by every Works Order stock is
  currently allocated to — a live view of what's reserved for which job, down to the item
  and quantity. Exportable to CSV.
- **Movements** — a full chronological audit log of every receipt, issue, and transfer:
  date, part, quantity, From, To, who moved it, and notes. Exportable to CSV.

**Moving stock** happens from the **Allocations** button on any stock item, via a single
"Move Stock" form with a **From** and a **To**:
- **From** — either "New Stock (Receipt)" (nothing to reduce — this is stock arriving
  from outside, e.g. against a purchase order) or any of the item's current allocations,
  each shown with how much is available there.
- **To** — a Site or a Works Order.

This one form covers **receiving** new stock (From: Receipt, To: a Site), **issuing** it
to a job (From: a Site, To: a Works Order), and **transferring** it between sites or
between jobs (From and To both existing allocations) — they're really all the same
underlying operation, just with different endpoints picked. Trying to move more than is
currently available at the source is rejected with the exact quantity that is available,
and every move is logged to the Movements audit trail with who did it and when
(`session["full_name"]` — the logged-in user) — this is the one place in the codebase
current-state (`stock_allocations`) and audit-log (`stock_movements`) are ever both
written, specifically so they can never drift apart (see `move_stock()` in `app.py`).

- **Permissions**: Procurement and Administrator manage inventory (create/edit items,
  move stock, remove allocations); everyone else has read access, same pattern as the
  rest of the app.
- **Deleting a stock item** removes its allocations with it (a confirmation names this
  explicitly) — there's no "orphaned allocation" state to worry about. Its movement
  history is removed too, for the same reason.
- **Stock allocated to a Works Order counts as that job's cost.** The moment stock is
  allocated to a job (quantity × the stock item's **Purchase Cost** — never Sale Price;
  the Sale Price field is for reference and margin visibility only, and never enters a
  job's cost calculation), it's added into that job's Actual Cost, alongside Labour
  Entries and Supplier Invoices — exactly the same way those already work. Profit,
  Margin %, and every alert that depends on cost (Budget Exceeded, Cost Above Estimated
  Target, Margin Risk) all react immediately, since they're all computed from the same
  underlying figure. This flows through everywhere a job's numbers appear — Dashboard,
  Works Orders, Reporting — because they all share one calculation function
  (`compute_job_metrics()` in `app.py`); there's no separate place that could show a
  different, stale number. The Works Order detail view breaks Actual
  Cost down by source (Labour / Supplier Invoices / Stock Allocated) once more than one
  of them is non-zero, so this is never a mystery jump in the numbers.

## Suppliers

Extended with three fields from the business's own supplier register
(`Supplier_List_14_9_26.xlsx`, bundled in `data/`), positioned right after Supplier Name
on both the list and the Add/Edit form, ahead of the original fields:
- **Supply Of** — what they supply (e.g. "specialist fasteners")
- **Reason For Use** — why this supplier is used (e.g. "only supplier available", "local
  to North site", "OEM")
- **Authorised By** — initials of who approved this supplier

**203 suppliers** are on file: the original 20 (from the Order Book import) plus 195 from
the register. This is applied automatically by `migrations.py` every time the app starts
(see "Deploying updates safely" above) — not a script you need to run by hand, and it
re-applies safely even after a full Order Book re-import (which rebuilds the suppliers
table from scratch) or on a completely fresh install.
**12 of those 195 turned out to already exist** under a shorter name (e.g. the existing
`Cromwell` matched the register's `Cromwell Tools`) — those were merged into the existing
supplier record rather than created as a duplicate, specifically so their linked Purchase
Orders and Supplier Invoices stayed attached to the same supplier rather than getting
split across two records for the same real company. 8 existing suppliers had no
corresponding entry in the register (`Bond`, `Carbide`, `Dixons`, `Hydraulics SS`,
`Perflex`, `RG Engineering`, `Sealants`, `Seals`) and were left untouched. The merge
mapping and its reasoning are in `migrations.py` (`SUPPLIER_MERGE_MAP`) if you ever need to
see exactly which name matched which. `import_supplier_list.py` still exists as a
standalone, manually-runnable version of the same logic — useful if you ever want to
import a differently-named file rather than the bundled one — but you don't need it for
this file specifically, since the app already reconciles against it automatically.

Every supplier — original, merged, or newly imported — is automatically available in the
Purchase Order form's Supplier dropdown, since that dropdown is always built from the full
current supplier list; there's nothing separate to wire up when a supplier gets added.

## Ship To addresses (Purchase Order delivery locations)

Purchase Orders now have a **Ship To** field — the delivery address the goods go to,
sourced from the business's own PO master template rather than typed freely each time.

- Seeded with two sites so far, both from the business's own PO master templates
  (bundled in `data/`):
  - **Derby** — Total Engineering Asset Management Limited, Unit 10, Enterprise Way,
    Derby, DE21 4BB
  - **Phoenix** — Total Engineering Asset Management Limited, Unit 5 Phoenix Road,
    Crowther Industrial Est, Washington, NE38 0AD
- Managed from **Settings** (Administrator only) — add every other site's address there
  as the business provides them; each one then appears in the Ship To dropdown on the
  Purchase Order form for everyone with Procurement access.
- An address in use on a Purchase Order can't be deleted (the same "can't remove
  something still referenced" pattern used elsewhere in the app) — it has to be taken off
  the PO(s) using it first.
- The field is optional on a PO — existing/imported Purchase Orders that predate this
  feature simply have no Ship To set, which is fine, not an error state.
- **Creating or editing a PO** asks "Have all items been added to this purchase order?"
  before it's saved — a last-check prompt, same idea as the Quote wizard's equivalent
  question. Answering no leaves the form open with nothing lost.

## Quotes, and the SharePoint roadmap

The Quotes tab is deliberately simple for now: Quote Number, Customer, Description,
Owner, Quote Date, Valid Until, Estimated Value, Status (Draft/Sent/Won/Lost/Expired) and
Notes. No SharePoint connection yet — that's intentional, per the plan discussed:

- **Auto-numbering today**: creating a quote assigns the next sequential number
  (`TMQ-3000`, `TMQ-3001`, ...) — never typed in, and fixed once created. All of this
  logic lives in one place: `get_next_quote_number()` in `app.py`.
- **In full deployment**: that one function is what would be swapped out to call the
  SharePoint API and create the next numbered folder there instead of just incrementing
  a number in SQLite — the rest of the app (the form, the API route, the "next number"
  concept) doesn't need to change.
- **Works Orders and Purchase Orders**: the same auto-numbering approach can be extended
  to `job_number` and `po_number` later using the identical pattern — a single
  "get next number" function per entity, called at creation time instead of accepting a
  typed-in number. Not done yet since your real Works Order numbers (`TM-nnnn`,
  `BLSK-nnn`, etc.) come from the imported Order Book and follow the existing TEAM
  numbering scheme, not a fresh sequence — this would need deciding on a transition point
  before switching it over. **Decision so far: waiting until deployment** to number Works
  Orders and Purchase Orders from existing data — no auto-numbering has been added to
  either yet, they're still typed in manually as before. For reference, if/when you're
  ready: your real Works Orders are almost entirely `TM-nnnn`, currently topping out at
  `TM-9429` (so `TM-9430` would be next); Purchase Order numbers are much less consistent
  (only 28 exist, some cells contain more than one PO number, several are blank/"N/A"),
  so that one needs an actual decision rather than just picking up where it left off.
- **SharePoint Folder link — added now**: Quotes, Works Orders and Purchase Orders each
  have a `sharepoint_url` field, editable from their forms and shown as a 🔗 link next to
  the record's number wherever it appears in a list. It's a plain manual text field today
  (paste in a link if you have one) — no live SharePoint connection, since that needs your
  actual tenant/site details and an app registration (client ID/secret or certificate) for
  authentication once you're ready, which should be configured as server-side environment
  variables, never typed into a chat. This field is deliberately built to be easy to
  populate automatically later: once real folder-creation exists, the same
  `sharepoint_url` field just gets filled in by that code instead of by hand.

## Project structure

```
jpm/
├── start_mac_linux.command  # Double-click launcher (Mac/Linux) - single user
├── start_windows.bat        # Double-click launcher (Windows) - single user
├── start_shared_host.command  # Double-click launcher (Mac/Linux) - shared hosting, see "Shared hosting"
├── start_shared_host.bat      # Double-click launcher (Windows) - shared hosting, see "Shared hosting"
├── start_test_shared_host.command  # Double-click launcher (Mac/Linux) - isolated test copy, see "Test copy (sandbox)"
├── start_test_shared_host.bat      # Double-click launcher (Windows) - isolated test copy, see "Test copy (sandbox)"
├── test_seed_data/          # One-time real-data snapshot the test copy seeds itself from - see its own README.txt
├── serve_shared.py          # Actual shared-hosting entry point - runs app.py's Flask app via waitress (used by both the real and test shared hosts)
├── create_shortcut.ps1      # Creates the Desktop shortcut - run by start_windows.bat, not directly
├── TEAMworks360.app/         # Double-clickable app bundle with the app's own icon (Mac)
├── TEAMworks360.desktop     # Double-clickable launcher with the app's own icon (Linux)
├── app_icon/                # icon.ico / icon.icns / icon.png / icon_master_1024.png - the app's icon
├── app.py                 # Flask app: routes, auth, profitability engine
├── db_location.py           # Where the database, backups, and Goods In documents live - see "Deploying updates safely"
├── migrations.py            # Schema self-healing + auto-applied data imports, run on every startup
├── pdf_common.py            # Shared TEAM logo/footer used by every PDF generator below
├── quote_pdf.py             # Quote PDF letter generator (reportlab)
├── po_pdf.py                # Purchase Order PDF generator (reportlab)
├── despatch_pdf.py          # Despatch Note PDF generator (reportlab)
├── totp.py                  # Two-factor authentication (RFC 6238 TOTP) - stdlib only, see "Security"
├── qr_png.py                # Renders the 2FA setup QR code to PNG (reportlab's QR encoder + Pillow, both already pinned)
├── schema.sql              # Database schema
├── seed.py                 # Sample data generator
├── import_team_order_book.py  # Real Order Book importer (first set-up only - wipes and rebuilds)
├── update_order_book.py     # Safe update of Works Orders from a newer Order Book - see "Updating Works Orders..."
├── prepare_for_deployment.py # Removes demo accounts and test records - see "Pre-deployment clean-up"
├── merge_suppliers.py      # Merges duplicate suppliers - see "Merging duplicate suppliers"
├── split_customers_by_site.py # Splits Rolls Royce jobs by site - see "Splitting Rolls Royce jobs by site"
├── import_po_register.py   # Adds the Purchase Order Register as history - see "Importing the Purchase Order Register"
├── import_quote_register.py # Adds quotes from the Quote Register spreadsheet - see "Importing the Quote Register"
├── update_order_book.bat / .command  # Double-click launchers for the update (preview, then YES to apply)
├── import_supplier_list.py  # One-time supplier register importer/merger
├── data/Team_Order_Book.xlsm  # Source workbook for the importer
├── data/Supplier_List_14_9_26.xlsx  # Source supplier register
├── data/Quote_Template_Example.docx  # Real quote used as the PDF template
├── requirements.txt
├── templates/
│   └── index.html          # SPA shell
└── static/
    ├── css/style.css       # Theme (traffic-light colours, KPI cards, tables, modals)
    ├── js/app.js            # Routing, views, CRUD forms, dashboard charts
    ├── img/teamworks360-mark.svg  # Sidebar/boot-screen mark - same design as the app icon
    └── pdf_assets/          # Logo + certification badge images for quote PDFs
```

Notice `jpm.db` isn't in this tree — see "Deploying updates safely" above for where it
actually lives and why.

## Known MVP limitations (by design, per brief: "focus on functionality first")

- SQLite rather than a client/server RDBMS — trivial to swap for Postgres by changing
  the connection logic in `app.py` and running `schema.sql` against it. Shared hosting
  (see above) makes SQLite's single-file nature work fine for a small team on one LAN —
  WAL mode plus a busy timeout handle real concurrent access correctly — but it's still
  one file on one host machine, not a resilient client/server database; a genuinely large
  team or a need for redundancy/failover is where a real RDBMS earns its complexity.
- Session secret and demo passwords are for local evaluation only — rotate the demo
  passwords, and if this ever moves beyond a trusted local network (e.g. behind a real
  HTTPS reverse proxy for remote access), also set `FLASK_HTTPS=1`.
- No file/document attachments on invoices or POs (Goods In is the exception - see its
  dedicated section above).
- No email notifications for alerts (they're surfaced in-app only, on the Dashboard and
  in each Works Order's detail view).


### Low stock warning (Inventory)

Each stock item has a **Reorder Level**. When an item's total quantity across all locations is **at or below** that level, the Inventory page shows an amber **Low stock warning** banner at the top listing the worst items (with a **Show only low stock** button to filter the table to them), plus a **Stock** column marking each item *Low stock* or *Out of stock* (quantity zero, shown in red). Items with no Reorder Level set (blank or 0) are never flagged; the Reorder Level column shows "not set" for them.

### Deleting a works order

An Administrator can delete a works order and everything attached to it: purchase orders (with their Goods In records and uploaded documents), supplier and customer invoices, labour entries, and despatch notes (with signatures). Quotes made from the job are kept and just unlinked, and stock movement history is kept. If **stock is still allocated to the job**, the delete is refused with a message naming the items — move them back to a site on the Inventory page first, so stock is never lost silently.


### Importing the Quote Register

`import_quote_register.bat` (Windows) / `import_quote_register.command` (Mac) adds the quotes from the TEAM Quote Register spreadsheet (the **Data** sheet) to the Quotes page. Close the app first, run it, drag the spreadsheet onto the window, read the preview, and type YES to apply (a backup is taken first). From the command line: `python import_quote_register.py <file.xlsx> [--apply]`.

- **Add-only and safe to re-run.** A quote number that's already in the app (including ones you raised there) is never changed, so running it twice creates no duplicates. Empty reserved numbers in the register are skipped.
- **Mapping.** Owner = the initials as typed; quote date = Date Quoted (else Date Request Received); Value → estimated value ("TBC" and blanks → £0, with the original wording kept in Notes); Contact, Towi Ref, Description carry across; Customer PO, fixture/part number, supplier used, type, comments and area go into Notes. A row holding two quotes (e.g. TMQ-1289.1 / .2) becomes two quotes.
- **Status.** Won → Won, Lost → Lost, Submitted → Sent, Cancelled → Cancelled, On Hold → On Hold (quotes now have all seven statuses: Draft, Sent, On Hold, Won, Lost, Expired, Cancelled; the register's own word is also kept in Notes). A blank status becomes Won if the quote has a works order, Sent if it was quoted, otherwise Draft.
- **Works orders and customers.** A quote whose works order is in the app is linked to it and takes that job's customer. Other quotes use the customer the app already uses for that register customer (Rolls Royce quotes follow the Rolls Royce works orders; the site goes in Notes). A register customer new to the app is created; anything that can't be placed goes under "Unknown Customer". Works orders the app doesn't have (older jobs) stay as text in the quote.
- A preview writes `quote_register_preview.csv` to your data folder listing every quote and anything to review.


### Importing the Purchase Order Register (history)

`import_po_register.bat` (Windows) / `import_po_register.command` (Mac) adds every PO from the TEAM Purchase Order Register (the **Data** sheet) to the Purchase Orders page **as history**. Close the app, run it, drag the spreadsheet onto the window, read the preview, and type YES (a backup is taken first). From the command line: `python import_po_register.py <file.xlsx> [--apply]`.

- **History only.** Imported POs are flagged *Historic* (a grey badge, and a "Live POs only / Historic POs only" filter on the Purchase Orders page). They are **not counted** in any job's cost, committed cost, forecast margin or "Over-Invoiced PO" alert, and don't appear in the PO pick-lists for new supplier invoices or Goods In. Without this, the register's purchases would have been added on top of the costs the Order Book already carries and made hundreds of jobs look loss-making. POs raised in the app from now on are unaffected and work as normal.
- **Add-only and safe to re-run.** A PO number already in the app is never changed.
- **Mapping.** PO Ref → PO number; Date PO Sent → order date; Expected Delivery → date required; Order Complete YES → Completed ("Cancelled" → Cancelled, blank → Issued); PO Owner → raised by; Value → PO value ("TBC"/"FOC" → £0, with the wording kept). Quantity, part numbers, supplier quote number, site, contact, end customer and the register's own works-order wording are added to the end of the description.
- **Works orders.** A PO whose job is in the app sits on that job. Older jobs and "Internal" purchases that aren't in the app sit on a placeholder job per customer called `HIST-PO-<CUSTOMER>` ("Historic purchase orders - … (jobs not in the app)"): Closed, no value, no costs.
- **Suppliers** are matched to yours ignoring Ltd/Limited and punctuation; a supplier you don't have yet is created, and the report lists them so you can spot spelling variants.
- The **Go-Live: Reset Job Data** button in Settings removes all POs, historic ones included, like every other job record.


### Which alerts show on the Dashboard and the sidebar

Dashboard and sidebar alerts cover **live work only**: jobs that are Draft, Open or In Progress, plus open quotes past their Valid Until date. A job that is **Completed, Closed or Cancelled** no longer raises a dashboard/sidebar alert (there is nothing left to act on), although its own detail view still shows what went over budget or margin, and its traffic light is unchanged. Alerts aren't stored anywhere, so there is nothing to "clear": they are worked out from the data each time.


### Pre-deployment clean-up

The login page no longer prints the demo accounts' passwords on a normal install (they only show on test copies and trial packs). To remove the demo sign-ins and the test records from a live database, close the app and double-click `prepare_for_deployment.bat` (Windows) / `prepare_for_deployment.command` (Mac), or run `python prepare_for_deployment.py [--apply]`. It previews first, takes a backup, then removes only:

- the five demo accounts (`admin`, `pmanager`, `procurement`, `finance`, `exec`);
- the test works orders `TEST` and `TM-10000` with everything attached to them;
- the test quote `TMQ-3000`, and the test stock items `123-4567` and `XC-123`.

Each must still carry its original demo/test name or description, otherwise it is skipped and reported. It refuses to run if it would leave no Administrator with two-factor sign-in set up. Login history, the audit log and the document access log are never deleted, and each removal is noted in the audit log.


### Quote statuses

Quotes can now be **Draft, Sent, On Hold, Won, Lost, Expired or Cancelled**. An existing database is upgraded automatically the next time the app starts (the quotes table is rebuilt once, keeping every quote and its cost lines), and quotes imported from the Quote Register that had to be filed as Lost / Sent are moved to the Cancelled / On Hold status the register actually gave them. Only Draft and Sent quotes raise a "past validity" alert.

### Merging duplicate suppliers

`merge_suppliers.bat` / `.command` (or `python merge_suppliers.py [--apply]`) folds a fixed list of known misspellings and name variants (e.g. "Red Stawberry Solutions" into "Red Strawberry Solutions") into the supplier you keep: purchase orders, supplier invoices and stock items are re-pointed, missing details are filled in, and the duplicate is removed. It previews first and takes a backup. It only touches the exact pairs listed in the script, and afterwards lists look-alike names it did **not** merge so you can check them by hand.

### Placeholder jobs for historic POs

The `HIST-PO-<CUSTOMER>` jobs that hold historic purchase orders for jobs not in the app are hidden from the Works Orders list, the Dashboard and the Reporting totals. They still appear on the Purchase Orders page (that is where the POs show their job and customer), but are not offered in the job pick-lists.

### Splitting Rolls Royce jobs by site

Almost every Rolls Royce job was loaded under the single customer "Rolls Royce - Derby", so reports by customer couldn't tell the sites apart. `split_customers_by_site.bat` / `.command` (or `python split_customers_by_site.py <Order Book file> [--apply]`) reads the **Site** column of the Order Book and moves each job to the matching site customer you already have (Washington to WMF, Fleet support to FSF, DCDD Derby to DCDD, DSFT Derby to DSFT, PCF Derby to PCF, CRF/BLSK Annesley to CRF, the Bristol sites to their Bristol customers, Ansty to a new "Rolls Royce - Ansty"). The table is `SITE_TO_CUSTOMER` at the top of the script.

- It previews first (and writes `site_split_preview.csv` to your data folder), takes a backup, and applies everything or nothing.
- It only moves jobs **currently under "Rolls Royce - Derby"**, so a customer you set by hand is never overwritten. Quotes linked to a moved job follow it. Nothing is deleted, and each move is audit-logged.
- Left under Derby on purpose: Derby, EOS Derby and SUBS Derby (they are Derby), plus Barnoldswick (Bankfield or Ghyll Brow - to be decided), ABCF Rotherham, Controls & Data, PTF and blank sites. To move one, add it to `SITE_TO_CUSTOMER` and run again.

### Automatic numbering and newest-first lists

Job numbers (**TM-nnnn**), purchase order numbers (**PO-nnnn**) and quote numbers (**TMQ-nnnn**) are always allocated by the app: the New form shows the next available number (greyed out) and it is assigned when you save. Whatever a client sends is ignored, so two people can't raise the same number, and editing never changes a number. Only plain numbers count when working out "next" - old register entries such as `PO-TM-8670` or `PO-1234 PO-1235`, and the `HIST-PO-*` placeholder jobs, are ignored (so, with the current data, the next job is TM-9596 and the next PO is PO-3302).

The Works Orders, Purchase Orders and Quotes lists open with the **highest number at the top**. On Works Orders you can still click the Job Number heading to flip the order. Purchase Orders can be filtered by **Raised By** (the "All raised by" drop-down).

### Tidying "Raised By" names

The imported PO history spells the same person many ways. `tidy_raised_by.bat` / `.command` (or `python tidy_raised_by.py [--apply]`) maps every known spelling to one full name (table `PEOPLE` at the top of the script), so the Raised By filter on Purchase Orders lists each person once. Where a PO names two people ("S Lee/K Hallam"), the person other than K Hallam is used. Entries it doesn't recognise are left alone and listed. It previews first, takes a backup, and records the original wording in the audit log and in `raised_by_tidy_report.csv`. On the PO form, Raised By now suggests the names already in use, to stop new variants creeping in.

### No made-up "HIST-PO" jobs on historic purchase orders

Historic POs (from the old register) whose job isn't in the app no longer sit on stand-in `HIST-PO-<CUSTOMER>` jobs. They simply have **no job**, and the register's own reference (e.g. `TM-1510`) is kept as plain text, shown on the Purchase Orders page as "Old job: TM-1510". A reference to a job number higher than the highest job actually raised is **not** kept. A new PO can only be placed against a job that exists. To clean an existing database, double-click `tidy_historic_pos.bat` / `.command` (or `python tidy_historic_pos.py [--apply]`): it previews, takes a backup, detaches the POs, removes the empty stand-in jobs, and writes `historic_po_tidy_report.csv`. The app also updates its own database structure automatically on start-up (historic POs are allowed to have no job). `import_po_register.py` now imports this way directly.
The Raised By drop-down lists only real people (staff names); anything else - initials, combined entries, notes - is grouped under "Other (not a listed person)".

### Purchase order numbers are only issued when a PO is raised

The Purchase Order Register had blank, pre-numbered rows at the end (PO-3238 onwards in the snapshot) which the first import brought in as empty POs, using up numbers. `tidy_historic_pos` now removes the trailing blank rows (no supplier, description, value or date), strips register clutter from descriptions ("date sent in register", spreadsheet errors such as `#DIV/0!`), and tells you the next PO number (one more than the last real PO). `import_po_register.py` skips blank rows and treats spreadsheet errors as empty. Historic POs no longer display any old TM job reference on the Purchase Orders page (the old reference is still stored quietly with the PO). A few very old PO numbers contain "TM-" (e.g. `PO-TM-8670`, `PO-2451-TM-7674`); they are left as the register had them, and every new PO is `PO-` plus the next number.

The historic-PO clean-up above also runs **automatically, once**, the first time the new version of the app starts on an existing database (a timestamped backup is taken first), so simply updating the app is enough. Running `tidy_historic_pos` by hand is only needed for a database that has never been through the new version.

`tidy_historic_pos` also removes **any** completely blank historic PO row (unknown supplier, no value, no date, no real description - an old job reference doesn't count as content) whose number is after the last real PO or isn't in the `PO-nnnn` form, and it writes `purchase_orders_to_check.csv` to your data folder listing every PO that still has an unknown supplier or an odd number, so they can be checked by eye. The automatic start-up clean-up is versioned, so this improved version runs once more on the next start.

### Starting the purchase order history again from scratch

`replace_historic_pos.bat` / `.command` (or `python import_po_register.py <register file> --replace-history --apply`) throws away every purchase order that was previously imported as history, and imports the Purchase Order Register again with the current, cleaner rules (no placeholder jobs, no blank pre-numbered rows, no spreadsheet errors). **Live purchase orders - anything raised in the app or created from the Order Book - are never touched**, and it stops without changing anything if a historic PO somehow has goods in or a supplier invoice against it. It previews first and takes a backup. The Purchase Orders page now opens showing **live POs only**; choose "Live + historic POs" or "Historic POs only" in the drop-down to see the history.

## Late Works Orders on the Dashboard (version 1.1.0)

A red-edged "Late Works Orders" panel sits under the Works Orders and Stock cards. A job counts as late when it is Open or In Progress and its End Date is before today. The panel shows how many are late, their **customer PO value**, their **cost to date** (purchases + hours + stock booked to the TM number), and the average days late. Below that is a list of the 10 latest jobs with each of those figures; days late show amber up to 30 days and red beyond. Click the count card, "View all", or a TM number to open the Works Orders page filtered to the late jobs (a "clear" link removes the filter). Purchases are supplier invoices, hours are the cost of booked labour, stock is the cost of stock allocated to the job. Service Engineers never see these figures.

## Summary panels and the Hours Booked report (version 1.2.0)

Each of these screens now has summary panels above the list:

- **Quotes:** quotes awaiting authorisation (status Sent) and customer purchase orders received (quotes Won this year). Click either to filter the list.
- **Works Orders:** open works orders (Open and In Progress) and completed year to date (Completed with an End Date this year). Click either to filter the list.
- **Purchase Orders:** open POs awaiting goods (Issued or Approved with nothing booked on Goods In yet) and part-received POs.
- **Goods In:** total goods received, this year to date, this month.
- **Despatch Notes:** total jobs complete (jobs with a Despatched note), this year to date, this month.
- **Customers / Suppliers:** the total number of each.

Service Engineers see the counts but never the £ values.

**Reporting - Hours Booked:** a new report type that totals the hours from Hours Quoted & Booked by month for a chosen year. Group it by employee, job, customer or contract code, filter by employee, customer or contract code, and export to CSV.

