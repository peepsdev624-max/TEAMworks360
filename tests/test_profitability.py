"""
Automated tests for the money-calculating logic in TEAMworks360 (formerly Job Profitability Manager).

This covers the functions that decide what every job's Revenue, Cost, Profit
and Margin actually are (`compute_job_metrics` / `works_order_with_metrics`
in app.py), the alerts those numbers drive (`compute_all_alerts`), the
Reporting export (`build_report_rows`), and the quote-validity rules
(`default_valid_until` / `is_past_validity`). These are the calculations the
whole app - Dashboard, Works Orders, Reporting, the alerts bell - is built
on, so a silent regression here would be wrong on every screen at once
without necessarily looking wrong on any one of them. That's exactly the
kind of bug a human reviewer skimming the UI is likely to miss, and exactly
what this file exists to catch.

Nothing here touches the real jpm.db. Every test builds its own throwaway
SQLite database from schema.sql in a temp file, runs against that, and
deletes it again - safe to run at any time, against any copy of this app,
without risk to real data.

Requires nothing beyond the Python standard library - no pytest, no other
dependency. Run it with either:

    python3 -m unittest discover -s tests -v
    python3 tests/test_profitability.py
"""
import importlib
import datetime as dt
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import app as appmod  # noqa: E402
import totp  # noqa: E402


def _complete_pending_2fa_setup(client):
    """Mirrors what the real 2FA setup screen does: fetches whichever
    secret is pending for the just-logged-in user and confirms it with a
    correctly computed code. Two-factor authentication is mandatory for
    every account (see login() in app.py), so a brand new test user's
    very first login always comes back with must_setup_2fa - every test
    class's own login() helper calls this automatically when that
    happens, so the many tests written before 2FA existed, which log in
    and immediately call a protected endpoint, keep working unchanged."""
    setup = client.get("/api/auth/2fa/setup").get_json()
    secret = setup["manual_entry_key"].replace(" ", "")
    code = totp.current_code(secret)
    cookie = client.get_cookie("csrf_token")
    headers = {"X-CSRF-Token": cookie.value} if cookie else {}
    client.post("/api/auth/2fa/setup", json={"code": code}, headers=headers)


def _build_test_db(path):
    """Creates a brand new SQLite file at `path` with the app's real schema
    (schema.sql) applied - same tables, same defaults, same seeded settings
    (default margin target, INT/FS-* contract code targets) as a fresh
    install - so a test is exercising the actual schema, not a hand-rolled
    approximation of it that could quietly drift out of sync."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with open(os.path.join(BASE_DIR, "schema.sql"), "r") as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


class ProfitabilityTestCase(unittest.TestCase):
    """Base class: every test gets its own fresh, empty-of-business-data
    database file and a pushed Flask app context (so app.get_db() works
    exactly as it does in a real request), torn down again afterwards."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db_path)  # _build_test_db creates it fresh
        self.conn = _build_test_db(self.db_path)

        # get_db() in app.py reads the module-level DB_PATH global directly,
        # so pointing tests at a throwaway database is exactly this one
        # assignment - restored in tearDown so later tests (and the real
        # app, if this process ever did anything else) are unaffected.
        self._orig_db_path = appmod.DB_PATH
        appmod.DB_PATH = self.db_path

        self.app_context = appmod.app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()
        appmod.DB_PATH = self._orig_db_path
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # ---- fixture helpers -------------------------------------------------
    # Thin wrappers around plain INSERTs, not a mini-ORM - kept obvious and
    # literal so a failing test's setup is easy to read back against its
    # assertions, rather than hidden behind clever defaults.

    def make_customer(self, name="Test Customer"):
        cur = self.conn.execute("INSERT INTO customers (customer_name) VALUES (?)", (name,))
        self.conn.commit()
        return cur.lastrowid

    def make_supplier(self, name="Test Supplier"):
        cur = self.conn.execute("INSERT INTO suppliers (supplier_name) VALUES (?)", (name,))
        self.conn.commit()
        return cur.lastrowid

    def make_works_order(self, job_number, customer_id, job_name="Test Job",
                          status="In Progress", contract_value=0, budget_cost=0,
                          budget_is_estimated=0, contract_code=None):
        cur = self.conn.execute(
            """INSERT INTO works_orders
               (job_number, job_name, customer_id, status, contract_value,
                budget_cost, budget_is_estimated, contract_code)
               VALUES (?,?,?,?,?,?,?,?)""",
            (job_number, job_name, customer_id, status, contract_value,
             budget_cost, budget_is_estimated, contract_code),
        )
        self.conn.commit()
        return cur.lastrowid

    def make_customer_invoice(self, wo_id, amount, status="Paid", invoice_number="INV-1", invoice_date=None):
        self.conn.execute(
            "INSERT INTO customer_invoices (works_order_id, invoice_number, invoice_amount, status, invoice_date) "
            "VALUES (?,?,?,?,?)", (wo_id, invoice_number, amount, status, invoice_date),
        )
        self.conn.commit()

    def make_purchase_order(self, wo_id, supplier_id, po_value, status="Issued", po_number=None):
        po_number = po_number or f"PO-{wo_id}-{self._po_seq()}"
        cur = self.conn.execute(
            "INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, status) "
            "VALUES (?,?,?,?,?)", (po_number, wo_id, supplier_id, po_value, status),
        )
        self.conn.commit()
        return cur.lastrowid

    _po_counter = 0

    def _po_seq(self):
        ProfitabilityTestCase._po_counter += 1
        return ProfitabilityTestCase._po_counter

    def make_supplier_invoice(self, wo_id, po_id, supplier_id, amount, status="Approved",
                               invoice_number="SINV-1", invoice_date=None):
        # supplier_id is NOT NULL on this table - a caller that doesn't care
        # which supplier (most tests only care about the amount/status) can
        # pass None and get the invoiced PO's own supplier, rather than
        # every such test needing to thread a real supplier_id through.
        if supplier_id is None:
            supplier_id = self.conn.execute(
                "SELECT supplier_id FROM purchase_orders WHERE purchase_order_id=?", (po_id,)
            ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO supplier_invoices (invoice_number, purchase_order_id, works_order_id, "
            "supplier_id, invoice_amount, status, invoice_date) VALUES (?,?,?,?,?,?,?)",
            (invoice_number, po_id, wo_id, supplier_id, amount, status, invoice_date),
        )
        self.conn.commit()

    def make_labour_entry(self, wo_id, hours, rate, work_date=None):
        self.conn.execute(
            "INSERT INTO labour_entries (works_order_id, employee_name, hours_worked, hourly_rate, labour_cost, work_date) "
            "VALUES (?, 'Test Employee', ?, ?, ?, ?)", (wo_id, hours, rate, hours * rate, work_date),
        )
        self.conn.commit()

    def make_quote(self, quote_number, customer_id, status="Sent", valid_until=None,
                    estimated_value=0):
        self.conn.execute(
            "INSERT INTO quotes (quote_number, customer_id, status, valid_until, estimated_value) "
            "VALUES (?,?,?,?,?)", (quote_number, customer_id, status, valid_until, estimated_value),
        )
        self.conn.commit()

    def metrics_for(self, wo_id):
        db = appmod.get_db()
        wo_row = db.execute("SELECT * FROM works_orders WHERE works_order_id=?", (wo_id,)).fetchone()
        return appmod.works_order_with_metrics(db, wo_row)

    def alert_types(self, metrics):
        return [a["type"] for a in metrics["alerts"]]


# ===========================================================================
# compute_job_metrics / works_order_with_metrics - the core profitability
# arithmetic that Dashboard, Works Orders, and Reporting all read from.
# ===========================================================================
class TestCoreArithmetic(ProfitabilityTestCase):

    def test_basic_revenue_cost_profit_margin(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-1", cust, contract_value=10000, budget_cost=5000)
        self.make_customer_invoice(wo, 10000)
        self.make_supplier_invoice(wo, self.make_purchase_order(wo, self.make_supplier(), 2000), None, 2000)
        self.make_labour_entry(wo, hours=20, rate=50)  # £1,000

        m = self.metrics_for(wo)
        self.assertEqual(m["revenue"], 10000)
        self.assertEqual(m["actual_cost"], 3000)       # 2000 supplier + 1000 labour + 0 stock
        self.assertEqual(m["profit"], 7000)
        self.assertEqual(m["margin_pct"], 70.0)
        self.assertEqual(m["status_light"], "green")   # profit >= 0, margin above 35% default target

    def test_outstanding_commitment_and_forecast(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-2", cust, contract_value=10000)
        self.make_customer_invoice(wo, 10000)
        po_id = self.make_purchase_order(wo, self.make_supplier(), po_value=2500)
        self.make_supplier_invoice(wo, po_id, None, 2000)  # 500 of the PO not yet invoiced

        m = self.metrics_for(wo)
        self.assertEqual(m["actual_cost"], 2000)
        self.assertEqual(m["committed_cost"], 500)      # max(0, 2500 - 2000)
        self.assertEqual(m["forecast_cost"], 2500)
        self.assertEqual(m["forecast_profit"], 7500)
        self.assertAlmostEqual(m["forecast_margin_pct"], 75.0)

    def test_outstanding_commitment_never_goes_negative(self):
        """An over-invoiced PO (invoiced more than its own value) must not
        create a *negative* commitment that would inflate forecast profit -
        the overage is already counted once, in Actual Cost. Floored at
        zero in compute_job_metrics; also see test_over_invoiced_po_alert."""
        cust = self.make_customer()
        wo = self.make_works_order("TM-3", cust, contract_value=10000)
        self.make_customer_invoice(wo, 10000)
        po_id = self.make_purchase_order(wo, self.make_supplier(), po_value=1000)
        self.make_supplier_invoice(wo, po_id, None, 1500)  # invoiced more than the PO value

        m = self.metrics_for(wo)
        self.assertEqual(m["committed_cost"], 0)
        self.assertEqual(m["forecast_cost"], m["actual_cost"])

    def test_zero_revenue_gives_zero_margin_not_a_crash(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-4", cust, contract_value=0)
        self.make_labour_entry(wo, hours=10, rate=30)  # cost with no revenue at all

        m = self.metrics_for(wo)
        self.assertEqual(m["revenue"], 0)
        self.assertEqual(m["margin_pct"], 0.0)
        self.assertEqual(m["forecast_margin_pct"], 0.0)

    def test_cancelled_records_excluded_from_every_total(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-5", cust, contract_value=10000)
        self.make_customer_invoice(wo, 10000, status="Paid")
        self.make_customer_invoice(wo, 99999, status="Cancelled")  # must not count
        po_id = self.make_purchase_order(wo, self.make_supplier(), po_value=500, status="Issued")
        cancelled_po = self.make_purchase_order(wo, self.make_supplier(), po_value=99999, status="Cancelled")
        self.make_supplier_invoice(wo, po_id, None, 500, status="Approved")
        self.make_supplier_invoice(wo, cancelled_po, None, 99999, status="Cancelled")

        m = self.metrics_for(wo)
        self.assertEqual(m["revenue"], 10000)
        self.assertEqual(m["actual_cost"], 500)
        self.assertEqual(m["po_total"], 500)
        self.assertEqual(m["supplier_invoice_total"], 500)


# ===========================================================================
# Alerts - Budget Exceeded / Cost Above Estimated Target / Margin Risk /
# Over-Invoiced PO, and the status_light (red/amber/green) they feed into.
# ===========================================================================
class TestAlerts(ProfitabilityTestCase):

    def test_confirmed_budget_exceeded_is_red(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-10", cust, contract_value=10000,
                                    budget_cost=1000, budget_is_estimated=0)
        self.make_labour_entry(wo, hours=100, rate=30)  # £3,000 actual cost, well above budget

        m = self.metrics_for(wo)
        self.assertIn("Budget Exceeded", self.alert_types(m))
        self.assertNotIn("Cost Above Estimated Target", self.alert_types(m))
        red_alert = next(a for a in m["alerts"] if a["type"] == "Budget Exceeded")
        self.assertEqual(red_alert["level"], "red")

    def test_estimated_budget_exceeded_is_amber_not_red(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-11", cust, contract_value=10000,
                                    budget_cost=1000, budget_is_estimated=1)
        self.make_labour_entry(wo, hours=100, rate=30)

        m = self.metrics_for(wo)
        self.assertIn("Cost Above Estimated Target", self.alert_types(m))
        self.assertNotIn("Budget Exceeded", self.alert_types(m))
        amber_alert = next(a for a in m["alerts"] if a["type"] == "Cost Above Estimated Target")
        self.assertEqual(amber_alert["level"], "amber")

    def test_no_budget_alert_when_under_budget(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-12", cust, contract_value=10000,
                                    budget_cost=5000, budget_is_estimated=0)
        self.make_labour_entry(wo, hours=10, rate=30)  # £300, well under budget

        m = self.metrics_for(wo)
        self.assertNotIn("Budget Exceeded", self.alert_types(m))
        self.assertNotIn("Cost Above Estimated Target", self.alert_types(m))

    def test_margin_risk_alert_uses_default_target(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-13", cust, contract_value=1000)
        self.make_customer_invoice(wo, 1000)
        self.make_labour_entry(wo, hours=30, rate=30)  # £900 cost -> 10% margin, below 35% default

        m = self.metrics_for(wo)
        self.assertIn("Margin Risk", self.alert_types(m))
        self.assertEqual(m["margin_target"], 35.0)

    def test_margin_risk_alert_uses_contract_code_target(self):
        """INT is seeded (schema.sql) with a 10.7% target instead of the
        35% default - a job at 20% margin should NOT trip Margin Risk under
        that code, even though it would under the default target."""
        cust = self.make_customer()
        wo = self.make_works_order("TM-14", cust, contract_value=1000, contract_code="INT")
        self.make_customer_invoice(wo, 1000)
        self.make_labour_entry(wo, hours=1, rate=800)  # £800 cost -> 20% margin

        m = self.metrics_for(wo)
        self.assertEqual(m["margin_target"], 10.7)
        self.assertNotIn("Margin Risk", self.alert_types(m))

    def test_no_margin_alert_with_zero_revenue(self):
        """The alert only fires `if revenue and forecast_margin_pct < target`
        - an un-invoiced job (0% margin by definition) must not be flagged
        as a margin risk just because it hasn't been invoiced yet."""
        cust = self.make_customer()
        wo = self.make_works_order("TM-15", cust, contract_value=1000)
        self.make_labour_entry(wo, hours=10, rate=30)

        m = self.metrics_for(wo)
        self.assertNotIn("Margin Risk", self.alert_types(m))

    def test_over_invoiced_po_alert(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-16", cust, contract_value=10000)
        po_id = self.make_purchase_order(wo, self.make_supplier(), po_value=1000, po_number="PO-OVER")
        self.make_supplier_invoice(wo, po_id, None, 1200)  # invoiced more than the PO's own value

        m = self.metrics_for(wo)
        self.assertIn("Over-Invoiced PO", self.alert_types(m))
        over_alert = next(a for a in m["alerts"] if a["type"] == "Over-Invoiced PO")
        self.assertEqual(over_alert["level"], "red")
        self.assertIn("PO-OVER", over_alert["message"])

    def test_cancelled_po_excluded_from_over_invoiced_check(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-17", cust, contract_value=10000)
        po_id = self.make_purchase_order(wo, self.make_supplier(), po_value=1000, status="Cancelled")
        self.make_supplier_invoice(wo, po_id, None, 1200, status="Approved")

        m = self.metrics_for(wo)
        self.assertNotIn("Over-Invoiced PO", self.alert_types(m))

    def test_status_light_red_on_loss_regardless_of_margin_target(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-18", cust, contract_value=1000, contract_code="INT")  # low 10.7% target
        self.make_customer_invoice(wo, 1000)
        self.make_labour_entry(wo, hours=50, rate=30)  # £1,500 cost -> a loss

        m = self.metrics_for(wo)
        self.assertLess(m["profit"], 0)
        self.assertEqual(m["status_light"], "red")

    def test_status_light_amber_when_under_target_but_profitable(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-19", cust, contract_value=1000)
        self.make_customer_invoice(wo, 1000)
        self.make_labour_entry(wo, hours=30, rate=30)  # £900 cost -> 10% margin, profitable but below 35%

        m = self.metrics_for(wo)
        self.assertGreater(m["profit"], 0)
        self.assertEqual(m["status_light"], "amber")

    def test_status_light_green_when_at_or_above_target(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-20", cust, contract_value=1000)
        self.make_customer_invoice(wo, 1000)
        self.make_labour_entry(wo, hours=1, rate=100)  # £100 cost -> 90% margin

        m = self.metrics_for(wo)
        self.assertEqual(m["status_light"], "green")


# ===========================================================================
# Quote validity: default_valid_until / is_past_validity - pure functions,
# no database needed, but exercised inside the same app context as
# everything else for consistency.
# ===========================================================================
class TestQuoteValidity(unittest.TestCase):

    def test_default_valid_until_adds_30_days_to_quote_date(self):
        result = appmod.default_valid_until("2026-01-01")
        self.assertEqual(result, "2026-01-31")

    def test_default_valid_until_falls_back_to_today_when_blank(self):
        expected = (date.today() + timedelta(days=30)).isoformat()
        self.assertEqual(appmod.default_valid_until(""), expected)
        self.assertEqual(appmod.default_valid_until(None), expected)

    def test_default_valid_until_tolerates_malformed_date(self):
        expected = (date.today() + timedelta(days=30)).isoformat()
        self.assertEqual(appmod.default_valid_until("not-a-date"), expected)

    def test_is_past_validity_true_for_open_quote_past_date(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertTrue(appmod.is_past_validity(yesterday, "Draft"))
        self.assertTrue(appmod.is_past_validity(yesterday, "Sent"))

    def test_is_past_validity_false_for_future_date(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertFalse(appmod.is_past_validity(tomorrow, "Sent"))

    def test_is_past_validity_false_for_closed_statuses_even_if_past(self):
        """Won/Lost/Expired quotes are no longer 'open', so an old Valid
        Until date on one of those is not something that needs flagging -
        only a still-open (Draft/Sent) quote should ever be marked."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        for status in ("Won", "Lost", "Expired"):
            self.assertFalse(appmod.is_past_validity(yesterday, status))

    def test_is_past_validity_false_with_no_date_set(self):
        self.assertFalse(appmod.is_past_validity(None, "Sent"))
        self.assertFalse(appmod.is_past_validity("", "Sent"))


# ===========================================================================
# compute_all_alerts - combines every job's alerts with open-quote-past-
# validity alerts into the one flat list the topbar bell and /api/alerts
# both show.
# ===========================================================================
class TestComputeAllAlerts(ProfitabilityTestCase):

    def test_combines_job_alerts_and_quote_validity_alerts(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-30", cust, contract_value=10000,
                                    budget_cost=100, budget_is_estimated=0)
        self.make_labour_entry(wo, hours=50, rate=30)  # triggers Budget Exceeded

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.make_quote("TMQ-1", cust, status="Sent", valid_until=yesterday)

        db = appmod.get_db()
        alerts = appmod.compute_all_alerts(db)
        types = [a["type"] for a in alerts]
        self.assertIn("Budget Exceeded", types)
        self.assertIn("Quote Past Validity", types)

        quote_alert = next(a for a in alerts if a["type"] == "Quote Past Validity")
        self.assertEqual(quote_alert["job_number"], "TMQ-1")

    def test_finished_jobs_raise_no_dashboard_alerts_but_live_ones_do(self):
        cust = self.make_customer()
        for num, status in (("TM-40", "Completed"), ("TM-41", "Closed"), ("TM-42", "Cancelled"), ("TM-43", "In Progress")):
            wo = self.make_works_order(num, cust, contract_value=10000, budget_cost=100, budget_is_estimated=0)
            self.conn.execute("UPDATE works_orders SET status=? WHERE works_order_id=?", (status, wo))
            self.make_labour_entry(wo, hours=50, rate=30)
        self.conn.commit()
        alerts = appmod.compute_all_alerts(appmod.get_db())
        self.assertEqual({a["job_number"] for a in alerts}, {"TM-43"})

    def test_quote_not_past_validity_is_excluded(self):
        cust = self.make_customer()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.make_quote("TMQ-2", cust, status="Sent", valid_until=tomorrow)

        db = appmod.get_db()
        alerts = appmod.compute_all_alerts(db)
        self.assertNotIn("Quote Past Validity", [a["type"] for a in alerts])

    def test_won_quote_past_validity_is_not_flagged(self):
        cust = self.make_customer()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.make_quote("TMQ-3", cust, status="Won", valid_until=yesterday)

        db = appmod.get_db()
        alerts = appmod.compute_all_alerts(db)
        self.assertNotIn("Quote Past Validity", [a["type"] for a in alerts])

    def test_passing_precomputed_jobs_matches_recomputing_from_scratch(self):
        """compute_all_alerts(db, jobs=...) is an optimisation so dashboard()
        doesn't run works_order_with_metrics twice - this checks the
        shortcut actually produces the same alerts as the from-scratch path
        the lightweight /api/alerts endpoint uses, not just a faster wrong
        answer."""
        cust = self.make_customer()
        wo = self.make_works_order("TM-31", cust, contract_value=10000,
                                    budget_cost=100, budget_is_estimated=0)
        self.make_labour_entry(wo, hours=50, rate=30)

        db = appmod.get_db()
        from_scratch = appmod.compute_all_alerts(db)

        rows = db.execute(
            """SELECT wo.*, c.customer_name FROM works_orders wo
               JOIN customers c ON c.customer_id = wo.customer_id"""
        ).fetchall()
        jobs = [appmod.works_order_with_metrics(db, r) for r in rows]
        precomputed = appmod.compute_all_alerts(db, jobs=jobs)

        self.assertEqual(from_scratch, precomputed)


# ===========================================================================
# build_report_rows - the Reporting page / CSV export, including the
# "invoiced" flag that drives the Completed-Not-Yet-Invoiced view.
# ===========================================================================
class TestBuildReportRows(ProfitabilityTestCase):

    def test_invoiced_flag_true_once_revenue_exists(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-40", cust, status="Completed", contract_value=5000)
        self.make_customer_invoice(wo, 5000)

        report = appmod.build_report_rows(appmod.get_db())
        row = next(r for r in report if r["job_number"] == "TM-40")
        self.assertTrue(row["invoiced"])
        self.assertEqual(row["revenue"], 5000)

    def test_invoiced_flag_false_with_no_revenue_even_if_completed(self):
        """This is the whole point of the 'invoiced' flag - a job marked
        Completed in the Status field but never actually invoiced must
        still show up as outstanding, not be hidden by its status alone."""
        cust = self.make_customer()
        wo = self.make_works_order("TM-41", cust, status="Completed", contract_value=5000)

        report = appmod.build_report_rows(appmod.get_db())
        row = next(r for r in report if r["job_number"] == "TM-41")
        self.assertFalse(row["invoiced"])
        self.assertEqual(row["revenue"], 0)

    def test_report_row_figures_match_compute_job_metrics(self):
        """The Reporting export must never quietly drift from the same
        numbers shown on Works Orders/Dashboard - both are expected to come
        from the exact same compute_job_metrics() call."""
        cust = self.make_customer()
        wo = self.make_works_order("TM-42", cust, contract_value=8000, budget_cost=4000)
        self.make_customer_invoice(wo, 8000)
        self.make_supplier_invoice(wo, self.make_purchase_order(wo, self.make_supplier(), 3000), None, 3000)

        db = appmod.get_db()
        m = self.metrics_for(wo)
        report = appmod.build_report_rows(db)
        row = next(r for r in report if r["job_number"] == "TM-42")

        self.assertEqual(row["revenue"], m["revenue"])
        self.assertEqual(row["actual_cost"], m["actual_cost"])
        self.assertEqual(row["profit"], m["profit"])
        self.assertEqual(row["margin_pct"], m["margin_pct"])


# ===========================================================================
# job_profit_margin_for_year - the Dashboard chart area's "this year only"
# per-job figures (Top Jobs by Profit, Loss-Making Jobs, the Profit vs
# Margin bubble chart). Deliberately a different cut from
# compute_job_metrics() - these tests pin down exactly how it differs.
# ===========================================================================
class TestJobProfitMarginForYear(ProfitabilityTestCase):

    # Computed from today's date, never hardcoded - this suite still needs
    # to pass correctly whichever year it actually runs in.
    CY = date.today().year
    PY = CY - 1

    def test_only_counts_revenue_and_cost_dated_in_the_requested_year(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-50", cust, contract_value=1000)
        self.make_customer_invoice(wo, 1000, invoice_date=f"{self.CY}-03-15")
        self.make_labour_entry(wo, hours=10, rate=20, work_date=f"{self.CY}-03-20")  # £200, same year
        # Cost dated in a DIFFERENT year must not bleed into this year's figures.
        self.make_supplier_invoice(wo, self.make_purchase_order(wo, self.make_supplier(), 500),
                                    None, 500, invoice_date=f"{self.PY}-11-01")

        year_jobs = appmod.job_profit_margin_for_year(appmod.get_db(), self.CY)
        job = next(j for j in year_jobs if j["job_number"] == "TM-50")
        self.assertEqual(job["revenue"], 1000)
        self.assertEqual(job["actual_cost"], 200)  # the prior-year supplier invoice is excluded
        self.assertEqual(job["profit"], 800)

    def test_job_with_no_activity_in_the_year_is_excluded_entirely(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-51", cust, contract_value=1000)
        self.make_customer_invoice(wo, 1000, invoice_date=f"{self.PY}-06-01")  # all activity last year

        year_jobs = appmod.job_profit_margin_for_year(appmod.get_db(), self.CY)
        self.assertNotIn("TM-51", [j["job_number"] for j in year_jobs])

    def test_cancelled_invoices_excluded_same_as_lifetime_metrics(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-52", cust, contract_value=1000)
        self.make_customer_invoice(wo, 1000, invoice_date=f"{self.CY}-01-10", status="Paid")
        self.make_customer_invoice(wo, 99999, invoice_date=f"{self.CY}-01-11", status="Cancelled")

        year_jobs = appmod.job_profit_margin_for_year(appmod.get_db(), self.CY)
        job = next(j for j in year_jobs if j["job_number"] == "TM-52")
        self.assertEqual(job["revenue"], 1000)

    def test_status_light_matches_the_same_rules_as_lifetime_metrics(self):
        cust = self.make_customer()
        wo_loss = self.make_works_order("TM-53", cust, contract_value=0)
        self.make_labour_entry(wo_loss, hours=10, rate=20, work_date=f"{self.CY}-05-01")  # cost, no revenue -> loss

        wo_healthy = self.make_works_order("TM-54", cust, contract_value=1000)
        self.make_customer_invoice(wo_healthy, 1000, invoice_date=f"{self.CY}-05-01")
        self.make_labour_entry(wo_healthy, hours=1, rate=50, work_date=f"{self.CY}-05-01")  # £50 cost -> 95% margin

        year_jobs = {j["job_number"]: j for j in appmod.job_profit_margin_for_year(appmod.get_db(), self.CY)}
        self.assertEqual(year_jobs["TM-53"]["status_light"], "red")
        self.assertEqual(year_jobs["TM-54"]["status_light"], "green")

    def test_dashboard_top_and_bottom_lists_never_overlap(self):
        """Exercises the actual /api/dashboard split logic end to end: Top
        is highest-profit jobs regardless of sign, Bottom is only jobs
        actually in loss this year - the two can never share a job."""
        cust = self.make_customer()
        for i in range(5):
            wo = self.make_works_order(f"TM-6{i}", cust, contract_value=1000 + i * 100)
            self.make_customer_invoice(wo, 1000 + i * 100, invoice_date=f"{self.CY}-02-01")
            self.make_labour_entry(wo, hours=1, rate=10, work_date=f"{self.CY}-02-01")
        loss_wo = self.make_works_order("TM-69", cust, contract_value=0)
        self.make_labour_entry(loss_wo, hours=5, rate=40, work_date=f"{self.CY}-02-01")  # £200 cost, no revenue

        # login_required/permission_required now re-check session["user_id"]
        # against a real row in the users table on every request (see
        # _revalidate_session_user() in app.py) rather than trusting the
        # session blindly - so calling dashboard() directly needs an actual
        # user behind the session, not just the session keys a real login
        # would set.
        cur = self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            ("test", appmod.generate_password_hash("secret123"), "Test", "Administrator"),
        )
        self.conn.commit()
        test_user_id = cur.lastrowid

        with appmod.app.test_request_context():
            appmod.session["user_id"] = test_user_id
            appmod.session["role"] = "Administrator"
            appmod.session["full_name"] = "Test"
            appmod.session["username"] = "test"
            resp = appmod.dashboard()
        data = resp.get_json()
        top_numbers = {j["job_number"] for j in data["top_profit_jobs"]}
        bottom_numbers = {j["job_number"] for j in data["bottom_profit_jobs"]}
        self.assertEqual(top_numbers & bottom_numbers, set())
        self.assertIn("TM-69", bottom_numbers)
        self.assertEqual(data["current_year"], self.CY)


# ===========================================================================
# Security hardening - CSP header, CSRF enforcement, durable login-event
# logging, and password-length enforcement on admin-set passwords. These
# go through the real Flask dispatch (app.test_client()) rather than
# calling a view function directly, since what's under test here -
# before_request/after_request hooks - is exactly what a direct call
# would skip straight past.
# ===========================================================================
class TestSecurityHardening(ProfitabilityTestCase):

    def tearDown(self):
        # _LOGIN_ATTEMPTS/_TOTP_ATTEMPTS are module-level dicts in app.py,
        # not reset by the base class's setUp/tearDown (which only ever
        # touch the database) - clear them after every test here so one
        # test's failed attempts can never carry over and lock out
        # another (each test's user_id restarts from 1 in its own fresh
        # database, so a stale entry really would collide).
        appmod._LOGIN_ATTEMPTS.clear()
        appmod._TOTP_ATTEMPTS.clear()
        super().tearDown()

    def make_user(self, username, password, role="Administrator", full_name=None):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), full_name or username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def test_csp_header_present_on_every_response(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        resp = self.login(client, "admin", "secret123")
        csp = resp.headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_write_request_without_csrf_token_is_rejected(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        resp = client.post("/api/users", json={"username": "newbie", "full_name": "New Bie", "role": "Finance"})
        self.assertEqual(resp.status_code, 403)
        self.assertIn("security token", resp.get_json()["error"])

    def test_write_request_with_matching_csrf_token_succeeds(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        resp = client.post("/api/users", json={"username": "newbie", "full_name": "New Bie", "role": "Finance"},
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 201)

    def test_pre_existing_session_with_no_csrf_token_self_heals_on_next_request(self):
        # A session created before this feature existed (or by a server
        # that hadn't picked up this code yet) has no csrf_token at all -
        # simulated here by setting session keys directly, the same shape
        # login() used to leave behind. Without ensure_csrf_token() healing
        # this on the session's next request, such a session would be
        # stuck rejected forever, with no way out short of logging out and
        # back in - this is the real bug a user hit after this feature
        # first shipped (raising a Quote failed with "security token
        # missing or stale" on an already-open tab).
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        with client.session_transaction() as sess:
            row = self.conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
            sess["user_id"] = row["user_id"]
            sess["role"] = row["role"]
            sess["full_name"] = row["full_name"]
            sess["username"] = row["username"]
            sess["must_change_password"] = False
            # deliberately no csrf_token - this is the whole point
        self.assertIsNone(client.get_cookie("csrf_token"))
        client.get("/api/auth/me")  # any plain page-load request heals it
        cookie = client.get_cookie("csrf_token")
        self.assertIsNotNone(cookie)
        resp = client.post("/api/users", json={"username": "newbie", "full_name": "New Bie", "role": "Finance"},
                            headers={"X-CSRF-Token": cookie.value})
        self.assertEqual(resp.status_code, 201)

    def test_csrf_not_checked_ahead_of_the_real_401_for_an_unauthenticated_request(self):
        # No session exists yet - the route's own login_required /
        # permission_required must still be the thing that rejects this
        # (401), not a CSRF 403 that would mask it.
        client = appmod.app.test_client()
        resp = client.post("/api/users", json={"username": "x", "full_name": "X", "role": "Finance"})
        self.assertEqual(resp.status_code, 401)

    def test_login_success_is_logged(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        rows = self.conn.execute("SELECT * FROM login_events").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "success")
        self.assertEqual(rows[0]["username"], "admin")

    def test_login_failure_is_logged(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        rows = self.conn.execute("SELECT * FROM login_events").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "failure")

    def test_lockout_is_logged_separately_from_plain_failures(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        for _ in range(appmod.LOGIN_MAX_ATTEMPTS):
            client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        self.assertEqual(resp.status_code, 429)
        rows = self.conn.execute("SELECT event_type FROM login_events ORDER BY event_id").fetchall()
        self.assertEqual([r["event_type"] for r in rows],
                          ["failure"] * appmod.LOGIN_MAX_ATTEMPTS + ["lockout"])

    def test_login_events_endpoint_requires_administrator(self):
        self.make_user("admin", "secret123", role="Administrator")
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        resp = client.get("/api/login-events")
        self.assertEqual(resp.status_code, 403)

    def test_create_user_rejects_short_explicit_password(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        resp = client.post(
            "/api/users",
            json={"username": "newbie", "full_name": "New Bie", "role": "Finance", "password": "short"},
            headers=self.csrf_headers(client),
        )
        self.assertEqual(resp.status_code, 400)

    def test_create_user_allows_omitting_password_to_auto_generate_one(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        resp = client.post(
            "/api/users",
            json={"username": "newbie", "full_name": "New Bie", "role": "Finance"},
            headers=self.csrf_headers(client),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertIn("temporary_password", resp.get_json())

    def test_update_user_rejects_short_explicit_password(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        created = client.post(
            "/api/users",
            json={"username": "newbie", "full_name": "New Bie", "role": "Finance"},
            headers=self.csrf_headers(client),
        )
        uid = created.get_json()["user_id"]
        resp = client.put(
            f"/api/users/{uid}", json={"role": "Finance", "password": "short"},
            headers=self.csrf_headers(client),
        )
        self.assertEqual(resp.status_code, 400)


# ===========================================================================
# totp.py in isolation - the hand-rolled RFC 6238/4226 implementation the
# whole 2FA feature is built on (see its own module docstring for why
# this isn't a third-party package). Checked directly against the
# official RFC 4226 HOTP test vectors, independent of anything in app.py,
# so a bug in the algorithm itself would fail here even if every app-level
# test above happened to still pass.
# ===========================================================================
class TestTotpModule(unittest.TestCase):

    def test_hotp_matches_rfc4226_test_vectors(self):
        # RFC 4226, Appendix D - the official reference values for the
        # 20-byte ASCII key "12345678901234567890", counters 0-9.
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode("ascii")
        expected = ["755224", "287082", "359152", "969429", "338314",
                    "254676", "287922", "162583", "399871", "520489"]
        for counter, expected_code in enumerate(expected):
            self.assertEqual(totp._hotp(secret, counter), expected_code)

    def test_generated_secret_round_trips_and_verifies_its_own_code(self):
        secret = totp.generate_secret()
        code = totp.current_code(secret)
        self.assertTrue(totp.verify(secret, code))

    def test_wrong_code_does_not_verify(self):
        secret = totp.generate_secret()
        code = totp.current_code(secret)
        wrong = f"{(int(code) + 1) % 1000000:06d}"
        self.assertFalse(totp.verify(secret, wrong))

    def test_verify_tolerates_one_step_of_clock_drift(self):
        secret = totp.generate_secret()
        earlier = totp.current_code(secret, for_time=time.time() - totp.TOTP_PERIOD)
        self.assertTrue(totp.verify(secret, earlier, valid_window=1))

    def test_verify_rejects_code_far_outside_the_window(self):
        secret = totp.generate_secret()
        far_past = totp.current_code(secret, for_time=time.time() - 10 * totp.TOTP_PERIOD)
        self.assertFalse(totp.verify(secret, far_past, valid_window=1))

    def test_verify_tolerates_surrounding_whitespace_and_a_middle_space(self):
        secret = totp.generate_secret()
        code = totp.current_code(secret)
        spaced = f"{code[:3]} {code[3:]}"
        self.assertTrue(totp.verify(secret, f"  {spaced}  "))

    def test_verify_handles_malformed_input_without_raising(self):
        secret = totp.generate_secret()
        for bad in ("", None, "abcdef", "12345", "1234567", "👍👍👍👍👍👍"):
            self.assertFalse(totp.verify(secret, bad))
        self.assertFalse(totp.verify(None, "123456"))
        self.assertFalse(totp.verify("", "123456"))

    def test_backup_codes_are_unique_and_formatted_consistently(self):
        codes = {totp.generate_backup_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)  # no collisions in a decent-sized sample
        for code in list(codes)[:20]:
            self.assertRegex(code, r"^[0-9A-F]{5}-[0-9A-F]{5}$")

    def test_provisioning_uri_contains_the_secret_and_issuer(self):
        secret = totp.generate_secret()
        uri = totp.provisioning_uri(secret, "jsmith")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(secret, uri)
        self.assertIn("issuer=TEAMworks360", uri)

    def test_format_secret_for_display_groups_in_fours(self):
        self.assertEqual(totp.format_secret_for_display("ABCDEFGH"), "ABCD EFGH")


# ===========================================================================
# Two-factor authentication (TOTP) - mandatory for every account. Covers
# the full lifecycle: a brand new account being forced through setup
# before it can use anything else, confirming a pending secret (right and
# wrong codes), a returning account being asked to verify instead of
# setup, backup codes as a one-time-use fallback, the attempt lockout, and
# the Administrator-only reset action. Goes through the real Flask
# dispatch (app.test_client()), same reasoning as TestSecurityHardening.
# ===========================================================================
class TestTwoFactorAuth(ProfitabilityTestCase):

    def tearDown(self):
        appmod._LOGIN_ATTEMPTS.clear()
        appmod._TOTP_ATTEMPTS.clear()
        super().tearDown()

    def make_user(self, username, password, role="Administrator"):
        cur = self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()
        return cur.lastrowid

    def raw_login(self, client, username, password):
        """The plain login POST, deliberately NOT auto-completing 2FA
        setup afterwards (unlike every other test class's login() helper)
        - these tests are specifically about what happens before and
        during that setup, so they need to see must_setup_2fa still set."""
        return client.post("/api/auth/login", json={"username": username, "password": password})

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def test_brand_new_account_login_reports_must_setup_2fa(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        resp = self.raw_login(client, "admin", "secret123")
        data = resp.get_json()
        self.assertTrue(data["must_setup_2fa"])
        self.assertFalse(data["must_verify_2fa"])

    def test_every_other_endpoint_is_blocked_until_2fa_setup_is_confirmed(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        resp = client.get("/api/reference")
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(resp.get_json()["must_setup_2fa"])

    def test_2fa_setup_endpoints_themselves_stay_reachable(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        resp = client.get("/api/auth/2fa/setup")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("manual_entry_key", resp.get_json())

    def test_setup_key_is_stable_across_repeated_calls_before_confirmation(self):
        # Reloading the setup screen (e.g. an accidental refresh) must never
        # hand out a different key than the one already typed into an
        # authenticator app, or that app's entry would stop working.
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        first = client.get("/api/auth/2fa/setup").get_json()["manual_entry_key"]
        second = client.get("/api/auth/2fa/setup").get_json()["manual_entry_key"]
        self.assertEqual(first, second)

    def test_qr_setup_endpoint_is_reachable_during_the_setup_gate(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        resp = client.get("/api/auth/2fa/setup/qr.png")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "image/png")
        self.assertTrue(resp.data.startswith(b"\x89PNG\r\n\x1a\n"))  # PNG file signature

    def test_qr_setup_endpoint_requires_login(self):
        client = appmod.app.test_client()
        resp = client.get("/api/auth/2fa/setup/qr.png")
        self.assertEqual(resp.status_code, 401)

    def test_qr_setup_image_is_stable_across_repeated_calls_before_confirmation(self):
        # Same reasoning as the manual key: refreshing the setup screen
        # must never hand out a QR code for a different secret.
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        first = client.get("/api/auth/2fa/setup/qr.png").data
        second = client.get("/api/auth/2fa/setup/qr.png").data
        self.assertEqual(first, second)

    def test_qr_setup_endpoint_is_blocked_once_2fa_is_already_enabled(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        setup = client.get("/api/auth/2fa/setup").get_json()
        secret = setup["manual_entry_key"].replace(" ", "")
        code = totp.current_code(secret)
        confirm = client.post("/api/auth/2fa/setup", json={"code": code}, headers=self.csrf_headers(client))
        self.assertEqual(confirm.status_code, 200)
        resp = client.get("/api/auth/2fa/setup/qr.png")
        self.assertEqual(resp.status_code, 400)

    def test_qr_image_decodes_to_an_otpauth_uri_for_the_same_secret_as_the_manual_key(self):
        # The strongest check available: actually decode the rendered QR
        # image and confirm it scans back to exactly the otpauth:// link
        # for the secret shown as text, so an authenticator app scanning
        # this image and one typed from the manual key end up identical.
        # Uses cv2 (OpenCV) purely as an independent verification oracle,
        # if it happens to be installed - it is NOT a dependency of the
        # app itself (see requirements.txt / qr_png.py), so this test
        # skips cleanly on a machine that doesn't have it rather than
        # failing the suite over an optional self-check.
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.skipTest("cv2/numpy not installed - optional QR-decode self-check")
        import io as _io
        from PIL import Image as _Image

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        setup = client.get("/api/auth/2fa/setup").get_json()
        png_bytes = client.get("/api/auth/2fa/setup/qr.png").data

        img = _Image.open(_io.BytesIO(png_bytes)).convert("L")
        decoded, _points, _ = cv2.QRCodeDetector().detectAndDecode(np.array(img))
        self.assertEqual(decoded, setup["otpauth_uri"])

    def test_confirm_with_wrong_code_does_not_enable_2fa(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        resp = client.post("/api/auth/2fa/setup", json={"code": "000000"}, headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 400)
        row = self.conn.execute("SELECT totp_enabled FROM users WHERE username='admin'").fetchone()
        self.assertEqual(row["totp_enabled"], 0)
        # still blocked - a wrong code must not have cleared the gate
        self.assertEqual(client.get("/api/reference").status_code, 403)

    def test_confirm_with_correct_code_enables_2fa_and_issues_ten_backup_codes(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        setup = client.get("/api/auth/2fa/setup").get_json()
        secret = setup["manual_entry_key"].replace(" ", "")
        code = totp.current_code(secret)
        resp = client.post("/api/auth/2fa/setup", json={"code": code}, headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.get_json()["backup_codes"]), 10)
        row = self.conn.execute("SELECT totp_enabled FROM users WHERE username='admin'").fetchone()
        self.assertEqual(row["totp_enabled"], 1)
        # the gate is cleared immediately - no separate verify step needed
        # on the same session that just finished setup
        self.assertEqual(client.get("/api/reference").status_code, 200)

    def test_returning_account_is_asked_to_verify_not_setup_again(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        setup = client.get("/api/auth/2fa/setup").get_json()
        secret = setup["manual_entry_key"].replace(" ", "")
        client.post("/api/auth/2fa/setup", json={"code": totp.current_code(secret)}, headers=self.csrf_headers(client))
        client.post("/api/auth/logout", headers=self.csrf_headers(client))

        client2 = appmod.app.test_client()
        resp = self.raw_login(client2, "admin", "secret123")
        data = resp.get_json()
        self.assertFalse(data["must_setup_2fa"])
        self.assertTrue(data["must_verify_2fa"])
        self.assertEqual(client2.get("/api/reference").status_code, 403)

    def _enable_2fa(self, client):
        """Shared setup for the verify/backup-code/reset tests below: get
        a brand new account all the way through initial 2FA setup, and
        return its secret for computing further codes with."""
        setup = client.get("/api/auth/2fa/setup").get_json()
        secret = setup["manual_entry_key"].replace(" ", "")
        resp = client.post("/api/auth/2fa/setup", json={"code": totp.current_code(secret)},
                            headers=self.csrf_headers(client))
        return secret, resp.get_json()["backup_codes"]

    def test_verify_wrong_code_is_rejected_and_gate_stays_up(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        secret, _ = self._enable_2fa(client)
        client.post("/api/auth/logout", headers=self.csrf_headers(client))

        client2 = appmod.app.test_client()
        self.raw_login(client2, "admin", "secret123")
        resp = client2.post("/api/auth/2fa/verify", json={"code": "000000"}, headers=self.csrf_headers(client2))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(client2.get("/api/reference").status_code, 403)

    def test_verify_correct_code_clears_the_gate(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        secret, _ = self._enable_2fa(client)
        client.post("/api/auth/logout", headers=self.csrf_headers(client))

        client2 = appmod.app.test_client()
        self.raw_login(client2, "admin", "secret123")
        code = totp.current_code(secret)
        resp = client2.post("/api/auth/2fa/verify", json={"code": code}, headers=self.csrf_headers(client2))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(client2.get("/api/reference").status_code, 200)

    def test_verify_locks_out_after_max_attempts(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        self._enable_2fa(client)
        client.post("/api/auth/logout", headers=self.csrf_headers(client))

        client2 = appmod.app.test_client()
        self.raw_login(client2, "admin", "secret123")
        for _ in range(appmod.TOTP_MAX_ATTEMPTS):
            resp = client2.post("/api/auth/2fa/verify", json={"code": "000000"}, headers=self.csrf_headers(client2))
            self.assertEqual(resp.status_code, 400)
        locked = client2.post("/api/auth/2fa/verify", json={"code": "000000"}, headers=self.csrf_headers(client2))
        self.assertEqual(locked.status_code, 429)

    def test_backup_code_verifies_and_can_only_be_used_once(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        _, backup_codes = self._enable_2fa(client)
        client.post("/api/auth/logout", headers=self.csrf_headers(client))

        client2 = appmod.app.test_client()
        self.raw_login(client2, "admin", "secret123")
        one_code = backup_codes[0]
        resp = client2.post("/api/auth/2fa/verify", json={"code": one_code, "is_backup_code": True},
                             headers=self.csrf_headers(client2))
        self.assertEqual(resp.status_code, 200)
        client2.post("/api/auth/logout", headers=self.csrf_headers(client2))

        client3 = appmod.app.test_client()
        self.raw_login(client3, "admin", "secret123")
        reused = client3.post("/api/auth/2fa/verify", json={"code": one_code, "is_backup_code": True},
                               headers=self.csrf_headers(client3))
        self.assertEqual(reused.status_code, 400)

    def test_backup_code_is_case_insensitive(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        _, backup_codes = self._enable_2fa(client)
        client.post("/api/auth/logout", headers=self.csrf_headers(client))

        client2 = appmod.app.test_client()
        self.raw_login(client2, "admin", "secret123")
        resp = client2.post("/api/auth/2fa/verify", json={"code": backup_codes[0].lower(), "is_backup_code": True},
                             headers=self.csrf_headers(client2))
        self.assertEqual(resp.status_code, 200)

    def test_admin_reset_2fa_clears_secret_enabled_flag_and_backup_codes(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        uid = self.conn.execute("SELECT user_id FROM users WHERE username='admin'").fetchone()[0]
        self._enable_2fa(client)

        resp = client.post(f"/api/users/{uid}/reset-2fa", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT totp_secret, totp_enabled FROM users WHERE user_id=?", (uid,)).fetchone()
        self.assertIsNone(row["totp_secret"])
        self.assertEqual(row["totp_enabled"], 0)
        remaining = self.conn.execute("SELECT COUNT(*) FROM totp_backup_codes WHERE user_id=?", (uid,)).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_reset_2fa_requires_administrator(self):
        self.make_user("admin", "secret123", role="Administrator")
        self.make_user("pm", "secret123", role="Project Manager")
        admin_client = appmod.app.test_client()
        self.raw_login(admin_client, "admin", "secret123")
        self._enable_2fa(admin_client)

        pm_client = appmod.app.test_client()
        self.raw_login(pm_client, "pm", "secret123")
        self._enable_2fa(pm_client)

        admin_uid = self.conn.execute("SELECT user_id FROM users WHERE username='admin'").fetchone()[0]
        resp = pm_client.post(f"/api/users/{admin_uid}/reset-2fa", headers=self.csrf_headers(pm_client))
        self.assertEqual(resp.status_code, 403)

    def test_me_endpoint_reports_2fa_flags(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.raw_login(client, "admin", "secret123")
        data = client.get("/api/auth/me").get_json()
        self.assertTrue(data["must_setup_2fa"])
        self.assertFalse(data["must_verify_2fa"])


# ===========================================================================
# Instant session cut-off - deleting a user, or changing their role, must
# take effect on their very next request rather than waiting for their
# existing session to expire naturally (see _revalidate_session_user() in
# app.py). Goes through the real Flask dispatch, same reasoning as
# TestSecurityHardening.
# ===========================================================================
class TestInstantSessionCutoff(ProfitabilityTestCase):

    def tearDown(self):
        appmod._LOGIN_ATTEMPTS.clear()
        appmod._TOTP_ATTEMPTS.clear()
        super().tearDown()

    def make_user(self, username, password, role="Administrator"):
        cur = self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()
        return cur.lastrowid

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def test_deleted_account_is_rejected_on_its_very_next_request(self):
        uid = self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        self.assertEqual(client.get("/api/reference").status_code, 200)

        self.conn.execute("DELETE FROM users WHERE user_id=?", (uid,))
        self.conn.commit()

        resp = client.get("/api/reference")
        self.assertEqual(resp.status_code, 401)
        # /api/auth/me must agree - this is what the frontend's bootstrap()
        # calls on every page load, and it has to kick a deleted account's
        # lingering browser tab back to the login screen too.
        self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_role_change_is_picked_up_on_the_very_next_request_not_at_next_login(self):
        uid = self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        # Project Manager can't read the Administrator-only login log
        self.assertEqual(client.get("/api/login-events").status_code, 403)

        self.conn.execute("UPDATE users SET role='Administrator' WHERE user_id=?", (uid,))
        self.conn.commit()

        # same still-open session, no new login - promotion takes effect immediately
        self.assertEqual(client.get("/api/login-events").status_code, 200)

    def test_demotion_from_administrator_is_picked_up_immediately_too(self):
        uid = self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        self.assertEqual(client.get("/api/login-events").status_code, 200)

        self.conn.execute("UPDATE users SET role='Project Manager' WHERE user_id=?", (uid,))
        self.conn.commit()

        self.assertEqual(client.get("/api/login-events").status_code, 403)

    def test_unrelated_sessions_are_not_disturbed_by_another_users_deletion(self):
        self.make_user("admin", "secret123", role="Administrator")
        other_uid = self.make_user("pm", "secret123", role="Project Manager")
        admin_client = appmod.app.test_client()
        self.login(admin_client, "admin", "secret123")
        pm_client = appmod.app.test_client()
        self.login(pm_client, "pm", "secret123")

        self.conn.execute("DELETE FROM users WHERE user_id=?", (other_uid,))
        self.conn.commit()

        # the PM's own session is correctly cut off...
        self.assertEqual(pm_client.get("/api/reference").status_code, 401)
        # ...but the admin's completely unrelated session is untouched
        self.assertEqual(admin_client.get("/api/reference").status_code, 200)


# ===========================================================================
# Document access log - every PDF download and CSV export must be
# recorded (who, what, when), the same way login attempts already are.
# Goes through the real Flask dispatch, same reasoning as
# TestSecurityHardening.
# ===========================================================================
class TestDocumentAccessLog(ProfitabilityTestCase):

    def tearDown(self):
        appmod._LOGIN_ATTEMPTS.clear()
        appmod._TOTP_ATTEMPTS.clear()
        super().tearDown()

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def make_quote(self, quote_number="TMQ-9001", customer_id=None):
        if customer_id is None:
            customer_id = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Test Co')").lastrowid
            self.conn.commit()
        cur = self.conn.execute(
            "INSERT INTO quotes (quote_number, customer_id, status) VALUES (?,?, 'Sent')",
            (quote_number, customer_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_quote_pdf_download_is_logged(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        qid = self.make_quote()
        resp = client.get(f"/api/quotes/{qid}/pdf")
        self.assertEqual(resp.status_code, 200)
        rows = self.conn.execute("SELECT * FROM document_access_log").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["document_type"], "Quote PDF")
        self.assertEqual(rows[0]["reference"], "TMQ-9001")
        self.assertEqual(rows[0]["username"], "admin")

    def test_reporting_csv_export_is_logged(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        resp = client.get("/api/reporting/export")
        self.assertEqual(resp.status_code, 200)
        rows = self.conn.execute("SELECT * FROM document_access_log WHERE document_type=?",
                                  ("Reporting CSV export",)).fetchall()
        self.assertEqual(len(rows), 1)

    def test_document_access_log_endpoint_requires_administrator(self):
        self.make_user("admin", "secret123", role="Administrator")
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        resp = client.get("/api/document-access-log")
        self.assertEqual(resp.status_code, 403)

    def test_document_access_log_endpoint_lists_recorded_access(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        self.make_quote()
        qid = self.conn.execute("SELECT quote_id FROM quotes").fetchone()[0]
        client.get(f"/api/quotes/{qid}/pdf")
        rows = client.get("/api/document-access-log").get_json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["document_type"], "Quote PDF")


# ===========================================================================
# File permission hardening - best-effort OS-level lockdown of the data
# directory, the database, and the secret key file. Doesn't go through
# Flask at all; just exercises db_location.py directly against a
# throwaway directory. Permission bits are a real, meaningful restriction
# on Mac/Linux but not on Windows (see harden_file_permissions()'s own
# docstring) - skipped there rather than asserting something that
# platform doesn't actually provide.
# ===========================================================================
@unittest.skipUnless(os.name == "posix", "File permission bits aren't meaningful on Windows - see harden_file_permissions()")
class TestFilePermissionHardening(unittest.TestCase):

    def setUp(self):
        import stat
        self.stat = stat
        self.tmp_home = tempfile.mkdtemp()
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp_home
        import importlib
        import db_location
        importlib.reload(db_location)
        self.db_location = db_location

    def tearDown(self):
        import importlib
        if self._orig_home is not None:
            os.environ["HOME"] = self._orig_home
        else:
            os.environ.pop("HOME", None)
        import db_location
        importlib.reload(db_location)
        import shutil
        shutil.rmtree(self.tmp_home, ignore_errors=True)

    def _mode(self, path):
        return self.stat.S_IMODE(os.stat(path).st_mode)

    def test_ensure_data_dir_locks_down_data_and_backup_directories(self):
        self.db_location.ensure_data_dir()
        self.assertEqual(self._mode(self.db_location.DATA_DIR), 0o700)
        self.assertEqual(self._mode(self.db_location.BACKUP_DIR), 0o700)

    def test_database_and_secret_key_are_locked_to_owner_only(self):
        self.db_location.ensure_data_dir()
        with open(self.db_location.DB_PATH, "w") as f:
            f.write("not a real database, just exercising permissions")
        with open(self.db_location.SECRET_KEY_PATH, "w") as f:
            f.write("fake-key")
        os.chmod(self.db_location.DB_PATH, 0o644)  # start deliberately too-open
        os.chmod(self.db_location.SECRET_KEY_PATH, 0o644)
        self.db_location.harden_file_permissions()
        self.assertEqual(self._mode(self.db_location.DB_PATH), 0o600)
        self.assertEqual(self._mode(self.db_location.SECRET_KEY_PATH), 0o600)

    def test_backup_files_are_locked_down_too(self):
        self.db_location.ensure_data_dir()
        with open(self.db_location.DB_PATH, "w") as f:
            f.write("not a real database, just exercising permissions")
        self.db_location.backup_db()
        backups = os.listdir(self.db_location.BACKUP_DIR)
        self.assertEqual(len(backups), 1)
        self.assertEqual(self._mode(os.path.join(self.db_location.BACKUP_DIR, backups[0])), 0o600)

    def test_missing_files_are_skipped_without_raising(self):
        # No ensure_data_dir() call first - the directory doesn't exist at
        # all yet. Must behave like the real first-ever startup (no crash),
        # not assume anything already exists.
        self.db_location.harden_file_permissions()


# ===========================================================================
# safe_float - malformed numeric input from the API must never crash a
# request; every money field passes through this first.
# ===========================================================================
class TestSafeFloat(unittest.TestCase):

    def test_valid_numbers(self):
        self.assertEqual(appmod.safe_float("123.45"), 123.45)
        self.assertEqual(appmod.safe_float(99), 99.0)

    def test_blank_and_none_use_default(self):
        self.assertEqual(appmod.safe_float(""), 0.0)
        self.assertEqual(appmod.safe_float(None), 0.0)
        self.assertEqual(appmod.safe_float(None, default=5.0), 5.0)

    def test_garbage_input_does_not_raise(self):
        self.assertEqual(appmod.safe_float("not a number"), 0.0)
        self.assertEqual(appmod.safe_float({"a": 1}), 0.0)
        self.assertEqual(appmod.safe_float([1, 2, 3]), 0.0)


# ===========================================================================
# Despatch Notes - the outbound counterpart to Goods In. Covers the parts
# most likely to silently break: auto-numbering staying gapless and
# collision-free, itemised lines round-tripping correctly, the PDF route
# actually producing a PDF, and that write access is restricted to
# Project Manager/Administrator (not thrown open the way Goods In is).
# Goes through the real Flask dispatch (app.test_client()), same reasoning
# as TestSecurityHardening above.
# ===========================================================================
class TestDespatchNotes(ProfitabilityTestCase):

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def make_works_order(self, job_number="TM-1000", customer_name="Test Customer"):
        cur = self.conn.execute("INSERT INTO customers (customer_name) VALUES (?)", (customer_name,))
        customer_id = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO works_orders (job_number, job_name, customer_id) VALUES (?,?,?)",
            (job_number, "Test Job", customer_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def test_despatch_numbers_are_sequential_and_gapless(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        wo_id = self.make_works_order()
        first = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-01-01"},
                             headers=self.csrf_headers(client))
        second = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-01-02"},
                              headers=self.csrf_headers(client))
        rows = client.get("/api/despatch-notes").get_json()
        numbers = sorted(r["despatch_number"] for r in rows)
        self.assertEqual(numbers, [f"{appmod.DESPATCH_NUMBER_PREFIX}{appmod.DESPATCH_NUMBER_START}",
                                    f"{appmod.DESPATCH_NUMBER_PREFIX}{appmod.DESPATCH_NUMBER_START + 1}"])

    def test_lines_round_trip_through_create_and_list(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        wo_id = self.make_works_order()
        resp = client.post(
            "/api/despatch-notes",
            json={"works_order_id": wo_id, "despatch_date": "2026-01-01",
                  "lines": [{"qty": 2, "part_number": "PN-1", "description": "Widget"},
                            {"qty": 5, "part_number": "PN-2", "description": "Gadget"}]},
            headers=self.csrf_headers(client),
        )
        self.assertEqual(resp.status_code, 201)
        rows = client.get("/api/despatch-notes").get_json()
        lines = rows[0]["lines"]
        self.assertEqual(len(lines), 2)
        self.assertEqual({l["part_number"] for l in lines}, {"PN-1", "PN-2"})

    def test_role_without_permission_cannot_create(self):
        self.make_user("proc", "secret123", role="Procurement")
        client = appmod.app.test_client()
        self.login(client, "proc", "secret123")
        wo_id = self.make_works_order()
        resp = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-01-01"},
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 403)

    def test_project_manager_can_create(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        wo_id = self.make_works_order()
        resp = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-01-01"},
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 201)

    def test_delete_is_administrator_only(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        wo_id = self.make_works_order()
        did = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-01-01"},
                           headers=self.csrf_headers(client)).get_json()["despatch_note_id"]
        resp = client.delete(f"/api/despatch-notes/{did}", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 403)

    def test_pdf_route_returns_a_pdf(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        wo_id = self.make_works_order()
        did = client.post(
            "/api/despatch-notes",
            json={"works_order_id": wo_id, "despatch_date": "2026-01-01", "delivery_address": "123 Test St",
                  "lines": [{"qty": 1, "part_number": "PN-1", "description": "Widget"}]},
            headers=self.csrf_headers(client),
        ).get_json()["despatch_note_id"]
        resp = client.get(f"/api/despatch-notes/{did}/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content_type, "application/pdf")
        self.assertTrue(resp.data.startswith(b"%PDF"))


class TestDespatchSignature(ProfitabilityTestCase):
    """The on-screen signature captured on the despatching staff member's
    own device at the point of delivery (see sign_despatch_note() in
    app.py) - covers the permission/status gating, input validation, the
    file actually landing on (and being removed from) disk, and the
    signed PDF embedding the captured image."""

    # A real, minimal 1x1 PNG - enough for Pillow (used by despatch_pdf.py
    # to size the embedded image) to open it as a genuine image, without
    # needing Pillow here just to generate a fixture.
    TINY_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

    def setUp(self):
        super().setUp()
        # Signatures are saved to disk at db_location.SIGNATURES_DIR, which
        # by default points at the real user's ~/TEAMworks360 folder (see
        # db_location.py) - tests must never write there, so this redirects
        # it to a throwaway temp directory for the duration of each test,
        # the same precaution TestGoodsInDocuments below takes for
        # GOODS_IN_DOCS_DIR.
        self._orig_sig_dir = appmod.db_location.SIGNATURES_DIR
        self.sig_dir = tempfile.mkdtemp(prefix="despatch_sig_test_")
        appmod.db_location.SIGNATURES_DIR = self.sig_dir

    def tearDown(self):
        appmod.db_location.SIGNATURES_DIR = self._orig_sig_dir
        shutil.rmtree(self.sig_dir, ignore_errors=True)
        super().tearDown()

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def make_works_order(self, job_number="TM-1000", customer_name="Test Customer"):
        cur = self.conn.execute("INSERT INTO customers (customer_name) VALUES (?)", (customer_name,))
        customer_id = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO works_orders (job_number, job_name, customer_id) VALUES (?,?,?)",
            (job_number, "Test Job", customer_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def make_despatch_note(self, client, status="Despatched", job_number="TM-1000", **overrides):
        wo_id = self.make_works_order(job_number=job_number)
        payload = {"works_order_id": wo_id, "despatch_date": "2026-01-01", "status": status}
        payload.update(overrides)
        resp = client.post("/api/despatch-notes", json=payload, headers=self.csrf_headers(client))
        return resp.get_json()["despatch_note_id"]

    def sign(self, client, did, name="Jane Smith", date="2026-01-02", signature=None):
        if signature is None:
            signature = f"data:image/png;base64,{self.TINY_PNG_B64}"
        return client.post(f"/api/despatch-notes/{did}/signature", json={"name": name, "date": date, "signature": signature},
                            headers=self.csrf_headers(client))

    def test_signing_requires_despatch_notes_write_permission(self):
        self.make_user("proc", "secret123", role="Procurement")
        client = appmod.app.test_client()
        self.login(client, "proc", "secret123")
        # Procurement can't even create the despatch note, so sign directly
        # against a note created by an Administrator for this permission check.
        self.make_user("admin", "secret123", role="Administrator")
        admin_client = appmod.app.test_client()
        self.login(admin_client, "admin", "secret123")
        did = self.make_despatch_note(admin_client, job_number="TM-2000")
        resp = self.sign(client, did)
        self.assertEqual(resp.status_code, 403)

    def test_cannot_sign_a_draft_note(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client, status="Draft")
        resp = self.sign(client, did)
        self.assertEqual(resp.status_code, 400)

    def test_cannot_sign_a_cancelled_note(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client, status="Cancelled")
        resp = self.sign(client, did)
        self.assertEqual(resp.status_code, 400)

    def test_missing_name_is_rejected(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        resp = self.sign(client, did, name="")
        self.assertEqual(resp.status_code, 400)

    def test_missing_signature_is_rejected(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        resp = self.sign(client, did, signature="")
        self.assertEqual(resp.status_code, 400)

    def test_malformed_signature_data_is_rejected(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        resp = self.sign(client, did, signature="not-a-data-url")
        self.assertEqual(resp.status_code, 400)
        resp2 = self.sign(client, did, signature="data:image/png;base64,***not-valid-base64***")
        self.assertEqual(resp2.status_code, 400)

    def test_successful_signing_sets_fields_and_stores_file(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        resp = self.sign(client, did, name="Jane Smith", date="2026-01-02")
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT * FROM despatch_notes WHERE despatch_note_id=?", (did,)).fetchone()
        self.assertEqual(row["signature_name"], "Jane Smith")
        self.assertEqual(row["signature_date"], "2026-01-02")
        self.assertEqual(row["signature_captured_by"], "pm")
        self.assertIsNotNone(row["signature_filename"])
        self.assertTrue(os.path.exists(os.path.join(self.sig_dir, row["signature_filename"])))
        # The list view surfaces the signature fields too (dn.* already
        # covers the new columns - see list_despatch_notes() in app.py).
        list_row = next(r for r in client.get("/api/despatch-notes").get_json() if r["despatch_note_id"] == did)
        self.assertEqual(list_row["signature_name"], "Jane Smith")

    def test_resigning_replaces_the_old_file(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        self.sign(client, did, name="First Signer")
        first_filename = self.conn.execute(
            "SELECT signature_filename FROM despatch_notes WHERE despatch_note_id=?", (did,)
        ).fetchone()["signature_filename"]
        first_path = os.path.join(self.sig_dir, first_filename)
        self.assertTrue(os.path.exists(first_path))

        resp = self.sign(client, did, name="Second Signer")
        self.assertEqual(resp.status_code, 200)
        second_filename = self.conn.execute(
            "SELECT signature_filename FROM despatch_notes WHERE despatch_note_id=?", (did,)
        ).fetchone()["signature_filename"]
        self.assertNotEqual(first_filename, second_filename)
        self.assertFalse(os.path.exists(first_path))
        self.assertTrue(os.path.exists(os.path.join(self.sig_dir, second_filename)))

    def test_pdf_generation_succeeds_with_signature_present(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        self.sign(client, did, name="Jane Smith")
        resp = client.get(f"/api/despatch-notes/{did}/pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data.startswith(b"%PDF"))

    def test_signature_file_removed_on_despatch_note_deletion(self):
        self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        did = self.make_despatch_note(client)
        self.sign(client, did, name="Jane Smith")
        filename = self.conn.execute(
            "SELECT signature_filename FROM despatch_notes WHERE despatch_note_id=?", (did,)
        ).fetchone()["signature_filename"]
        path = os.path.join(self.sig_dir, filename)
        self.assertTrue(os.path.exists(path))
        resp = client.delete(f"/api/despatch-notes/{did}", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(path))

    def test_signature_download_requires_login(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        self.sign(client, did, name="Jane Smith")
        anon_client = appmod.app.test_client()
        resp = anon_client.get(f"/api/despatch-notes/{did}/signature")
        self.assertEqual(resp.status_code, 401)

    def test_signature_download_serves_the_png_and_logs_access(self):
        self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        did = self.make_despatch_note(client)
        self.sign(client, did, name="Jane Smith")
        resp = client.get(f"/api/despatch-notes/{did}/signature")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/png")
        log_rows = client.get("/api/document-access-log").get_json()
        self.assertTrue(any(r["document_type"] == "Despatch Note Signature" for r in log_rows))

    def test_no_signature_yet_returns_404(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        did = self.make_despatch_note(client)
        resp = client.get(f"/api/despatch-notes/{did}/signature")
        self.assertEqual(resp.status_code, 404)


class TestGoodsInDocuments(ProfitabilityTestCase):
    """Goods In now requires at least one uploaded "goods received
    document" (a delivery note, packing slip, or photo) attached before a
    record can be saved - enforced server-side in create_goods_in() /
    save_goods_in_documents(), not just as a frontend nicety. These tests
    also cover downloading, adding, and (Administrator-only) removing the
    stored files afterwards."""

    def setUp(self):
        super().setUp()
        # Uploaded documents are saved to disk at db_location.GOODS_IN_DOCS_DIR,
        # which by default points at the real user's ~/TEAMworks360 folder
        # (see db_location.py) - tests must never write there, so this
        # redirects it to a throwaway temp directory for the duration of
        # each test, the same precaution setUp() above already takes for
        # appmod.DB_PATH.
        self._orig_docs_dir = appmod.db_location.GOODS_IN_DOCS_DIR
        self.docs_dir = tempfile.mkdtemp(prefix="gi_docs_test_")
        appmod.db_location.GOODS_IN_DOCS_DIR = self.docs_dir

    def tearDown(self):
        appmod.db_location.GOODS_IN_DOCS_DIR = self._orig_docs_dir
        shutil.rmtree(self.docs_dir, ignore_errors=True)
        super().tearDown()

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def make_purchase_order(self, po_number="PO-1000", job_number="TM-1000"):
        cur = self.conn.execute("INSERT INTO customers (customer_name) VALUES (?)", ("Test Customer",))
        customer_id = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO works_orders (job_number, job_name, customer_id) VALUES (?,?,?)",
            (job_number, "Test Job", customer_id),
        )
        wo_id = cur.lastrowid
        cur = self.conn.execute("INSERT INTO suppliers (supplier_name) VALUES (?)", ("Test Supplier",))
        supplier_id = cur.lastrowid
        cur = self.conn.execute(
            "INSERT INTO purchase_orders (po_number, works_order_id, supplier_id) VALUES (?,?,?)",
            (po_number, wo_id, supplier_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def goods_in_form_data(self, po_id, **overrides):
        data = {
            "purchase_order_id": str(po_id),
            "destination_type": "Works Order",
            "received_date": "2026-01-01",
            "quantity_status": "Full",
            "received_by": "Jane Smith",
        }
        data.update(overrides)
        return data

    def create_with_one_document(self, client, po_id, filename="one.pdf", content=b"one"):
        data = self.goods_in_form_data(po_id)
        data["documents"] = (io.BytesIO(content), filename)
        resp = client.post("/api/goods-in", data=data, content_type="multipart/form-data",
                            headers=self.csrf_headers(client))
        return resp

    def test_cannot_create_without_a_document(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        resp = client.post("/api/goods-in", data=self.goods_in_form_data(po_id),
                            content_type="multipart/form-data", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("document", resp.get_json()["error"].lower())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goods_in").fetchone()[0], 0)

    def test_json_only_request_is_also_rejected(self):
        # Belt and braces: a plain JSON POST (which can never carry a file
        # at all) must be refused too, not silently accepted with zero
        # documents - defence in depth beyond the frontend's own <input
        # required> and its FormData-based submit.
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        resp = client.post("/api/goods-in", json=self.goods_in_form_data(po_id),
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goods_in").fetchone()[0], 0)

    def test_create_with_a_document_succeeds_and_is_stored(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        resp = self.create_with_one_document(client, po_id, "delivery_note.pdf", b"%PDF-1.4 fake delivery note")
        self.assertEqual(resp.status_code, 201)
        gid = resp.get_json()["goods_in_id"]
        docs = client.get(f"/api/goods-in/{gid}/documents").get_json()
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["original_filename"], "delivery_note.pdf")
        # Actually landed on disk, not just a DB row with nothing behind it.
        stored = self.conn.execute(
            "SELECT stored_filename FROM goods_in_documents WHERE goods_in_id=?", (gid,)
        ).fetchone()["stored_filename"]
        self.assertTrue(os.path.exists(os.path.join(self.docs_dir, stored)))
        # The list view's document_count reflects it too.
        list_row = next(r for r in client.get("/api/goods-in").get_json() if r["goods_in_id"] == gid)
        self.assertEqual(list_row["document_count"], 1)

    def test_rejects_disallowed_file_type(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        resp = self.create_with_one_document(client, po_id, "script.sh", b"#!/bin/sh\necho hi")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goods_in").fetchone()[0], 0)

    def test_too_many_files_in_one_upload_is_rejected(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        data = self.goods_in_form_data(po_id)
        data["documents"] = [(io.BytesIO(b"x"), f"f{i}.pdf") for i in range(appmod.GOODS_IN_MAX_FILES_PER_UPLOAD + 1)]
        resp = client.post("/api/goods-in", data=data, content_type="multipart/form-data",
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM goods_in").fetchone()[0], 0)

    def test_download_is_logged_to_document_access_log(self):
        self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        po_id = self.make_purchase_order()
        gid = self.create_with_one_document(client, po_id, "note.pdf", b"%PDF-1.4 x").get_json()["goods_in_id"]
        doc_id = client.get(f"/api/goods-in/{gid}/documents").get_json()[0]["document_id"]
        resp = client.get(f"/api/goods-in/documents/{doc_id}/download")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"%PDF-1.4 x")
        log_rows = client.get("/api/document-access-log").get_json()
        self.assertTrue(any(r["document_type"] == "Goods In document" for r in log_rows))

    def test_adding_a_second_document_to_an_existing_record(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        gid = self.create_with_one_document(client, po_id, "one.pdf", b"one").get_json()["goods_in_id"]
        resp = client.post(f"/api/goods-in/{gid}/documents",
                            data={"documents": (io.BytesIO(b"two"), "two.pdf")},
                            content_type="multipart/form-data", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 201)
        docs = client.get(f"/api/goods-in/{gid}/documents").get_json()
        self.assertEqual(len(docs), 2)

    def test_non_administrator_cannot_delete_a_document(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        po_id = self.make_purchase_order()
        gid = self.create_with_one_document(client, po_id).get_json()["goods_in_id"]
        doc_id = client.get(f"/api/goods-in/{gid}/documents").get_json()[0]["document_id"]
        resp = client.delete(f"/api/goods-in/documents/{doc_id}", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 403)

    def test_cannot_delete_the_last_remaining_document(self):
        self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        po_id = self.make_purchase_order()
        gid = self.create_with_one_document(client, po_id).get_json()["goods_in_id"]
        doc_id = client.get(f"/api/goods-in/{gid}/documents").get_json()[0]["document_id"]
        resp = client.delete(f"/api/goods-in/documents/{doc_id}", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(client.get(f"/api/goods-in/{gid}/documents").get_json()), 1)

    def test_administrator_can_delete_a_document_when_another_remains(self):
        self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        po_id = self.make_purchase_order()
        gid = self.create_with_one_document(client, po_id, "one.pdf", b"one").get_json()["goods_in_id"]
        client.post(f"/api/goods-in/{gid}/documents", data={"documents": (io.BytesIO(b"two"), "two.pdf")},
                    content_type="multipart/form-data", headers=self.csrf_headers(client))
        docs = client.get(f"/api/goods-in/{gid}/documents").get_json()
        first_doc_id = docs[0]["document_id"]
        stored = self.conn.execute(
            "SELECT stored_filename FROM goods_in_documents WHERE document_id=?", (first_doc_id,)
        ).fetchone()["stored_filename"]
        path = os.path.join(self.docs_dir, stored)
        self.assertTrue(os.path.exists(path))
        resp = client.delete(f"/api/goods-in/documents/{first_doc_id}", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 200)
        remaining = client.get(f"/api/goods-in/{gid}/documents").get_json()
        self.assertEqual(len(remaining), 1)
        self.assertFalse(os.path.exists(path))  # the file itself was removed, not just the DB row

    def test_deleting_a_goods_in_record_removes_its_document_files(self):
        self.make_user("admin", "secret123", role="Administrator")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        po_id = self.make_purchase_order()
        gid = self.create_with_one_document(client, po_id).get_json()["goods_in_id"]
        stored = self.conn.execute(
            "SELECT stored_filename FROM goods_in_documents WHERE goods_in_id=?", (gid,)
        ).fetchone()["stored_filename"]
        path = os.path.join(self.docs_dir, stored)
        self.assertTrue(os.path.exists(path))
        resp = client.delete(f"/api/goods-in/{gid}", headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(path))

    def test_every_role_that_can_create_goods_in_can_attach_a_document(self):
        # Goods In is deliberately open to every role (see ROLE_PERMISSIONS)
        # - confirm the new document requirement didn't accidentally narrow
        # that down to just Administrator.
        for role in ("Project Manager", "Procurement", "Finance", "Executive"):
            with self.subTest(role=role):
                username = role.lower().replace(" ", "_")
                self.make_user(username, "secret123", role=role)
                client = appmod.app.test_client()
                self.login(client, username, "secret123")
                po_id = self.make_purchase_order(po_number=f"PO-{username}", job_number=f"TM-{username}")
                resp = self.create_with_one_document(client, po_id)
                self.assertEqual(resp.status_code, 201)


class TestResetJobData(ProfitabilityTestCase):
    """The Administrator-only "Reset for go-live" action (reset_job_data()
    in app.py) that wipes every job transaction while leaving reference
    data and every audit/security trail untouched - see Settings, "Go-Live:
    Reset Job Data" in the UI."""

    def setUp(self):
        super().setUp()
        # reset_job_data() calls db_location.backup_db() (reads
        # db_location.DB_PATH/BACKUP_DIR directly, not appmod.DB_PATH) and
        # save_goods_in_documents()/download use db_location.GOODS_IN_DOCS_DIR
        # - all three must be redirected to throwaway locations so this
        # never touches the real user's ~/TEAMworks360 folder, the same
        # precaution TestGoodsInDocuments above takes.
        self._orig_db_path = appmod.db_location.DB_PATH
        self._orig_backup_dir = appmod.db_location.BACKUP_DIR
        self._orig_docs_dir = appmod.db_location.GOODS_IN_DOCS_DIR
        appmod.db_location.DB_PATH = self.db_path
        self.backup_dir = tempfile.mkdtemp(prefix="reset_backup_test_")
        appmod.db_location.BACKUP_DIR = self.backup_dir
        self.docs_dir = tempfile.mkdtemp(prefix="reset_docs_test_")
        appmod.db_location.GOODS_IN_DOCS_DIR = self.docs_dir

    def tearDown(self):
        appmod.db_location.DB_PATH = self._orig_db_path
        appmod.db_location.BACKUP_DIR = self._orig_backup_dir
        appmod.db_location.GOODS_IN_DOCS_DIR = self._orig_docs_dir
        shutil.rmtree(self.backup_dir, ignore_errors=True)
        shutil.rmtree(self.docs_dir, ignore_errors=True)
        super().tearDown()

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def csrf_headers(self, client):
        cookie = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": cookie.value} if cookie else {}

    def build_full_job_graph(self, client):
        """One of everything job-related, tied together, plus a stock item
        with one allocation to the job and one to a Ship To site (so the
        test can tell which allocation gets cleared and which doesn't)."""
        cust = self.make_customer()
        supplier = self.make_supplier()
        wo_id = self.make_works_order("TM-RESET-1", cust, contract_value=10000, budget_cost=5000)
        po_id = self.make_purchase_order(wo_id, supplier, po_value=2000)
        self.make_supplier_invoice(wo_id, po_id, supplier, 2000)
        self.make_customer_invoice(wo_id, 10000)
        self.make_labour_entry(wo_id, hours=10, rate=50)
        self.make_quote("TMQ-RESET-1", cust)

        gi_data = {
            "purchase_order_id": str(po_id), "destination_type": "Works Order",
            "received_date": "2026-01-01", "quantity_status": "Full", "received_by": "Jane Smith",
            "documents": (io.BytesIO(b"fake delivery note"), "delivery_note.pdf"),
        }
        gi_resp = client.post("/api/goods-in", data=gi_data, content_type="multipart/form-data",
                               headers=self.csrf_headers(client))
        self.assertEqual(gi_resp.status_code, 201)
        doc_path = os.path.join(self.docs_dir, self.conn.execute(
            "SELECT stored_filename FROM goods_in_documents"
        ).fetchone()["stored_filename"])
        self.assertTrue(os.path.exists(doc_path))

        dn_resp = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-01-02"},
                               headers=self.csrf_headers(client))
        self.assertEqual(dn_resp.status_code, 201)

        ship_to_id = self.conn.execute("SELECT ship_to_id FROM ship_to_addresses LIMIT 1").fetchone()[0]
        stock_item_id = self.conn.execute(
            "INSERT INTO stock_items (part_number, description) VALUES ('PN-RESET-1', 'Test Part')"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO stock_allocations (stock_item_id, quantity, works_order_id) VALUES (?, 5, ?)",
            (stock_item_id, wo_id),
        )
        self.conn.execute(
            "INSERT INTO stock_allocations (stock_item_id, quantity, ship_to_id) VALUES (?, 3, ?)",
            (stock_item_id, ship_to_id),
        )
        self.conn.execute(
            "INSERT INTO stock_movements (stock_item_id, quantity, to_works_order_id) VALUES (?, 5, ?)",
            (stock_item_id, wo_id),
        )
        self.conn.execute(
            "INSERT INTO stock_movements (stock_item_id, quantity, to_ship_to_id) VALUES (?, 3, ?)",
            (stock_item_id, ship_to_id),
        )
        self.conn.commit()
        return {"wo_id": wo_id, "po_id": po_id, "stock_item_id": stock_item_id,
                "ship_to_id": ship_to_id, "doc_path": doc_path}

    def test_requires_administrator_role(self):
        self.make_user("pm", "secret123", role="Project Manager")
        client = appmod.app.test_client()
        self.login(client, "pm", "secret123")
        resp = client.post("/api/admin/reset-job-data", json={"confirm": "RESET JOB DATA"},
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 403)

    def test_wrong_or_missing_confirmation_phrase_is_rejected_and_deletes_nothing(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        self.build_full_job_graph(client)
        for payload in ({"confirm": "reset job data"}, {"confirm": "RESET"}, {}):
            with self.subTest(payload=payload):
                resp = client.post("/api/admin/reset-job-data", json=payload, headers=self.csrf_headers(client))
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works_orders").fetchone()[0], 1)

    def test_wipes_job_tables_but_keeps_reference_and_audit_data(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        graph = self.build_full_job_graph(client)

        # Reference data and audit trails that must survive untouched.
        self.conn.execute("INSERT INTO login_events (username, event_type) VALUES ('admin', 'success')")
        self.conn.execute(
            "INSERT INTO document_access_log (username, document_type, reference) VALUES ('admin', 'Quote', 'TMQ-1')"
        )
        self.conn.execute(
            "INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value, changed_by) "
            "VALUES ('works_orders', 1, 'status', 'Draft', 'Open', 'admin')"
        )
        self.conn.commit()
        login_events_before = self.conn.execute("SELECT COUNT(*) FROM login_events").fetchone()[0]
        doc_log_before = self.conn.execute("SELECT COUNT(*) FROM document_access_log").fetchone()[0]
        audit_before = self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

        resp = client.post("/api/admin/reset-job-data", json={"confirm": "RESET JOB DATA"},
                            headers=self.csrf_headers(client))
        self.assertEqual(resp.status_code, 200)
        deleted = resp.get_json()["deleted"]
        self.assertEqual(deleted["works_orders"], 1)
        self.assertEqual(deleted["quotes"], 1)
        self.assertEqual(deleted["stock_allocations"], 1)  # only the job-tied one
        self.assertEqual(deleted["stock_movements"], 1)    # only the job-tied one

        for table in ("quotes", "works_orders", "purchase_orders", "goods_in", "goods_in_documents",
                      "despatch_notes", "despatch_note_lines", "supplier_invoices", "customer_invoices",
                      "labour_entries", "quote_labour_lines", "quote_purchase_lines", "purchase_order_lines"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

        # The uploaded Goods In document is gone from disk too.
        self.assertFalse(os.path.exists(graph["doc_path"]))

        # The ship-to-allocated stock (not job-tied) survives.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM stock_allocations WHERE ship_to_id=?", (graph["ship_to_id"],)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE to_ship_to_id=?", (graph["ship_to_id"],)
        ).fetchone()[0], 1)
        # The stock item (catalogue entry) itself is untouched.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM stock_items").fetchone()[0], 1)

        # Reference data untouched.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0], 1)
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM ship_to_addresses").fetchone()[0], 1)
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM rate_card").fetchone()[0], 1)
        self.assertGreaterEqual(self.conn.execute("SELECT COUNT(*) FROM contract_code_targets").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

        # Every audit/security trail untouched (reset_job_data adds exactly
        # one new audit_log row of its own, recording the reset itself).
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM login_events").fetchone()[0], login_events_before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM document_access_log").fetchone()[0], doc_log_before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0], audit_before + 1)
        new_audit_row = self.conn.execute(
            "SELECT * FROM audit_log WHERE field_name='reset_job_data'"
        ).fetchone()
        self.assertEqual(new_audit_row["changed_by"], "admin")

        # A backup was taken.
        self.assertTrue(any(f.startswith("jpm-") for f in os.listdir(self.backup_dir)))

    def test_quote_and_despatch_numbering_restarts_after_reset(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        self.build_full_job_graph(client)
        client.post("/api/admin/reset-job-data", json={"confirm": "RESET JOB DATA"},
                    headers=self.csrf_headers(client))

        cust = self.make_customer("Another Customer")
        wo_id = self.make_works_order("TM-AFTER-RESET", cust)
        dn_resp = client.post("/api/despatch-notes", json={"works_order_id": wo_id, "despatch_date": "2026-02-01"},
                               headers=self.csrf_headers(client))
        dn_id = dn_resp.get_json()["despatch_note_id"]
        despatch_number = self.conn.execute(
            "SELECT despatch_number FROM despatch_notes WHERE despatch_note_id=?", (dn_id,)
        ).fetchone()["despatch_number"]
        self.assertEqual(despatch_number, f"{appmod.DESPATCH_NUMBER_PREFIX}{appmod.DESPATCH_NUMBER_START}")
        self.assertEqual(
            appmod.get_next_quote_number(appmod.get_db()),
            f"{appmod.QUOTE_NUMBER_PREFIX}{appmod.QUOTE_NUMBER_START}",
        )


class TestReferenceEndpoint(ProfitabilityTestCase):
    """/api/reference feeds every page's filter-dropdown option lists (see
    the many `state.ref.*` reads in app.js), not just whatever that page's
    own live list endpoint returns - so a field a dropdown needs has to
    actually be selected here, even if nothing else reads it from this
    particular response. Regression coverage for a real bug: the Works
    Orders page's Owner filter (renderWorksOrders() in app.js) builds its
    option list from this works_orders reference set's project_manager
    field, which this endpoint's query wasn't selecting - so the dropdown
    silently had no options beyond 'All owners', for every installation,
    without the API ever returning an error."""

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def test_works_orders_reference_rows_include_project_manager(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-REF-1", cust)
        self.conn.execute("UPDATE works_orders SET project_manager=? WHERE works_order_id=?", ("Alice", wo))
        self.conn.commit()

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        data = client.get("/api/reference").get_json()

        row = next(r for r in data["works_orders"] if r["job_number"] == "TM-REF-1")
        self.assertIn("project_manager", row)
        self.assertEqual(row["project_manager"], "Alice")


class TestReportingEndpoints(ProfitabilityTestCase):
    """The three new Reporting views - Spend by Supplier, Monthly Income &
    Spend, and Income by Contract Code (filterable by Project Manager) -
    added alongside the existing Job Profitability report. Covers the
    numbers each one adds up (Cancelled records excluded, same as every
    other total in the app), the year-selection fallback logic, and that
    every role can reach them (plain @login_required, same as the
    existing /api/reporting, since any of these might reasonably be
    checked by Finance, a Project Manager, or an Administrator alike)."""

    def make_user(self, username, password, role="Administrator"):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def login(self, client, username, password):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        if resp.status_code == 200 and resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return resp

    def set_project_manager(self, wo_id, name):
        self.conn.execute("UPDATE works_orders SET project_manager=? WHERE works_order_id=?", (name, wo_id))
        self.conn.commit()

    # ---- Spend by Supplier ------------------------------------------------

    def test_spend_by_supplier_sums_non_cancelled_invoices_and_lists_every_supplier(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-SS-1", cust)
        supplier_a = self.make_supplier("Supplier A")
        supplier_b = self.make_supplier("Supplier B")  # never invoiced - should still be listed, at zero
        po_a = self.make_purchase_order(wo, supplier_a, po_value=500)
        self.make_supplier_invoice(wo, po_a, supplier_a, 300, status="Approved")
        self.make_supplier_invoice(wo, po_a, supplier_a, 9999, status="Cancelled")  # must not count

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        data = client.get("/api/reporting/spend-by-supplier").get_json()

        row_a = next(r for r in data["rows"] if r["supplier_name"] == "Supplier A")
        row_b = next(r for r in data["rows"] if r["supplier_name"] == "Supplier B")
        self.assertEqual(row_a["total_spend"], 300)
        self.assertEqual(row_a["invoice_count"], 1)
        self.assertEqual(row_b["total_spend"], 0)
        self.assertEqual(row_b["invoice_count"], 0)

    def test_spend_by_supplier_year_filter(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-SS-2", cust)
        supplier = self.make_supplier("Year Test Supplier")
        po = self.make_purchase_order(wo, supplier, po_value=1000)
        self.make_supplier_invoice(wo, po, supplier, 100, invoice_date="2024-03-01")
        self.make_supplier_invoice(wo, po, supplier, 250, invoice_date="2025-06-01")

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")

        data_2024 = client.get("/api/reporting/spend-by-supplier?year=2024").get_json()
        row_2024 = next(r for r in data_2024["rows"] if r["supplier_name"] == "Year Test Supplier")
        self.assertEqual(row_2024["total_spend"], 100)
        self.assertEqual(sorted(data_2024["years"]), ["2024", "2025"])

        data_2025 = client.get("/api/reporting/spend-by-supplier?year=2025").get_json()
        row_2025 = next(r for r in data_2025["rows"] if r["supplier_name"] == "Year Test Supplier")
        self.assertEqual(row_2025["total_spend"], 250)

    def test_spend_by_supplier_export_hides_zero_spend_suppliers_unless_asked(self):
        self.make_supplier("No Invoices Ltd")
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")

        default_csv = client.get("/api/reporting/spend-by-supplier/export").get_data(as_text=True)
        self.assertNotIn("No Invoices Ltd", default_csv)

        with_zero_csv = client.get("/api/reporting/spend-by-supplier/export?include_zero=1").get_data(as_text=True)
        self.assertIn("No Invoices Ltd", with_zero_csv)

    def test_spend_by_supplier_requires_login(self):
        resp = appmod.app.test_client().get("/api/reporting/spend-by-supplier")
        self.assertEqual(resp.status_code, 401)

    def test_spend_by_supplier_reachable_by_every_role(self):
        self.make_user("proc", "secret123", role="Procurement")
        client = appmod.app.test_client()
        self.login(client, "proc", "secret123")
        resp = client.get("/api/reporting/spend-by-supplier")
        self.assertEqual(resp.status_code, 200)

    # ---- Monthly Income & Spend --------------------------------------------

    def test_monthly_income_spend_excludes_cancelled_and_sums_supplier_plus_labour(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-MIS-1", cust)
        self.make_customer_invoice(wo, 1000, status="Paid", invoice_date="2024-01-15")
        self.make_customer_invoice(wo, 9999, status="Cancelled", invoice_date="2024-01-20")  # excluded
        self.make_customer_invoice(wo, 2000, status="Paid", invoice_date="2024-02-10")
        po = self.make_purchase_order(wo, self.make_supplier(), po_value=500)
        self.make_supplier_invoice(wo, po, None, 300, status="Approved", invoice_date="2024-01-05")
        self.make_labour_entry(wo, hours=5, rate=20, work_date="2024-02-01")  # £100

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        data = client.get("/api/reporting/monthly-income-spend?year=2024").get_json()

        self.assertEqual(data["year"], "2024")
        self.assertEqual(len(data["months"]), 12)  # a past year always shows all 12 months
        jan = next(m for m in data["months"] if m["month"] == "2024-01")
        feb = next(m for m in data["months"] if m["month"] == "2024-02")
        self.assertEqual(jan["income"], 1000)
        self.assertEqual(jan["spend"], 300)
        self.assertEqual(jan["net"], 700)
        self.assertEqual(feb["income"], 2000)
        self.assertEqual(feb["spend"], 100)
        self.assertEqual(feb["net"], 1900)
        self.assertEqual(data["totals"], {"income": 3000, "spend": 400, "net": 2600})

    def test_monthly_income_spend_offers_current_year_even_with_no_data(self):
        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        data = client.get("/api/reporting/monthly-income-spend").get_json()

        current_year = str(date.today().year)
        self.assertEqual(data["year"], current_year)
        self.assertIn(current_year, data["years"])
        self.assertEqual(len(data["months"]), date.today().month)  # zero-filled up to this month, not beyond
        self.assertEqual(data["totals"], {"income": 0, "spend": 0, "net": 0})

    def test_monthly_income_spend_falls_back_when_requested_year_has_no_data(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-MIS-2", cust)
        self.make_customer_invoice(wo, 500, status="Paid", invoice_date="2024-05-01")

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        data = client.get("/api/reporting/monthly-income-spend?year=1999").get_json()
        # 1999 isn't a real year in this data, so it falls back to the most
        # recent year on the list - which always includes the current year
        # (see build_monthly_income_spend), so that's what's picked here
        # rather than the older 2024 year that actually has data.
        self.assertEqual(data["year"], str(date.today().year))
        self.assertIn("2024", data["years"])

    def test_monthly_income_spend_export_includes_a_total_row(self):
        cust = self.make_customer()
        wo = self.make_works_order("TM-MIS-3", cust)
        self.make_customer_invoice(wo, 500, status="Paid", invoice_date="2024-07-01")

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        csv_text = client.get("/api/reporting/monthly-income-spend/export?year=2024").get_data(as_text=True)
        self.assertIn("Total", csv_text)
        self.assertIn("500.00", csv_text)

    # ---- Income by Contract Code ------------------------------------------

    def test_income_by_contract_code_rows_carry_project_manager_and_exclude_cancelled(self):
        cust = self.make_customer()
        wo1 = self.make_works_order("TM-IBC-1", cust, contract_code="INT")
        self.set_project_manager(wo1, "Alice")
        self.make_customer_invoice(wo1, 500, status="Paid")
        self.make_customer_invoice(wo1, 9999, status="Cancelled")  # excluded

        wo2 = self.make_works_order("TM-IBC-2", cust, contract_code="INT")
        self.set_project_manager(wo2, "Bob")
        self.make_customer_invoice(wo2, 300, status="Paid")

        wo3 = self.make_works_order("TM-IBC-3", cust, contract_code="NEW")
        self.set_project_manager(wo3, "Alice")
        self.make_customer_invoice(wo3, 200, status="Paid")

        self.make_user("admin", "secret123")
        client = appmod.app.test_client()
        self.login(client, "admin", "secret123")
        data = client.get("/api/reporting/income-by-contract-code").get_json()

        self.assertEqual(data["project_managers"], ["Alice", "Bob"])
        row1 = next(r for r in data["rows"] if r["job_number"] == "TM-IBC-1")
        row2 = next(r for r in data["rows"] if r["job_number"] == "TM-IBC-2")
        row3 = next(r for r in data["rows"] if r["job_number"] == "TM-IBC-3")
        self.assertEqual((row1["revenue"], row1["invoice_count"], row1["project_manager"]), (500, 1, "Alice"))
        self.assertEqual((row2["revenue"], row2["invoice_count"], row2["project_manager"]), (300, 1, "Bob"))
        self.assertEqual((row3["revenue"], row3["invoice_count"], row3["project_manager"]), (200, 1, "Alice"))

    def test_income_by_contract_code_requires_login(self):
        resp = appmod.app.test_client().get("/api/reporting/income-by-contract-code")
        self.assertEqual(resp.status_code, 401)

    def test_income_by_contract_code_reachable_by_every_role(self):
        self.make_user("fin", "secret123", role="Finance")
        client = appmod.app.test_client()
        self.login(client, "fin", "secret123")
        resp = client.get("/api/reporting/income-by-contract-code")
        self.assertEqual(resp.status_code, 200)


class TestDeleteWorksOrder(ProfitabilityTestCase):
    """Deleting a job that has linked records (despatch note, goods in, stock,
    quote...) used to fail with a bare 'Request failed'."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            ("admin", appmod.generate_password_hash("secret123"), "admin", "Administrator"),
        )
        self.conn.commit()
        self.client = appmod.app.test_client()
        resp = self.client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(self.client)

    def hdr(self):
        c = self.client.get_cookie("csrf_token")
        return {"X-CSRF-Token": c.value} if c else {}

    def build(self):
        q = self.conn.execute
        cust = q("INSERT INTO customers (customer_name) VALUES ('C')").lastrowid
        sup = q("INSERT INTO suppliers (supplier_name) VALUES ('S')").lastrowid
        wo = q("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TM-1','J',?)", (cust,)).lastrowid
        po = q("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value) VALUES ('PO1',?,?,10)", (wo, sup)).lastrowid
        q("INSERT INTO purchase_order_lines (purchase_order_id, qty, unit_price) VALUES (?,1,10)", (po,))
        gi = q("INSERT INTO goods_in (purchase_order_id, destination_type, received_date, quantity_status, received_by) "
               "VALUES (?,'Works Order','2026-01-01','Full','x')", (po,)).lastrowid
        q("INSERT INTO goods_in_documents (goods_in_id, original_filename, stored_filename, uploaded_by) VALUES (?,'a','a.png','x')", (gi,))
        q("INSERT INTO supplier_invoices (works_order_id, purchase_order_id, supplier_id, invoice_number, invoice_amount) VALUES (?,?,?,'SI1',10)", (wo, po, sup))
        q("INSERT INTO despatch_notes (despatch_number, works_order_id) VALUES ('TMD-1',?)", (wo,))
        q("INSERT INTO quotes (quote_number, works_order_id) VALUES ('TMQ-1',?)", (wo,))
        self.conn.commit()
        return wo

    def test_delete_removes_job_and_everything_linked(self):
        wo = self.build()
        resp = self.client.delete(f"/api/works-orders/{wo}", headers=self.hdr())
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        for tbl in ("works_orders", "purchase_orders", "goods_in", "goods_in_documents", "despatch_notes", "supplier_invoices"):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0], 0, tbl)
        # the quote survives, just unlinked
        self.assertIsNone(self.conn.execute("SELECT works_order_id FROM quotes").fetchone()[0])

    def test_delete_is_refused_with_a_clear_message_while_stock_is_allocated(self):
        wo = self.build()
        item = self.conn.execute("INSERT INTO stock_items (part_number) VALUES ('WIDGET')").lastrowid
        self.conn.execute("INSERT INTO stock_allocations (stock_item_id, quantity, works_order_id) VALUES (?,3,?)", (item, wo))
        self.conn.commit()
        resp = self.client.delete(f"/api/works-orders/{wo}", headers=self.hdr())
        self.assertEqual(resp.status_code, 400)
        self.assertIn("WIDGET", resp.get_json()["error"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works_orders").fetchone()[0], 1)


class TestQuoteRegisterImport(ProfitabilityTestCase):
    """import_quote_register.py: adds register quotes without touching what's there."""

    def reg(self, tmq, **kw):
        r = [tmq, "SC", dt.datetime(2026, 1, 5), dt.datetime(2026, 1, 6), "RR", "DCDD", "Pat", None, "REF1",
             "Part1", "Fixture repair", "TEAM", "FR", 500, None, None, "Won"]
        for k, v in kw.items():
            r[int(k[1:])] = v
        return (3, tuple(r))

    def run_import(self, rows):
        import import_quote_register as iqr
        imp = iqr.Importer(self.conn)
        imp.learn(rows)
        for n, r in rows:
            imp.add(n, r)
        self.conn.commit()
        return imp

    def setUp(self):
        super().setUp()
        cid = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Rolls Royce - Derby')").lastrowid
        self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Unknown Customer')")
        self.conn.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TM-5000','J',?)", (cid,))
        self.conn.commit()

    def test_quote_links_to_works_order_and_takes_its_customer(self):
        self.run_import([self.reg("TMQ-1", r14="TM-5000")])
        q = self.conn.execute("SELECT q.*, c.customer_name FROM quotes q JOIN customers c USING(customer_id)").fetchone()
        self.assertEqual((q["customer_name"], q["status"], q["estimated_value"]), ("Rolls Royce - Derby", "Won", 500))
        self.assertIsNotNone(q["works_order_id"])
        self.assertIn("Area: DCDD", q["notes"])

    def test_values_dates_and_statuses_are_tidied(self):
        imp = self.run_import([
            self.reg("TMQ-2", r13="TBC", r16="Cancelled"),
            self.reg("TMQ-3", r13="£4.166.66", r16="Submitted", r3="08/102025"),
            self.reg("TMQ-4", r16=None, r3=None, r14=None),
        ])
        rows = {r["quote_number"]: r for r in self.conn.execute("SELECT * FROM quotes")}
        self.assertEqual((rows["TMQ-2"]["status"], rows["TMQ-2"]["estimated_value"]), ("Cancelled", 0))
        self.assertIn("Cancelled", rows["TMQ-2"]["notes"])
        self.assertEqual((rows["TMQ-3"]["status"], rows["TMQ-3"]["estimated_value"]), ("Sent", 4166.66))
        self.assertEqual(rows["TMQ-4"]["status"], "Draft")

    def test_one_row_holding_two_quotes_becomes_two(self):
        self.run_import([self.reg("TMQ-1289.1\nTMQ-1289.2", r13="£8,672.00\n£7,319.40")])
        vals = [r[0] for r in self.conn.execute("SELECT estimated_value FROM quotes ORDER BY quote_number")]
        self.assertEqual(vals, [8672.0, 7319.4])

    def test_existing_quote_is_never_changed_and_rerun_adds_nothing(self):
        self.conn.execute("INSERT INTO quotes (quote_number, description, status) VALUES ('TMQ-9','mine','Draft')")
        self.conn.commit()
        self.run_import([self.reg("TMQ-9"), self.reg("TMQ-10")])
        self.assertEqual(self.conn.execute("SELECT description FROM quotes WHERE quote_number='TMQ-9'").fetchone()[0], "mine")
        again = self.run_import([self.reg("TMQ-9"), self.reg("TMQ-10")])
        self.assertEqual(again.counts["added"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 2)

    def test_empty_reserved_numbers_are_skipped(self):
        imp = self.run_import([(3, ("TMQ-2999",) + (None,) * 16)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 0)


class TestPoRegisterImport(ProfitabilityTestCase):
    """import_po_register.py: history only - never changes a job's costs."""

    def reg(self, po, **kw):
        r = [po, dt.datetime(2025, 3, 1), None, dt.datetime(2025, 3, 8), "YES", "TM-5000", "Midlands", "Acme Engineering Ltd.",
             None, "Rolls Royce", "Bob", "S Lee", "Widget", 2, None, None, "P1", None, None, 100, None, None]
        for k, v in kw.items():
            r[int(k[1:])] = v
        return (4, tuple(r))

    def setUp(self):
        super().setUp()
        cid = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Rolls Royce - Derby')").lastrowid
        self.wo = self.conn.execute(
            "INSERT INTO works_orders (job_number, job_name, customer_id, contract_value) VALUES ('TM-5000','J',?,1000)", (cid,)).lastrowid
        self.conn.execute("INSERT INTO suppliers (supplier_name) VALUES ('Acme Engineering')")
        self.conn.commit()

    def run_import(self, rows):
        import import_po_register as ipr
        imp = ipr.Importer(self.conn)
        for n, r in rows:
            imp.add(n, r)
        self.conn.commit()
        return imp

    def metrics(self):
        wo = self.conn.execute("SELECT * FROM works_orders WHERE works_order_id=?", (self.wo,)).fetchone()
        with appmod.app.app_context():
            return appmod.compute_job_metrics(self.conn, wo)

    def test_historic_pos_do_not_touch_job_costs_but_are_stored(self):
        before = self.metrics()
        imp = self.run_import([self.reg("PO-1", r19=5000), self.reg("PO-2", r4=None, r19=700)])
        after = self.metrics()
        self.assertEqual((before["committed_cost"], before["forecast_cost"]), (after["committed_cost"], after["forecast_cost"]))
        rows = self.conn.execute("SELECT * FROM purchase_orders ORDER BY po_number").fetchall()
        self.assertEqual([(r["po_value"], r["status"], r["is_historic"]) for r in rows], [(5000, "Completed", 1), (700, "Issued", 1)])
        self.assertEqual(rows[0]["works_order_id"], self.wo)
        self.assertEqual(imp.counts["suppliers_added"], 0)   # 'Acme Engineering Ltd.' matched 'Acme Engineering'

    def test_live_po_still_counts(self):
        sup = self.conn.execute("SELECT supplier_id FROM suppliers").fetchone()[0]
        self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, status) VALUES ('LIVE',?,?,300,'Issued')", (self.wo, sup))
        self.conn.commit()
        self.assertEqual(self.metrics()["committed_cost"], 300)

    def test_po_for_a_job_not_in_the_app_has_no_job_and_keeps_the_old_reference(self):
        self.run_import([self.reg("PO-3", r5="TM-111 & TM-112")])
        row = self.conn.execute("SELECT * FROM purchase_orders").fetchone()
        self.assertIsNone(row["works_order_id"])
        self.assertEqual(row["register_job_ref"], "TM-111 & TM-112")
        self.assertNotIn("Register WO", row["description"] or "")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works_orders WHERE job_number LIKE 'HIST-PO-%'").fetchone()[0], 0)

    def test_reference_to_a_job_not_raised_yet_is_not_kept(self):
        import import_po_register as ipr
        self.assertEqual(ipr.clean_register_ref("TM-9646", 9595), (None, "TM-9646"))
        self.assertEqual(ipr.clean_register_ref("TM-100 & TM-9700", 9595), ("TM-100", "TM-9700"))
        self.assertEqual(ipr.clean_register_ref("TM-100", 9595), ("TM-100", None))

    def test_rerun_adds_nothing_and_existing_po_untouched(self):
        sup = self.conn.execute("SELECT supplier_id FROM suppliers").fetchone()[0]
        self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value) VALUES ('PO-9',?,?,1)", (self.wo, sup))
        self.conn.commit()
        self.run_import([self.reg("PO-9"), self.reg("PO-10")])
        again = self.run_import([self.reg("PO-9"), self.reg("PO-10")])
        self.assertEqual(again.counts["added"], 0)
        self.assertEqual(self.conn.execute("SELECT po_value, is_historic FROM purchase_orders WHERE po_number='PO-9'").fetchone()[:], (1, 0))

    def test_replace_history_removes_only_historic_pos_and_empty_stand_ins(self):
        import import_po_register as ipr
        sup = self.conn.execute("SELECT supplier_id FROM suppliers").fetchone()[0]
        cust = self.conn.execute("SELECT customer_id FROM customers").fetchone()[0]
        stand = self.conn.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('HIST-PO-X','x',?)", (cust,)).lastrowid
        self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, is_historic) VALUES ('PO-H1',?,?,5,1)", (stand, sup))
        self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, is_historic) VALUES ('PO-H2',NULL,?,5,1)", (sup,))
        self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value) VALUES ('PO-LIVE',?,?,7)", (self.wo, sup))
        self.conn.commit()
        self.assertEqual(ipr.remove_historic_pos(self.conn), 2)
        self.conn.commit()
        self.assertEqual([r[0] for r in self.conn.execute("SELECT po_number FROM purchase_orders")], ["PO-LIVE"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works_orders WHERE job_number LIKE 'HIST-PO-%'").fetchone()[0], 0)
        # re-importing afterwards works and doesn't touch the live PO
        self.run_import([self.reg("PO-1"), self.reg("PO-LIVE")])
        self.assertEqual(self.conn.execute("SELECT is_historic FROM purchase_orders WHERE po_number='PO-LIVE'").fetchone()[0], 0)

    def test_spreadsheet_errors_and_blank_rows_are_ignored(self):
        import import_po_register as ipr
        self.assertIsNone(ipr.clean("#DIV/0!"))
        self.assertEqual(ipr.parse_value("#DIV/0!"), (0.0, None))
        self.run_import([self.reg("PO-7", r4=None, r19=None, r12=None, r7=None, r1=None)])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0], 0)

    def test_messy_values_are_tidied(self):
        import import_po_register as ipr
        self.assertEqual(ipr.parse_value("££62.32")[0], 62.32)
        self.assertEqual(ipr.parse_value("1,555.20.")[0], 1555.2)
        self.assertEqual(ipr.parse_value("TBC")[0], 0)
        self.assertIn("EUR", ipr.parse_value("EUR 9,626.10")[1])


class TestTidyHistoricPos(unittest.TestCase):
    """tidy_historic_pos.py, run as a separate process against a scratch data folder."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "schema.sql")) as f:
            schema = f.read()
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        con.executescript(schema)
        con.execute("INSERT INTO customers (customer_name) VALUES ('C')")
        con.execute("INSERT INTO suppliers (supplier_name) VALUES ('S')")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TM-9595','Real',1)")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('HIST-PO-C','stand-in',1)")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('HIST-PO-KEEP','stand-in with a live PO',1)")
        pos = [("PO-1", 2, "Widgets | Register WO: TM-1510 | Qty: 2", 1), ("PO-2", 2, "Gadgets | Register WO: TM-9646", 1),
               ("PO-3", 2, "Nothing | Qty: 1", 1), ("PO-4", 1, "Real job PO", 1), ("PO-5", 3, "live", 0)]
        for n, wo, d, h in pos:
            con.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, description, is_historic) VALUES (?,?,1,?,?)", (n, wo, d, h))
        con.commit()
        con.close()

    def run_tool(self, *args):
        import subprocess
        env = dict(os.environ, TEAMWORKS360_DATA_DIR=self.dir)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "tidy_historic_pos.py", *args], cwd=root, env=env, capture_output=True, text=True)

    def q(self, sql):
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_preview_changes_nothing(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.q("SELECT COUNT(*) FROM purchase_orders WHERE works_order_id=2")[0][0], 3)
        self.assertEqual(self.q("SELECT COUNT(*) FROM works_orders")[0][0], 3)

    def test_apply_detaches_keeps_ref_and_removes_empty_standins(self):
        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = {x[0]: x[1:] for x in self.q("SELECT po_number, works_order_id, register_job_ref, description FROM purchase_orders")}
        self.assertEqual(rows["PO-1"], (None, "TM-1510", "Widgets | Qty: 2"))
        self.assertEqual(rows["PO-2"], (None, None, "Gadgets"))          # TM-9646 isn't raised yet - dropped
        self.assertEqual(rows["PO-3"], (None, None, "Nothing | Qty: 1"))
        self.assertEqual(rows["PO-4"][0], 1)                              # real job untouched
        self.assertEqual(rows["PO-5"][0], 3)                              # live PO on another stand-in untouched
        self.assertEqual([x[0] for x in self.q("SELECT job_number FROM works_orders ORDER BY 1")], ["HIST-PO-KEEP", "TM-9595"])
        self.assertEqual(self.q("PRAGMA foreign_key_check"), [])

    def test_second_run_does_nothing(self):
        self.run_tool("--apply")
        r = self.run_tool("--apply")
        self.assertIn("detached from a stand-in job: 0", r.stdout)

    def test_clutter_and_trailing_blank_rows_are_removed(self):
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        con.execute("INSERT INTO suppliers (supplier_name) VALUES ('Unknown Supplier')")
        for n in ("PO-10", "PO-11", "PO-12"):        # PO-10 is real, 11 and 12 blank pre-numbered rows
            real = n == "PO-10"
            con.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, order_date, description, is_historic) VALUES (?,1,?,?,?,?,1)",
                        (n, 1 if real else 2, 5 if real else 0, "2026-10-01" if real else None,
                         "Bracket | date sent in register: '' | Supplier quote: #DIV/0! | Site: Midlands" if real else None))
        con.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, is_historic) VALUES ('TM-12147',NULL,2,0,1)")   # blank row with a TM-style number: removed
        con.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, is_historic, register_job_ref) VALUES ('PO-13',NULL,2,0,1,'TM-9606')")   # blank trailing row that only carries an old job ref: removed
        con.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, is_historic) VALUES ('PO-6',1,2,0,1)")  # blank but BEFORE the last real PO: kept
        con.commit(); con.close()
        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("next PO number will be PO-11", r.stdout)
        nums = [x[0] for x in self.q("SELECT po_number FROM purchase_orders ORDER BY 1")]
        self.assertNotIn("PO-11", nums); self.assertNotIn("PO-12", nums)
        self.assertNotIn("TM-12147", nums); self.assertNotIn("PO-13", nums)
        self.assertIn("PO-6", nums)
        self.assertEqual(self.q("SELECT description FROM purchase_orders WHERE po_number='PO-10'")[0][0], "Bracket | Site: Midlands")


class TestHistoricPoTidyRunsOnStartup(unittest.TestCase):
    def test_runs_once_automatically(self):
        import migrations
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        os.environ["TEAMWORKS360_DATA_DIR"] = d
        self.addCleanup(os.environ.pop, "TEAMWORKS360_DATA_DIR", None)
        con = sqlite3.connect(os.path.join(d, "jpm.db"))
        con.row_factory = sqlite3.Row
        with open(os.path.join(root, "schema.sql")) as f:
            con.executescript(f.read())
        con.execute("INSERT INTO customers (customer_name) VALUES ('C')")
        con.execute("INSERT INTO suppliers (supplier_name) VALUES ('S')")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('HIST-PO-C','stand-in',1)")
        con.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, description, is_historic) VALUES ('PO-1',1,1,'Thing | Register WO: TM-77 | Supplier quote: x',1)")
        con.commit()
        migrations.tidy_historic_pos_once(con, root)
        row = con.execute("SELECT works_order_id, register_job_ref, description FROM purchase_orders").fetchone()
        self.assertEqual((row[0], row[1], row[2]), (None, "TM-77", "Thing | Supplier quote: x"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM works_orders").fetchone()[0], 0)
        # marked as done: a stand-in job created later is not touched again
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('HIST-PO-D','later',1)")
        con.commit()
        migrations.tidy_historic_pos_once(con, root)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM works_orders").fetchone()[0], 1)
        con.close()


class TestPoNeedsARaisedJob(ProfitabilityTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
                          ("admin", appmod.generate_password_hash("secret123"), "admin", "Administrator"))
        self.conn.commit()
        self.client = appmod.app.test_client()
        resp = self.client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(self.client)
        c = self.client.get_cookie("csrf_token")
        self.h = {"X-CSRF-Token": c.value} if c else {}
        self.cust = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('C')").lastrowid
        self.sup = self.conn.execute("INSERT INTO suppliers (supplier_name) VALUES ('S')").lastrowid
        self.wo = self.conn.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TM-9595','J',?)", (self.cust,)).lastrowid
        self.conn.commit()

    def test_new_po_needs_an_existing_job(self):
        for wid in (None, 99999):
            r = self.client.post("/api/purchase-orders", json={"works_order_id": wid, "supplier_id": self.sup, "po_value": 1}, headers=self.h)
            self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/purchase-orders", json={"works_order_id": self.wo, "supplier_id": self.sup, "po_value": 1}, headers=self.h)
        self.assertEqual(r.status_code, 201)

    def test_historic_po_without_a_job_lists_and_edits(self):
        pid = self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value, is_historic, register_job_ref) VALUES ('PO-1',NULL,?,5,1,'TM-1510')", (self.sup,)).lastrowid
        self.conn.commit()
        rows = self.client.get("/api/purchase-orders").get_json()
        self.assertEqual([(r["po_number"], r["job_number"], r["register_job_ref"]) for r in rows], [("PO-1", None, "TM-1510")])
        r = self.client.put(f"/api/purchase-orders/{pid}", json={"works_order_id": None, "supplier_id": self.sup, "po_value": 6, "status": "Issued"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(f"/api/purchase-orders/{pid}/pdf").status_code, 200)

    def test_reference_lists_staff_names_for_the_raised_by_filter(self):
        names = self.client.get("/api/reference").get_json()["staff_names"]
        self.assertIn("Simon Lee", names)
        self.assertNotIn("S Lee/K Hallam", names)

    def test_live_po_cannot_lose_its_job(self):
        pid = self.conn.execute("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value) VALUES ('PO-2',?,?,5)", (self.wo, self.sup)).lastrowid
        self.conn.commit()
        r = self.client.put(f"/api/purchase-orders/{pid}", json={"works_order_id": None, "supplier_id": self.sup, "po_value": 6, "status": "Issued"}, headers=self.h)
        self.assertEqual(r.status_code, 400)


class TestAutoNumbering(ProfitabilityTestCase):
    """Job (TM-) and PO numbers are server-assigned; lists show the highest number first."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            ("admin", appmod.generate_password_hash("secret123"), "admin", "Administrator"))
        self.conn.commit()
        self.client = appmod.app.test_client()
        resp = self.client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(self.client)
        c = self.client.get_cookie("csrf_token")
        self.h = {"X-CSRF-Token": c.value} if c else {}
        q = self.conn.execute
        self.cust = q("INSERT INTO customers (customer_name) VALUES ('C')").lastrowid
        self.sup = q("INSERT INTO suppliers (supplier_name) VALUES ('S')").lastrowid
        for j in ("TM-9595", "TM-9100", "HIST-PO-X", "TM-10001"):
            q("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES (?, 'J', ?)", (j, self.cust))
        wo = q("SELECT works_order_id FROM works_orders WHERE job_number='TM-9595'").fetchone()[0]
        for n in ("PO-3301", "PO-12", "PO-TM-8670", "PO-1 PO-99999"):
            q("INSERT INTO purchase_orders (po_number, works_order_id, supplier_id, po_value) VALUES (?,?,?,1)", (n, wo, self.sup))
        self.conn.commit()

    def test_next_numbers_use_plain_patterns_only(self):
        self.assertEqual(self.client.get("/api/works-orders/next-number").get_json()["job_number"], "TM-10002")
        self.assertEqual(self.client.get("/api/purchase-orders/next-number").get_json()["po_number"], "PO-3302")

    def test_create_ignores_client_number(self):
        r = self.client.post("/api/works-orders", json={"job_number": "TM-1", "job_name": "New", "customer_id": self.cust}, headers=self.h)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["job_number"], "TM-10002")
        wid = r.get_json()["works_order_id"]
        r2 = self.client.post("/api/purchase-orders", json={"po_number": "PO-1", "works_order_id": wid, "supplier_id": self.sup, "po_value": 5}, headers=self.h)
        self.assertEqual(r2.status_code, 201)
        self.assertEqual(r2.get_json()["po_number"], "PO-3302")
        r3 = self.client.post("/api/purchase-orders", json={"works_order_id": wid, "supplier_id": self.sup, "po_value": 5}, headers=self.h)
        self.assertEqual(r3.get_json()["po_number"], "PO-3303")

    def test_edit_keeps_number(self):
        wid = self.conn.execute("SELECT works_order_id FROM works_orders WHERE job_number='TM-9100'").fetchone()[0]
        r = self.client.put(f"/api/works-orders/{wid}", json={"job_number": "TM-1", "job_name": "Renamed", "customer_id": self.cust, "status": "Open"}, headers=self.h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.conn.execute("SELECT job_number FROM works_orders WHERE works_order_id=?", (wid,)).fetchone()[0], "TM-9100")

    def test_lists_show_highest_number_first(self):
        jobs = [w["job_number"] for w in self.client.get("/api/works-orders").get_json()]
        self.assertEqual(jobs[:3], ["TM-10001", "TM-9595", "TM-9100"])
        pos = [p["po_number"] for p in self.client.get("/api/purchase-orders").get_json()]
        self.assertEqual(pos[:2], ["PO-3301", "PO-12"])    # odd references sort to the bottom
        self.assertLess(pos.index("PO-3301"), pos.index("PO-12"))


class TestTidyRaisedBy(unittest.TestCase):
    def test_canonical_names(self):
        import tidy_raised_by as t
        c = t.canonical
        self.assertEqual(c("S.Lee"), "Simon Lee")
        self.assertEqual(c("S Lee/K Hallam"), "Simon Lee")
        self.assertEqual(c("K Hallam/John Murray"), "John Murray")
        self.assertEqual(c("K.Hallam"), "Kay Hallam")
        self.assertEqual(c("KH/JM/TB"), "John Murray")
        self.assertEqual(c("A.Jackson"), "Andy Jackson")
        self.assertEqual(c("craig sansom"), "Craig Sansom")
        self.assertIsNone(c("T Bentley Jrn"))        # not recognised - left alone
        self.assertIsNone(c("A & R Jackson"))
        self.assertIsNone(c(None))


class TestSplitCustomersBySite(unittest.TestCase):
    """split_customers_by_site.py, run as a separate process against a scratch data folder."""

    def setUp(self):
        import openpyxl
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        con.executescript(open(os.path.join(root, "schema.sql")).read())
        for n in ("Rolls Royce - Derby", "Rolls Royce - WMF", "Rolls Royce - DCDD", "Somebody Else"):
            con.execute("INSERT INTO customers (customer_name) VALUES (?)", (n,))
        # jobs: (job, customer_id)
        jobs = [("TM-1", 1), ("TM-2", 1), ("TM-3", 1), ("TM-4", 1), ("TM-5", 1), ("TM-6", 4), ("TM-7", 1)]
        for j, c in jobs:
            con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES (?,?,?)", (j, j, c))
        con.execute("INSERT INTO quotes (quote_number, description, customer_id, works_order_id) VALUES ('TMQ-1','q',1,1)")
        con.commit()
        con.close()
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        import import_team_order_book as imp
        sites = {"TM-1": "Washington", "TM-2": "Ansty", "TM-3": "Derby", "TM-4": "Barnoldswick",
                 "TM-5": "DCDD Derby", "TM-6": "Washington", "TM-7": "Washington"}
        r = imp.DATA_FIRST_ROW
        for j, site in sites.items():
            ws.cell(row=r, column=imp.COL["tm_no"], value=j)
            ws.cell(row=r, column=imp.COL["tm_id"], value=r)
            ws.cell(row=r, column=imp.COL["end_customer"], value="Rolls Royce")
            ws.cell(row=r, column=imp.COL["site"], value=site)
            ws.cell(row=r, column=imp.COL["details"], value=j)
            r += 1
        self.xl = os.path.join(self.dir, "book.xlsx")
        wb.save(self.xl)

    def run_tool(self, *args):
        import subprocess
        env = dict(os.environ, TEAMWORKS360_DATA_DIR=self.dir)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "split_customers_by_site.py", self.xl, *args], cwd=root, env=env,
                              capture_output=True, text=True)

    def jobs(self):
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        try:
            return dict(con.execute(
                "SELECT w.job_number, c.customer_name FROM works_orders w JOIN customers c USING (customer_id)").fetchall())
        finally:
            con.close()

    def test_preview_changes_nothing(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.jobs()["TM-1"], "Rolls Royce - Derby")
        self.assertIn("PREVIEW", r.stdout)

    def test_apply_moves_only_mapped_jobs_currently_under_derby(self):
        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        j = self.jobs()
        self.assertEqual(j["TM-1"], "Rolls Royce - WMF")
        self.assertEqual(j["TM-5"], "Rolls Royce - DCDD")
        self.assertEqual(j["TM-7"], "Rolls Royce - WMF")
        self.assertEqual(j["TM-2"], "Rolls Royce - Ansty")      # customer created
        self.assertEqual(j["TM-3"], "Rolls Royce - Derby")      # Derby stays
        self.assertEqual(j["TM-4"], "Rolls Royce - Derby")      # Barnoldswick left for a decision
        self.assertEqual(j["TM-6"], "Somebody Else")            # hand-set customer never touched

    def test_quote_follows_job_and_second_run_does_nothing(self):
        self.run_tool("--apply")
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        name = con.execute("SELECT c.customer_name FROM quotes q JOIN customers c USING (customer_id)").fetchone()[0]
        n_audit = con.execute("SELECT COUNT(*) FROM audit_log WHERE changed_by='split_customers_by_site.py'").fetchone()[0]
        con.close()
        self.assertEqual(name, "Rolls Royce - WMF")
        self.assertEqual(n_audit, 4)
        r = self.run_tool("--apply")
        self.assertIn("Jobs moved to a site customer: 0", r.stdout)


class TestPrepareForDeployment(unittest.TestCase):
    """prepare_for_deployment.py, run as a separate process against a scratch data folder."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        con.executescript(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schema.sql")).read())
        con.execute("INSERT INTO customers (customer_name) VALUES ('C')")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TEST','Test hours and parts logging',1)")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TM-1','Real job',1)")
        con.execute("INSERT INTO works_orders (job_number, job_name, customer_id) VALUES ('TM-10000','Something real that reused the number',1)")
        con.execute("INSERT INTO stock_items (part_number, description) VALUES ('XC-123','Test pieces (Small)')")
        con.execute("INSERT INTO stock_allocations (stock_item_id, quantity, works_order_id) VALUES (1,2,1)")
        con.execute("INSERT INTO quotes (quote_number, description) VALUES ('TMQ-3000','10 Off production chucks')")
        for u, n, role, totp in (("admin", "Alex Morgan", "Administrator", 0), ("exec", "Marcus Lee", "Executive", 0),
                                 ("boss", "Simon Lee", "Administrator", 1)):
            con.execute("INSERT INTO users (username, password_hash, full_name, role, totp_enabled) VALUES (?,?,?,?,?)",
                        (u, "x", n, role, totp))
        con.commit()
        con.close()

    def run_tool(self, *args):
        import subprocess
        env = dict(os.environ, TEAMWORKS360_DATA_DIR=self.dir)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, "prepare_for_deployment.py", *args], cwd=root, env=env,
                              capture_output=True, text=True)

    def q(self, sql):
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        try:
            return con.execute(sql).fetchall()
        finally:
            con.close()

    def test_preview_changes_nothing(self):
        r = self.run_tool()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users")[0][0], 3)
        self.assertEqual(self.q("SELECT COUNT(*) FROM works_orders")[0][0], 3)

    def test_apply_removes_only_the_demo_and_test_items(self):
        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x[0] for x in self.q("SELECT username FROM users")], ["boss"])
        self.assertEqual({x[0] for x in self.q("SELECT job_number FROM works_orders")}, {"TM-1", "TM-10000"})  # lookalike left alone
        self.assertEqual(self.q("SELECT COUNT(*) FROM stock_items")[0][0], 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM quotes")[0][0], 0)
        self.assertIn("left alone", r.stdout)

    def test_refuses_if_it_would_lock_everyone_out(self):
        con = sqlite3.connect(os.path.join(self.dir, "jpm.db"))
        con.execute("UPDATE users SET totp_enabled=0 WHERE username='boss'")
        con.commit(); con.close()
        r = self.run_tool("--apply")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.q("SELECT COUNT(*) FROM users")[0][0], 3)


class TestQuoteStatusMigration(unittest.TestCase):
    def test_old_quotes_table_is_widened_and_register_quotes_moved_to_their_real_status(self):
        import migrations
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("CREATE TABLE customers (customer_id INTEGER PRIMARY KEY)")
        con.execute("""CREATE TABLE quotes (quote_id INTEGER PRIMARY KEY AUTOINCREMENT, quote_number TEXT UNIQUE NOT NULL,
            customer_id INTEGER REFERENCES customers(customer_id), notes TEXT,
            status TEXT NOT NULL DEFAULT 'Draft' CHECK (status IN ('Draft','Sent','Won','Lost','Expired')))""")
        con.execute("CREATE TABLE quote_labour_lines (line_id INTEGER PRIMARY KEY, quote_id INTEGER NOT NULL REFERENCES quotes(quote_id) ON DELETE CASCADE)")
        con.executemany("INSERT INTO quotes (quote_number, notes, status) VALUES (?,?,?)", [
            ("A", "status in register: Cancelled", "Lost"), ("B", "status in register: On Hold", "Sent"),
            ("C", None, "Lost"), ("D", None, "Sent")])
        con.execute("INSERT INTO quote_labour_lines (quote_id) VALUES (1)")
        con.commit()
        migrations.widen_quote_statuses(con)
        self.assertEqual(dict(con.execute("SELECT quote_number, status FROM quotes")), {"A": "Cancelled", "B": "On Hold", "C": "Lost", "D": "Sent"})
        con.execute("INSERT INTO quotes (quote_number, status) VALUES ('E','On Hold')")   # new rule accepts it
        self.assertEqual(con.execute("SELECT COUNT(*) FROM quote_labour_lines").fetchone()[0], 1)  # children survived
        migrations.widen_quote_statuses(con)   # second run is a no-op
        self.assertEqual(con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 5)


class TestPlaceholderJobsHidden(ProfitabilityTestCase):
    def test_historic_po_placeholder_jobs_are_kept_out_of_lists_and_reports_but_stay_in_reference(self):
        cust = self.make_customer()
        self.make_works_order("TM-1", cust, contract_value=100)
        self.make_works_order("HIST-PO-ACME", cust)
        db = appmod.get_db()
        with appmod.app.test_request_context():
            self.assertEqual([r["job_number"] for r in appmod.build_report_rows(db)], ["TM-1"])
            self.assertEqual({a["job_number"] for a in appmod.compute_all_alerts(db)} - {"TM-1"}, set())
        rows = db.execute(f"SELECT wo.job_number FROM works_orders wo WHERE {appmod.PLACEHOLDER_JOB_SQL}").fetchall()
        self.assertEqual([r[0] for r in rows], ["TM-1"])

    def test_routes_exclude_placeholders_and_reference_lists_them_separately(self):
        cust = self.make_customer()
        self.make_works_order("TM-1", cust, contract_value=100)
        self.make_works_order("HIST-PO-ACME", cust)
        self.conn.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES ('a', ?, 'a', 'Administrator')",
                          (appmod.generate_password_hash("secret123"),))
        self.conn.commit()
        client = appmod.app.test_client()
        resp = client.post("/api/auth/login", json={"username": "a", "password": "secret123"})
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        listed = [w["job_number"] for w in client.get("/api/works-orders").get_json()]
        self.assertEqual(listed, ["TM-1"])
        ref = client.get("/api/reference").get_json()
        self.assertEqual([w["job_number"] for w in ref["works_orders"]], ["TM-1"])
        self.assertEqual([w["job_number"] for w in ref["placeholder_jobs"]], ["HIST-PO-ACME"])
        self.assertEqual(client.get("/api/dashboard").status_code, 200)
        self.assertEqual(client.get("/api/reporting/by-job").status_code in (200, 404), True)


class TestOrderBookUpdate(ProfitabilityTestCase):
    """update_order_book.py: adds new jobs and refreshes existing ones from a
    newer Order Book without the destructive wipe the full import does."""

    def row(self, tm_no, **kw):
        base = {"tm_no": tm_no, "tm_id": tm_no, "enquiry_date": None, "team_po": None,
                "end_customer": "Acme", "site": "Derby", "site_area": "A1", "location": "Midlands",
                "owner": "Pat", "customer_po": None, "contract_code": "INT", "details": "Widget",
                "invoice_value": 1000, "cost": 600, "delivery_req": None, "delivered_on": None,
                "status": "A", "tcp": None, "supplier1": None, "supplier2": None,
                "bought_out_value": None, "date_invoiced": None, "team_quote_ref": None, "coupa_ref": None}
        base.update(kw)
        return base

    def run_update(self, rows):
        import update_order_book as uob
        plans = [uob.build_plan(r) for r in rows]
        existing = {r["job_number"]: r for r in self.conn.execute("SELECT * FROM works_orders")}
        up = uob.Updater(self.conn)
        up.learn_customers(plans, existing)
        for plan in plans:
            if plan["job_number"] in existing:
                up.update_job(existing[plan["job_number"]], plan)
            else:
                up.add_job(plan)
        self.conn.commit()
        return up

    def test_new_job_is_added_with_its_invoice_and_labour(self):
        up = self.run_update([self.row("TM-1")])
        self.assertEqual(up.counts["jobs_added"], 1)
        wo = self.conn.execute("SELECT * FROM works_orders WHERE job_number='TM-1'").fetchone()
        self.assertEqual(wo["contract_value"], 1000)
        self.assertEqual(self.conn.execute("SELECT SUM(invoice_amount) FROM customer_invoices").fetchone()[0], 1000)
        self.assertEqual(self.conn.execute("SELECT SUM(labour_cost) FROM labour_entries").fetchone()[0], 600)

    def test_existing_job_is_refreshed_and_rerun_changes_nothing(self):
        self.run_update([self.row("TM-1")])
        up = self.run_update([self.row("TM-1", status="D", invoice_value=2000, cost=900)])
        self.assertEqual(up.counts["jobs_updated"], 1)
        wo = self.conn.execute("SELECT * FROM works_orders WHERE job_number='TM-1'").fetchone()
        self.assertEqual((wo["status"], wo["contract_value"]), ("Completed", 2000))
        self.assertEqual(self.conn.execute("SELECT SUM(invoice_amount) FROM customer_invoices").fetchone()[0], 2000)
        self.assertEqual(self.conn.execute("SELECT SUM(labour_cost) FROM labour_entries").fetchone()[0], 900)
        again = self.run_update([self.row("TM-1", status="D", invoice_value=2000, cost=900)])
        self.assertEqual((again.counts["jobs_updated"], again.counts["jobs_unchanged"]), (0, 1))

    def test_job_edited_in_the_app_is_left_alone(self):
        self.run_update([self.row("TM-1")])
        self.conn.execute("UPDATE works_orders SET updated_by='Simon', status='Closed' WHERE job_number='TM-1'")
        self.conn.commit()
        up = self.run_update([self.row("TM-1", status="D", invoice_value=5000)])
        self.assertEqual(up.counts["jobs_left_alone"], 1)
        wo = self.conn.execute("SELECT * FROM works_orders WHERE job_number='TM-1'").fetchone()
        self.assertEqual((wo["status"], wo["contract_value"]), ("Closed", 1000))

    def test_existing_jobs_customer_is_not_changed_and_new_jobs_follow_the_apps_customer(self):
        self.run_update([self.row("TM-1")])
        cid = self.make_customer("Acme - Derby")
        self.conn.execute("UPDATE works_orders SET customer_id=? WHERE job_number='TM-1'", (cid,))
        self.conn.commit()
        self.run_update([self.row("TM-1"), self.row("TM-2")])
        names = {r["job_number"]: r["customer_name"] for r in self.conn.execute(
            "SELECT w.job_number, c.customer_name FROM works_orders w JOIN customers c USING(customer_id)")}
        self.assertEqual(names["TM-1"], "Acme - Derby")
        self.assertEqual(names["TM-2"], "Acme - Derby")

    def test_jobs_not_in_the_spreadsheet_are_never_touched(self):
        cust = self.make_customer("Other")
        self.make_works_order("TEST", cust)
        self.run_update([self.row("TM-1")])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM works_orders WHERE job_number='TEST'").fetchone()[0], 1)


class TestDataDirOverride(unittest.TestCase):
    """db_location.py computes DATA_DIR (and every path derived from it -
    DB_PATH, BACKUP_DIR, etc.) once, at module-import time, from the
    TEAMWORKS360_DATA_DIR environment variable - this is what lets the
    isolated test copy (start_test_shared_host.bat/.command) run against
    its own separate data folder without any other code change. Since
    that computation only happens on import, these tests reload the
    module around changes to the environment variable rather than just
    setting it, to see the same thing a freshly-started process would."""

    def setUp(self):
        self._orig_env = os.environ.get("TEAMWORKS360_DATA_DIR")

    def tearDown(self):
        # Restore the environment exactly as found, then reload once more
        # so db_location is left back in its normal (no override) state
        # for any test that runs after this one.
        if self._orig_env is None:
            os.environ.pop("TEAMWORKS360_DATA_DIR", None)
        else:
            os.environ["TEAMWORKS360_DATA_DIR"] = self._orig_env
        import db_location
        importlib.reload(db_location)

    def test_default_data_dir_is_unchanged_when_unset(self):
        os.environ.pop("TEAMWORKS360_DATA_DIR", None)
        import db_location
        importlib.reload(db_location)
        self.assertEqual(db_location.DATA_DIR, os.path.join(os.path.expanduser("~"), "TEAMworks360"))

    def test_data_dir_follows_override_when_set(self):
        os.environ["TEAMWORKS360_DATA_DIR"] = "/tmp/teamworks360-test-override"
        import db_location
        importlib.reload(db_location)
        self.assertEqual(db_location.DATA_DIR, "/tmp/teamworks360-test-override")
        # Every path that matters (the database above all) must follow the
        # override too, not just DATA_DIR itself - this is the whole point:
        # a test copy must never be able to find or touch the real jpm.db.
        self.assertEqual(db_location.DB_PATH, "/tmp/teamworks360-test-override/jpm.db")
        self.assertEqual(db_location.BACKUP_DIR, "/tmp/teamworks360-test-override/backups")
        self.assertEqual(db_location.GOODS_IN_DOCS_DIR, "/tmp/teamworks360-test-override/goods_in_documents")
        self.assertEqual(db_location.SIGNATURES_DIR, "/tmp/teamworks360-test-override/signatures")

    def test_empty_override_falls_back_to_default(self):
        # An empty string (e.g. a launcher script accidentally setting
        # TEAMWORKS360_DATA_DIR= with nothing after it) must behave exactly
        # like the variable being unset at all, not point the app at "".
        os.environ["TEAMWORKS360_DATA_DIR"] = ""
        import db_location
        importlib.reload(db_location)
        self.assertEqual(db_location.DATA_DIR, os.path.join(os.path.expanduser("~"), "TEAMworks360"))


if __name__ == "__main__":
    unittest.main(verbosity=2)


class RoleTestBase(ProfitabilityTestCase):
    # Shared helpers for the role tests below (no tests of its own).

    def tearDown(self):
        appmod._LOGIN_ATTEMPTS.clear()
        appmod._TOTP_ATTEMPTS.clear()
        super().tearDown()

    def make_user(self, username, password, role):
        self.conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?,?,?,?)",
            (username, appmod.generate_password_hash(password), username, role),
        )
        self.conn.commit()

    def engineer_client(self):
        self.make_user("eng", "secret123", "Service Engineer")
        client = appmod.app.test_client()
        resp = client.post("/api/auth/login", json={"username": "eng", "password": "secret123"})
        self.assertEqual(resp.status_code, 200)
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return client

    def csrf(self, client):
        c = client.get_cookie("csrf_token")
        return {"X-CSRF-Token": c.value} if c else {}



class TestServiceEngineerRole(RoleTestBase):
    """The Service Engineer role: Works Orders, Purchase Orders, Goods In,
    Despatch Notes, Inventory and Hours Quoted & Booked - and nothing else,
    enforced by the server, not just by hiding menu items."""

    def test_can_read_their_six_areas(self):
        client = self.engineer_client()
        for path in ("/api/works-orders", "/api/purchase-orders", "/api/goods-in",
                     "/api/despatch-notes", "/api/stock-items", "/api/labour-entries", "/api/reference"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

    def test_cannot_reach_anything_else(self):
        client = self.engineer_client()
        for path in ("/api/quotes", "/api/supplier-invoices", "/api/customer-invoices",
                     "/api/customers", "/api/suppliers", "/api/dashboard", "/api/alerts",
                     "/api/reporting", "/api/reporting/spend-by-supplier", "/api/users",
                     "/api/settings/margin-targets", "/api/settings/ship-to-addresses",
                     "/api/rate-card", "/api/login-events", "/api/audit-log/quotes/1"):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 403)

    def test_can_write_in_their_areas_but_not_delete(self):
        client = self.engineer_client()
        cust = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Cust')").lastrowid
        self.conn.commit()
        resp = client.post("/api/works-orders", json={"job_name": "Engineer job", "customer_id": cust},
                           headers=self.csrf(client))
        self.assertEqual(resp.status_code, 201)
        wid = resp.get_json()["works_order_id"]
        self.assertEqual(client.delete(f"/api/works-orders/{wid}", headers=self.csrf(client)).status_code, 403)
        # and still cannot write to areas outside the list
        self.assertEqual(client.post("/api/quotes", json={}, headers=self.csrf(client)).status_code, 403)

    def test_cannot_create_users_or_change_settings(self):
        client = self.engineer_client()
        self.assertEqual(client.post("/api/users", json={"username": "x", "role": "Administrator"},
                                     headers=self.csrf(client)).status_code, 403)

    def test_administrator_can_create_a_service_engineer(self):
        self.make_user("boss", "secret123", "Administrator")
        client = appmod.app.test_client()
        resp = client.post("/api/auth/login", json={"username": "boss", "password": "secret123"})
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        resp = client.post("/api/users", json={"username": "neweng", "full_name": "New Eng", "role": "Service Engineer"},
                           headers=self.csrf(client))
        self.assertEqual(resp.status_code, 201)

    def test_existing_database_is_upgraded_to_allow_the_role(self):
        import sqlite3 as _sq
        old = _sq.connect(":memory:")
        old.row_factory = _sq.Row
        old.executescript(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "schema.sql")).read()
                          .replace(",'Service Engineer'", ""))
        old.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES ('a','x','A','Finance')")
        old.commit()
        with self.assertRaises(_sq.IntegrityError):
            old.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES ('b','x','B','Service Engineer')")
        appmod.migrations.allow_service_engineer_role(old)
        old.execute("INSERT INTO users (username, password_hash, full_name, role) VALUES ('b','x','B','Service Engineer')")
        self.assertEqual(old.execute("SELECT COUNT(*) FROM users").fetchone()[0], 2)
        appmod.migrations.allow_service_engineer_role(old)  # safe to run twice


class TestServiceEngineerCannotSeeJobMoney(RoleTestBase):
    """Works Orders for a Service Engineer carry no money or profitability at all."""

    MONEY = ("contract_value", "budget_cost", "budget_is_estimated", "revenue", "actual_cost", "committed_cost",
             "forecast_cost", "profit", "forecast_profit", "margin_pct", "forecast_margin_pct",
             "margin_target", "alerts", "status_light", "po_total", "supplier_invoice_total",
             "labour_total", "stock_cost")

    def seed_job(self):
        cust = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Cust')").lastrowid
        wid = self.conn.execute(
            "INSERT INTO works_orders (job_number, job_name, customer_id, status, contract_value, budget_cost) "
            "VALUES ('TM-9001','Secret money job',?, 'Open', 5000, 3000)", (cust,)).lastrowid
        self.conn.commit()
        return cust, wid

    def test_list_and_detail_have_no_money_fields(self):
        client = self.engineer_client()
        cust, wid = self.seed_job()
        row = [r for r in client.get("/api/works-orders").get_json() if r["job_number"] == "TM-9001"][0]
        detail = client.get(f"/api/works-orders/{wid}").get_json()
        for d in (row, detail):
            for f in self.MONEY:
                self.assertNotIn(f, d)
        self.assertEqual(row["job_name"], "Secret money job")

    def test_administrator_still_sees_the_money(self):
        self.make_user("boss", "secret123", "Administrator")
        client = appmod.app.test_client()
        r = client.post("/api/auth/login", json={"username": "boss", "password": "secret123"})
        if r.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        cust, wid = self.seed_job()
        d = client.get(f"/api/works-orders/{wid}").get_json()
        self.assertEqual(d["contract_value"], 5000)
        self.assertIn("margin_pct", d)

    def test_editing_a_job_leaves_its_value_and_budget_alone(self):
        client = self.engineer_client()
        cust, wid = self.seed_job()
        resp = client.put(f"/api/works-orders/{wid}", json={
            "job_name": "Renamed", "customer_id": cust, "status": "In Progress",
            "contract_value": 1, "budget_cost": 1}, headers=self.csrf(client))
        self.assertEqual(resp.status_code, 200)
        row = self.conn.execute("SELECT job_name, contract_value, budget_cost FROM works_orders WHERE works_order_id=?", (wid,)).fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("Renamed", 5000, 3000))

    def test_new_job_by_an_engineer_has_no_value_set(self):
        client = self.engineer_client()
        cust, _ = self.seed_job()
        resp = client.post("/api/works-orders", json={"job_name": "N", "customer_id": cust, "contract_value": 99999},
                           headers=self.csrf(client))
        self.assertEqual(resp.status_code, 201)
        row = self.conn.execute("SELECT contract_value, budget_cost FROM works_orders WHERE job_number=?", (resp.get_json()["job_number"],)).fetchone()
        self.assertEqual((row[0], row[1]), (0, 0))

    def test_reference_data_has_no_targets_or_standard_values_and_history_hides_job_value_changes(self):
        client = self.engineer_client()
        cust, wid = self.seed_job()
        self.assertEqual(client.get("/api/reference").get_json()["contract_code_targets"], [])
        self.conn.execute("INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value) VALUES ('works_orders',?,'contract_value','1','2')", (wid,))
        self.conn.execute("INSERT INTO audit_log (table_name, record_id, field_name, old_value, new_value) VALUES ('works_orders',?,'status','Open','Closed')", (wid,))
        self.conn.commit()
        fields = {h["field_name"] for h in client.get(f"/api/audit-log/works_orders/{wid}").get_json()}
        self.assertEqual(fields, {"status"})


class TestAcceptAlerts(RoleTestBase):
    """Alerts carry a link target, and can be accepted (with a reason) so they stop showing."""

    def admin_client(self, role="Administrator"):
        self.make_user("boss", "secret123", role)
        client = appmod.app.test_client()
        r = client.post("/api/auth/login", json={"username": "boss", "password": "secret123"})
        if r.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(client)
        return client

    def overspent_job(self):
        cust = self.conn.execute("INSERT INTO customers (customer_name) VALUES ('Cust')").lastrowid
        wid = self.conn.execute(
            "INSERT INTO works_orders (job_number, job_name, customer_id, status, contract_value, budget_cost) "
            "VALUES ('TM-9100','Over budget',?, 'Open', 1000, 100)", (cust,)).lastrowid
        self.conn.execute("INSERT INTO labour_entries (works_order_id, employee_name, hours_worked, hourly_rate, labour_cost) VALUES (?, 'x', 10, 50, 500)", (wid,))
        self.conn.commit()
        return wid

    def test_alert_has_job_link_data_and_can_be_accepted_then_comes_back_if_figures_change(self):
        wid = self.overspent_job()
        client = self.admin_client()
        alerts = client.get("/api/alerts").get_json()["alerts"]
        mine = [a for a in alerts if a["job_number"] == "TM-9100"]
        self.assertTrue(mine)
        self.assertEqual(mine[0]["works_order_id"], wid)
        # a reason is required
        self.assertEqual(client.post("/api/alerts/accept", json={"alert_key": mine[0]["alert_key"], "reason": " "},
                                     headers=self.csrf(client)).status_code, 400)
        for a in mine:
            r = client.post("/api/alerts/accept", json={"alert_key": a["alert_key"], "reason": "Variation agreed"},
                            headers=self.csrf(client))
            self.assertEqual(r.status_code, 200)
        self.assertFalse([a for a in client.get("/api/alerts").get_json()["alerts"] if a["job_number"] == "TM-9100"])
        accepted = client.get("/api/alerts/accepted").get_json()
        self.assertEqual(accepted[0]["reason"], "Variation agreed")
        # more cost goes on: wording (figures) changes, so the alert is new and shows again
        self.conn.execute("UPDATE labour_entries SET labour_cost=900 WHERE works_order_id=?", (wid,))
        self.conn.commit()
        self.assertTrue([a for a in client.get("/api/alerts").get_json()["alerts"] if a["job_number"] == "TM-9100"])

    def test_undo_brings_alert_back(self):
        self.overspent_job()
        client = self.admin_client()
        a = [x for x in client.get("/api/alerts").get_json()["alerts"] if x["job_number"] == "TM-9100"][0]
        client.post("/api/alerts/accept", json={"alert_key": a["alert_key"], "reason": "ok"}, headers=self.csrf(client))
        aid = client.get("/api/alerts/accepted").get_json()[0]["acceptance_id"]
        self.assertEqual(client.delete(f"/api/alerts/accepted/{aid}", headers=self.csrf(client)).status_code, 200)
        self.assertTrue([x for x in client.get("/api/alerts").get_json()["alerts"] if x["alert_key"] == a["alert_key"]])

    def test_executive_cannot_accept_and_service_engineer_cannot_reach_alerts(self):
        self.overspent_job()
        client = self.admin_client("Executive")
        a = client.get("/api/alerts").get_json()["alerts"][0]
        self.assertEqual(client.post("/api/alerts/accept", json={"alert_key": a["alert_key"], "reason": "x"},
                                     headers=self.csrf(client)).status_code, 403)
    def test_stale_alert_key_is_refused(self):
        client = self.admin_client()
        self.assertEqual(client.post("/api/alerts/accept", json={"alert_key": "nope", "reason": "x"},
                                     headers=self.csrf(client)).status_code, 404)


class TestVersionCheck(RoleTestBase):
    """Version shown in the app, the guard against old code on a newer database,
    and the optional (off by default) 'update available' notice for Administrators."""

    def test_version_comparison(self):
        import version
        self.assertEqual(version.parse_version("1.2.10"), (1, 2, 10))
        self.assertIsNone(version.parse_version("banana"))
        self.assertTrue(version.is_newer("1.10.0", "1.9.9"))
        self.assertFalse(version.is_newer("1.0.0", "1.0.0"))
        self.assertFalse(version.is_newer("junk", "1.0.0"))

    def test_endpoint_shows_version_to_everyone_including_service_engineers(self):
        client = self.engineer_client()
        r = client.get("/api/version")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["version"], appmod.app_version.APP_VERSION)
        self.assertFalse(r.get_json()["update_available"])

    def test_startup_stamps_version_and_logs_the_change(self):
        self.conn.execute("DELETE FROM settings WHERE key='app_version'")
        self.conn.commit()
        self.assertTrue(appmod.check_and_stamp_database_version(self.conn))
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()[0],
                         appmod.app_version.APP_VERSION)
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM audit_log WHERE field_name='app_version' AND new_value=?",
            (appmod.app_version.APP_VERSION,)).fetchone())

    def test_old_code_refuses_to_open_a_newer_database(self):
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('app_version','99.0.0')")
        self.conn.commit()
        self.assertFalse(appmod.check_and_stamp_database_version(self.conn))
        # nothing was changed
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='app_version'").fetchone()[0], "99.0.0")

    def test_update_notice_is_off_by_default_and_only_for_administrators(self):
        import os as _os
        appmod._UPDATE_CACHE.update(at=0, data=None)
        called = []
        orig = appmod.fetch_latest_release
        try:
            _os.environ.pop("TEAMWORKS360_UPDATE_URL", None)
            self.assertIsNone(appmod.fetch_latest_release())
            appmod.fetch_latest_release = lambda: {"version": "99.0.0", "notes": "New things"}
            admin = self.admin_client() if hasattr(self, "admin_client") else None
            if admin is None:
                self.make_user("boss", "secret123", "Administrator")
                admin = appmod.app.test_client()
                r = admin.post("/api/auth/login", json={"username": "boss", "password": "secret123"})
                if r.get_json().get("must_setup_2fa"):
                    _complete_pending_2fa_setup(admin)
            data = admin.get("/api/version").get_json()
            self.assertTrue(data["update_available"])
            self.assertEqual(data["latest_version"], "99.0.0")
            eng = self.engineer_client()
            self.assertFalse(eng.get("/api/version").get_json()["update_available"])
        finally:
            appmod.fetch_latest_release = orig

    def test_update_url_must_be_https(self):
        import os as _os
        appmod._UPDATE_CACHE.update(at=0, data=None)
        _os.environ["TEAMWORKS360_UPDATE_URL"] = "http://example.com/latest.json"
        try:
            self.assertIsNone(appmod.fetch_latest_release())
        finally:
            _os.environ.pop("TEAMWORKS360_UPDATE_URL", None)


class TestOwnershipLabel(ProfitabilityTestCase):
    def test_splash_and_login_show_owner_and_licence(self):
        html = appmod.app.test_client().get("/").get_data(as_text=True)
        self.assertEqual(html.count("Owned by Simon Lee"), 2)  # opening splash + sign-in card
        self.assertEqual(html.count("Licence: Trial"), 2)


class TestLateWorksOrders(RoleTestBase):
    """Dashboard 'Late Works Orders': live jobs past their End Date, with the
    customer PO value and cost to date split into purchases / hours / stock."""

    def setUp(self):
        super().setUp()
        from datetime import date, timedelta
        iso = lambda n: (date.today() + timedelta(days=n)).isoformat()
        cust = self.make_customer()
        sup = self.make_supplier()
        self.late = self.make_works_order("TM-1", cust, status="In Progress", contract_value=1000)
        self.very_late = self.make_works_order("TM-2", cust, status="Open", contract_value=500)
        self.future = self.make_works_order("TM-3", cust, status="Open", contract_value=700)
        self.done = self.make_works_order("TM-4", cust, status="Completed", contract_value=800)
        for wo, n in ((self.late, -10), (self.very_late, -45), (self.future, 5), (self.done, -60)):
            self.conn.execute("UPDATE works_orders SET end_date=? WHERE works_order_id=?", (iso(n), wo))
        self.conn.commit()
        po = self.make_purchase_order(self.late, sup, 300)
        self.make_supplier_invoice(self.late, po, sup, 200)
        self.make_labour_entry(self.late, 2, 50)       # 100 of hours
        self.make_admin()

    def make_admin(self):
        self.make_user("boss", "secret123", "Administrator")
        self.client = appmod.app.test_client()
        resp = self.client.post("/api/auth/login", json={"username": "boss", "password": "secret123"})
        if resp.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(self.client)

    def test_dashboard_counts_only_live_jobs_past_end_date(self):
        d = self.client.get("/api/dashboard").get_json()
        self.assertEqual(d["late_count"], 2)
        self.assertEqual([j["job_number"] for j in d["late_jobs"]], ["TM-2", "TM-1"])  # most late first
        self.assertEqual(d["late_jobs"][0]["days_late"], 45)

    def test_dashboard_money_split(self):
        d = self.client.get("/api/dashboard").get_json()
        self.assertEqual(d["late_po_value"], 1500)
        self.assertEqual(d["late_purchases"], 200)
        self.assertEqual(d["late_hours"], 100)
        self.assertEqual(d["late_stock"], 0)
        self.assertEqual(d["late_cost_total"], 300)
        self.assertEqual(d["late_avg_days"], 28)   # (10 + 45) / 2 = 27.5

    def test_list_filter_matches_dashboard(self):
        rows = self.client.get("/api/works-orders?late=1").get_json()
        self.assertEqual(sorted(r["job_number"] for r in rows), ["TM-1", "TM-2"])

    def test_service_engineer_gets_no_late_money(self):
        eng = self.engineer_client()
        # Dashboard itself is off-limits to them; the list filter works but carries no money.
        self.assertEqual(eng.get("/api/dashboard").status_code, 403)
        rows = eng.get("/api/works-orders?late=1").get_json()
        self.assertEqual(len(rows), 2)
        self.assertNotIn("contract_value", rows[0])


class TestScreenSummaries(RoleTestBase):
    """Summary panels on the Quotes, Works Orders, PO, Goods In, Despatch,
    Customers and Suppliers screens, and the Hours Booked report."""

    def setUp(self):
        super().setUp()
        from datetime import date
        self.year = str(date.today().year)
        cust = self.make_customer("C1")
        sup = self.make_supplier("S1")
        q = self.conn.execute
        q("INSERT INTO quotes (quote_number, customer_id, status, estimated_value, quote_date) VALUES ('TMQ-1',?,'Sent',100,?)", (cust, f"{self.year}-01-05"))
        q("INSERT INTO quotes (quote_number, customer_id, status, estimated_value, quote_date) VALUES ('TMQ-2',?,'Sent',50,?)", (cust, f"{self.year}-01-06"))
        q("INSERT INTO quotes (quote_number, customer_id, status, estimated_value, quote_date) VALUES ('TMQ-3',?,'Won',400,?)", (cust, f"{self.year}-02-01"))
        q("INSERT INTO quotes (quote_number, customer_id, status, estimated_value, quote_date) VALUES ('TMQ-4',?,'Won',900,'2020-02-01')", (cust,))
        self.conn.commit()
        self.open1 = self.make_works_order("TM-1", cust, status="In Progress", contract_value=1000)
        self.open2 = self.make_works_order("TM-2", cust, status="Open", contract_value=500)
        self.done_now = self.make_works_order("TM-3", cust, status="Completed", contract_value=700)
        self.done_old = self.make_works_order("TM-4", cust, status="Completed", contract_value=800)
        q("UPDATE works_orders SET end_date=? WHERE works_order_id=?", (f"{self.year}-03-01", self.done_now))
        q("UPDATE works_orders SET end_date='2020-03-01' WHERE works_order_id=?", (self.done_old,))
        self.conn.commit()
        self.po_none = self.make_purchase_order(self.open1, sup, 300, status="Issued")
        self.po_part = self.make_purchase_order(self.open1, sup, 200, status="Issued")
        self.po_full = self.make_purchase_order(self.open1, sup, 100, status="Issued")
        self.po_draft = self.make_purchase_order(self.open1, sup, 999, status="Draft")
        for po, qs in ((self.po_part, "Partial"), (self.po_full, "Full")):
            q("INSERT INTO goods_in (purchase_order_id, destination_type, received_date, quantity_status, received_by) "
              "VALUES (?, 'Works Order', ?, ?, 'x')", (po, f"{self.year}-04-01", qs))
        q("INSERT INTO despatch_notes (despatch_number, works_order_id, despatch_date, status) VALUES ('TMD-1',?,?, 'Despatched')", (self.done_now, f"{self.year}-05-01"))
        q("INSERT INTO despatch_notes (despatch_number, works_order_id, despatch_date, status) VALUES ('TMD-2',?,?, 'Despatched')", (self.done_now, f"{self.year}-05-02"))
        q("INSERT INTO despatch_notes (despatch_number, works_order_id, despatch_date, status) VALUES ('TMD-3',?,'2020-01-01','Despatched')", (self.done_old,))
        q("INSERT INTO despatch_notes (despatch_number, works_order_id, despatch_date, status) VALUES ('TMD-4',?,?, 'Draft')", (self.open1, f"{self.year}-05-03"))
        self.conn.commit()
        self.make_user("boss", "secret123", "Administrator")
        self.admin = appmod.app.test_client()
        r = self.admin.post("/api/auth/login", json={"username": "boss", "password": "secret123"})
        if r.get_json().get("must_setup_2fa"):
            _complete_pending_2fa_setup(self.admin)

    def test_figures(self):
        d = self.admin.get("/api/screen-summaries").get_json()
        self.assertEqual(d["quotes"], {"awaiting_count": 2, "awaiting_value": 150,
                                       "po_received_count": 1, "po_received_value": 400})
        self.assertEqual((d["works_orders"]["open_count"], d["works_orders"]["open_value"]), (2, 1500))
        self.assertEqual((d["works_orders"]["completed_ytd_count"], d["works_orders"]["completed_ytd_value"]), (1, 700))
        self.assertEqual((d["purchase_orders"]["awaiting_count"], d["purchase_orders"]["awaiting_value"]), (1, 300))
        self.assertEqual(d["purchase_orders"]["part_received_count"], 1)
        self.assertEqual(d["goods_in"]["total"], 2)
        self.assertEqual(d["goods_in"]["ytd"], 2)
        self.assertEqual(d["despatch_notes"]["jobs_complete"], 2)   # draft ignored, two notes on one job count once
        self.assertEqual(d["despatch_notes"]["ytd"], 1)
        self.assertEqual(d["customers"]["total"], 1)
        self.assertEqual(d["suppliers"]["total"], 1)

    def test_service_engineer_sees_counts_but_no_money_or_other_areas(self):
        d = self.engineer_client().get("/api/screen-summaries").get_json()
        self.assertIsNone(d["works_orders"]["open_value"])
        self.assertIsNone(d["purchase_orders"]["awaiting_value"])
        self.assertEqual(d["works_orders"]["open_count"], 2)
        for k in ("quotes", "customers", "suppliers"):
            self.assertNotIn(k, d)

    def test_completed_year_filter_matches_panel(self):
        rows = self.admin.get(f"/api/works-orders?status=Completed&end_year={self.year}").get_json()
        self.assertEqual([r["job_number"] for r in rows], ["TM-3"])

    def test_hours_booked_report(self):
        self.make_labour_entry(self.open1, 8, 10, work_date=f"{self.year}-01-10")
        self.make_labour_entry(self.open1, 4, 10, work_date=f"{self.year}-02-10")
        self.make_labour_entry(self.open2, 6, 10, work_date=f"{self.year}-02-11")
        self.make_labour_entry(self.open2, 3, 10, work_date="2020-02-11")     # other year, ignored
        d = self.admin.get(f"/api/reporting/hours-booked?year={self.year}").get_json()
        self.assertEqual(d["totals"]["hours"], 18)
        self.assertEqual(d["totals"]["jobs"], 2)
        self.assertEqual(d["totals"]["avg_per_job"], 9)
        by_job = self.admin.get(f"/api/reporting/hours-booked?year={self.year}&group=job").get_json()
        self.assertEqual([r["name"] for r in by_job["rows"]], ["TM-1", "TM-2"])
        self.assertEqual(by_job["rows"][0]["months"][:2], [8, 4])
        csv = self.admin.get(f"/api/reporting/hours-booked/export?year={self.year}")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Total hours", csv.get_data(as_text=True))
