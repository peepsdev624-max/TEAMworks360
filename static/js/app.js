/* TEAMworks360 (formerly Job Profitability Manager) - front-end application (vanilla JS SPA) */

const state = {
  user: null,
  ref: null,       // reference data: customers, suppliers, works_orders, purchase_orders
  charts: {},       // active Chart.js instances, keyed by canvas id
};

const NAV = [
  { id: "dashboard", label: "Dashboard", icon: "&#9673;" },
  { id: "quotes", label: "Quotes", icon: "&#128221;" },
  { id: "works-orders", label: "Works Orders", icon: "&#128193;" },
  { id: "purchase-orders", label: "Purchase Orders", icon: "&#128230;" },
  { id: "goods-in", label: "Goods In", icon: "&#128229;" },
  { id: "despatch-notes", label: "Despatch Notes", icon: "&#128667;" },
  { id: "inventory", label: "Inventory", icon: "&#128452;" },
  { id: "labour-entries", label: "Hours Quoted & Booked", icon: "&#9201;" },
  { id: "supplier-invoices", label: "Supplier Invoices", icon: "&#128209;" },
  { id: "customer-invoices", label: "Customer Invoices", icon: "&#128179;" },
  { id: "customers", label: "Customers", icon: "&#127970;" },
  { id: "suppliers", label: "Suppliers", icon: "&#128666;" },
  { id: "reporting", label: "Reporting", icon: "&#128200;" },
  { id: "users", label: "Users", icon: "&#128100;", adminOnly: true },
  { id: "settings", label: "Settings", icon: "&#9881;", adminOnly: true },
];

const SERVICE_ENGINEER_PAGES = ["works-orders", "purchase-orders", "goods-in", "despatch-notes", "inventory", "labour-entries"];

const VIEW_TITLES = Object.fromEntries(NAV.map(n => [n.id, n.label]));

/* ---------------------------------------------------------------------- */
/* API helper                                                             */
/* ---------------------------------------------------------------------- */
// Reads the CSRF token the server mirrors into a plain (non-HttpOnly)
// cookie on every response once a session exists (see set_security_headers
// in app.py) - this is how a request below can echo it back as a header
// without ever needing a dedicated endpoint just to fetch it.
function getCsrfToken() {
  const m = document.cookie.match(/(?:^|; )csrf_token=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : null;
}

async function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  // A FormData body (file uploads - see Goods In's document attachment)
  // must NOT get a JSON Content-Type or be JSON.stringify'd: the browser
  // sets its own multipart/form-data header (with the boundary it needs)
  // as soon as fetch sees a FormData body, and only if we leave it alone.
  const isFormData = typeof FormData !== "undefined" && opts.body instanceof FormData;
  const headers = isFormData ? {} : { "Content-Type": "application/json" };
  // Only state-changing requests need the CSRF header - the server only
  // checks it on POST/PUT/DELETE/PATCH (see enforce_csrf in app.py).
  if (method !== "GET" && method !== "HEAD") {
    const csrf = getCsrfToken();
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const res = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: opts.body === undefined ? undefined : (isFormData ? opts.body : JSON.stringify(opts.body)),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* no body */ }
  // A 401 from the login endpoint itself means wrong credentials, not an
  // expired session - let its own specific error ("Invalid username or
  // password", or the lockout message) through below instead of
  // overriding it with the generic session-expired redirect, which is
  // actively misleading here (a brand new user has no session to expire).
  if (res.status === 401 && path !== "/api/auth/login") {
    showLogin();
    throw new Error("Session expired - please sign in again");
  }
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

/* ---------------------------------------------------------------------- */
/* Formatting helpers                                                     */
/* ---------------------------------------------------------------------- */
function fmtMoney(n) {
  n = Number(n || 0);
  return "£" + n.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(n) { return Number(n || 0).toFixed(1) + "%"; }
function fmtDate(d) {
  if (!d) return "";
  const iso = d.substring(0, 10);
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : iso;
}
function fmtDateInput(d) { return d ? d.substring(0, 10) : ""; }  // <input type="date"> always needs ISO YYYY-MM-DD, regardless of display format
function fmtDateTime(d) {  // date + time, for timestamps that carry both (e.g. login_events.event_date)
  if (!d) return "";
  const t = d.match(/\d{2}:\d{2}/);
  return t ? `${fmtDate(d)} ${t[0]}` : fmtDate(d);
}
function fmtFileSize(bytes) {  // Goods In document attachments - a plain human size, not raw byte counts
  const n = Number(bytes);
  if (!n || n <= 0) return "";
  if (n < 1024) return "<1 KB";
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}
// Small "Created by X on Y" hint shown on a record's Edit form - never
// on New (nothing to show yet). Kept as one shared helper across every
// form that has these fields, rather than repeating the same three-line
// template seven times, so the wording only ever needs updating once.
function auditHint(record) {
  if (!record || !record.created_by) return "";
  let text = `Created by ${esc(record.created_by)}`;
  if (record.created_date) text += ` on ${fmtDate(record.created_date)}`;
  if (record.updated_by && record.updated_date) {
    text += ` · Last edited by ${esc(record.updated_by)} on ${fmtDate(record.updated_date)}`;
  }
  return `<div class="hint" style="margin-top:12px;">${text}</div>`;
}
// Change History - a read-only audit trail of edits to the financially
// significant fields (status, value/cost/rate fields) on the record types
// that drive profitability and invoicing. Backed by /api/audit-log/<table>
// /<id>, which only ever has rows where one of those fields was actually
// changed after the record was created - see log_field_changes() in app.py.
async function showAuditHistory(table, id, label) {
  let rows = [];
  try { rows = await api(`/api/audit-log/${table}/${id}`); } catch (e) { /* show as empty below */ }
  const body = rows.length ? `
    <table class="data-table">
      <thead><tr><th>Field</th><th>From</th><th>To</th><th>Changed By</th><th>Date</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${esc(r.field_name)}</td>
          <td>${esc(r.old_value ?? "—")}</td>
          <td>${esc(r.new_value ?? "—")}</td>
          <td>${esc(r.changed_by || "—")}</td>
          <td>${fmtDate(r.changed_date)}</td>
        </tr>`).join("")}</tbody>
    </table>` : `<div class="hint">No changes to status or value/cost fields have been recorded yet for ${esc(label)}.</div>`;
  openModal(`Change History — ${esc(label)}`, body, { showFooter: false });
}

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtMonthLabel(ym) {
  const m = String(ym || "").match(/^(\d{4})-(\d{2})$/);
  return m ? `${MONTH_NAMES[Number(m[2]) - 1]} ${m[1]}` : (ym || "");
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function lightBadge(light, text) {
  return `<span class="badge ${light}">${esc(text)}</span>`;
}
function statusBadge(status) {
  const map = {
    Draft: "grey", Open: "blue", "In Progress": "amber", Completed: "green", Closed: "grey", Cancelled: "red",
    Submitted: "blue", Approved: "green", Issued: "blue", Pending: "amber", Paid: "green", Disputed: "red",
    Sent: "blue", "On Hold": "amber", Won: "green", Lost: "red", Expired: "grey", Overdue: "red", Full: "green", Partial: "amber",
  };
  return `<span class="badge ${map[status] || "grey"}">${esc(status)}</span>`;
}
function sharepointLink(url) {
  if (!url) return "";
  return ` <a class="sp-link" href="${esc(url)}" target="_blank" rel="noopener" title="Open in SharePoint">&#128279;</a>`;
}
function marginBadge(pct, target = 15) {
  if (pct < 0) return lightBadge("red", fmtPct(pct));
  if (pct < target) return lightBadge("amber", fmtPct(pct));
  return lightBadge("green", fmtPct(pct));
}

/* ---------------------------------------------------------------------- */
/* Permissions (mirrors server-side ROLE_PERMISSIONS)                     */
/* ---------------------------------------------------------------------- */
const ROLE_WRITE = {
  "Project Manager": ["works_orders", "labour_entries", "quotes", "despatch_notes"],
  "Procurement": ["purchase_orders", "inventory"],
  "Finance": ["supplier_invoices", "customer_invoices"],
  "Executive": [],
  "Service Engineer": ["works_orders", "purchase_orders", "goods_in", "despatch_notes", "inventory", "labour_entries"],
};
function canWrite(domain) {
  if (!state.user) return false;
  if (state.user.role === "Administrator") return true;
  return (ROLE_WRITE[state.user.role] || []).includes(domain);
}
function canSeeJobMoney() { return !(state.user && state.user.role === "Service Engineer"); }
function isAdmin() { return state.user && state.user.role === "Administrator"; }

/* ---------------------------------------------------------------------- */
/* Auth / bootstrap                                                       */
/* ---------------------------------------------------------------------- */
// Every full-page auth screen this app can show other than login-screen
// (which toggles via inline style.display rather than a class, same as
// it always has) - kept as one list so showLogin/showForcePasswordScreen/
// showForce2faSetupScreen/showForce2faVerifyScreen/showApp can each just
// hide every other one rather than every screen needing to know about
// every other screen.
const _VISIBLE_CLASS_SCREEN_IDS = ["force-password-screen", "force-2fa-setup-screen", "force-2fa-verify-screen"];
function _hideAllAuthScreens() {
  document.getElementById("login-screen").style.display = "none";
  _VISIBLE_CLASS_SCREEN_IDS.forEach(id => document.getElementById(id).classList.remove("visible"));
  document.getElementById("app").classList.remove("visible");
}
function showLogin() {
  _hideAllAuthScreens();
  document.getElementById("login-screen").style.display = "flex";
}
function showForcePasswordScreen() {
  _hideAllAuthScreens();
  document.getElementById("force-password-screen").classList.add("visible");
}
function showForce2faSetupScreen() {
  _hideAllAuthScreens();
  document.getElementById("force-2fa-setup-screen").classList.add("visible");
  renderTfaSetupCard();
}
function showForce2faVerifyScreen() {
  _hideAllAuthScreens();
  document.getElementById("force-2fa-verify-screen").classList.add("visible");
  resetTfaVerifyForm();
}
function showApp() {
  _hideAllAuthScreens();
  document.getElementById("app").classList.add("visible");
}

// Looks at state.user's auth flags in the order the server enforces them
// (see _check_auth_gates() in app.py: password change, then 2FA setup,
// then 2FA verification) and shows whichever screen is still outstanding -
// or, once none are, proceeds into the app. Called after every step that
// can clear one of these flags (login, force-password submit, 2FA setup
// confirm, 2FA verify) instead of each of those jumping straight to
// afterLogin() itself, so a login that needs two steps (e.g. a brand new
// account: change password, then set up 2FA) walks through both in order.
async function proceedAfterAuth() {
  if (state.user.must_change_password) { showForcePasswordScreen(); return; }
  if (state.user.must_setup_2fa) { showForce2faSetupScreen(); return; }
  if (state.user.must_verify_2fa) { showForce2faVerifyScreen(); return; }
  await afterLogin();
}

function hideBootSplash() {
  const el = document.getElementById("boot-splash");
  if (!el) return;
  el.classList.add("hidden");
  setTimeout(() => el.remove(), 400); // matches the CSS fade duration
}

async function bootstrap() {
  try {
    const me = await fetch("/api/auth/me", { credentials: "same-origin" });
    if (me.ok) {
      state.user = await me.json();
      await proceedAfterAuth();
      return;
    }
  } catch (e) { /* fall through to login */ }
  showLogin();
}

async function showAppVersion() {
  const el = document.getElementById("app-version");
  if (!el) return;
  try {
    const v = await api("/api/version");
    el.innerHTML = `Version ${esc(v.version)}` +
      (v.update_available ? `<div class="app-version-update" title="${esc(v.notes || "")}">Update available: ${esc(v.latest_version)}</div>` : "");
    el.title = `Built ${v.build_date}`;
  } catch (e) { el.textContent = ""; }
}

async function afterLogin() {
  showApp();
  document.getElementById("user-name").textContent = state.user.full_name;
  document.getElementById("user-role").textContent = state.user.role;
  renderNav();
  showAppVersion();
  state.ref = await api("/api/reference");
  // Fetched once here so a budget overrun or a stale quote shows up the
  // moment someone signs in - not only once they happen to open the
  // Dashboard. renderDashboard() refreshes it again (with its own, already
  // in-hand data) whenever that page is actually visited.
  if (state.user.role === "Service Engineer") {
    state.alerts = [];
    document.getElementById("topbar-right").innerHTML = "";
  } else {
    try {
      const alertsResp = await api("/api/alerts");
      state.alerts = alertsResp.alerts;
    } catch (e) { state.alerts = []; }
    renderTopbarAlerts();
  }
  if (!location.hash) location.hash = "#/" + visibleNav()[0].id;
  route();
}

/* ---------------------------------------------------------------------- */
/* Topbar alerts - persistent across every page, not just the Dashboard   */
/* ---------------------------------------------------------------------- */
function renderTopbarAlerts() {
  const right = document.getElementById("topbar-right");
  if (!right) return;
  const count = (state.alerts || []).length;
  right.innerHTML = count > 0
    ? `<button class="btn small secondary" id="alerts-bell">&#9888; ${count} Alert${count === 1 ? "" : "s"}</button>`
    : `<button class="btn small secondary" id="alerts-bell" style="opacity:0.6;">&#9888; No Alerts</button>`;
  document.getElementById("alerts-bell").addEventListener("click", showAlertsModal);
}

/* Alert rows: job number is a link to the job, plus Accept (with a reason). */
function alertRowHtml(a, i) {
  const canAccept = canWrite("works_orders");
  return `
    <div class="alert-row"><span class="dot ${a.level}"></span>
      <div style="flex:1;"><a href="#" class="alert-job-link" data-alert-open="${i}" title="Open this job"><b>${esc(a.job_number)}</b></a> — ${esc(a.type)}: ${esc(a.message)}</div>
      ${canAccept ? `<button class="btn small secondary" data-alert-accept="${i}">Accept</button>` : ""}
    </div>`;
}

async function refreshAlerts() {
  try { state.alerts = (await api("/api/alerts")).alerts; } catch (e) { state.alerts = []; }
  renderTopbarAlerts();
}

function wireAlertRows(container, alerts, rerender) {
  container.querySelectorAll("[data-alert-open]").forEach(el => el.addEventListener("click", (e) => {
    e.preventDefault();
    const a = alerts[Number(el.dataset.alertOpen)];
    closeModal();
    if (a.works_order_id) {
      // Land on the Works Orders list narrowed to this job, with its profitability open.
      const alreadyThere = location.hash === "#/works-orders";
      goToJob(a.job_number);
      if (alreadyThere) route();
      viewWorksOrder(a.works_order_id);
    } else if (a.quote_id) {
      const alreadyThere = location.hash === "#/quotes";
      quoteState.search = a.job_number;
      location.hash = "#/quotes";
      if (alreadyThere) route();
    }
  }));
  container.querySelectorAll("[data-alert-accept]").forEach(el => el.addEventListener("click", () => {
    const a = alerts[Number(el.dataset.alertAccept)];
    openModal("Accept alert — " + a.job_number, `
      <div class="hint">${esc(a.type)}: ${esc(a.message)}</div>
      <div class="form-row" style="margin-top:10px;"><label>Why is this fine as it is?</label>
        <textarea name="reason" rows="3" required placeholder="e.g. Customer agreed extra work; variation order to follow"></textarea></div>
      <div class="hint">Your name and the date are recorded. If this job's figures change, the alert comes back on its own.</div>`, {
      submitLabel: "Accept alert",
      onSubmit: async (fd) => {
        await api("/api/alerts/accept", { method: "POST", body: { alert_key: a.alert_key, reason: fd.get("reason") } });
        closeModal();
        await refreshAlerts();
        rerender();
      },
    });
  }));
}

function showAcceptedAlerts() {
  api("/api/alerts/accepted").then(rows => {
    const body = rows.length ? rows.map(r => `
      <div class="alert-row"><span class="dot green"></span>
        <div style="flex:1;"><b>${esc(r.job_number)}</b> — ${esc(r.alert_type)}: ${esc(r.message)}<div class="hint">Accepted by ${esc(r.accepted_by || "?")} on ${fmtDate(r.accepted_date)} — “${esc(r.reason)}”</div></div>
        ${canWrite("works_orders") ? `<button class="btn small secondary" data-undo="${r.acceptance_id}">Undo</button>` : ""}
      </div>`).join("") : `<div class="hint">No alerts have been accepted.</div>`;
    openModal("Accepted alerts", body, { showFooter: false });
    document.querySelectorAll("[data-undo]").forEach(b => b.addEventListener("click", async () => {
      await api("/api/alerts/accepted/" + b.dataset.undo, { method: "DELETE" });
      await refreshAlerts();
      closeModal();
      route();
    }));
  });
}

function showAlertsModal() {
  const alerts = state.alerts || [];
  const body = (alerts.length ? alerts.slice(0, 100).map((a, i) => alertRowHtml(a, i)).join("") : `<div class="hint">No active alerts. All jobs are within budget and margin thresholds, and no open quotes have run past their validity date.</div>`)
    + (alerts.length > 100 ? `<div class="hint" style="margin-top:8px;">Showing the first 100 of ${alerts.length} — see the Dashboard for the rest.</div>` : "");
  openModal(`Alerts (${alerts.length})`, body + `<div style="margin-top:10px;"><a href="#" id="alerts-show-accepted">View accepted alerts</a></div>`, { showFooter: false });
  const modal = document.getElementById("modal-root");
  wireAlertRows(modal, alerts.slice(0, 100), showAlertsModal);
  document.getElementById("alerts-show-accepted").addEventListener("click", (e) => { e.preventDefault(); showAcceptedAlerts(); });
}

document.getElementById("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = document.getElementById("login-username").value.trim();
  const password = document.getElementById("login-password").value;
  const errEl = document.getElementById("login-error");
  errEl.textContent = "";
  try {
    state.user = await api("/api/auth/login", { method: "POST", body: { username, password } });
    await proceedAfterAuth();
  } catch (err) {
    errEl.textContent = err.message;
  }
});

document.getElementById("force-password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const current_password = document.getElementById("fp-current").value;
  const new_password = document.getElementById("fp-new").value;
  const confirm_password = document.getElementById("fp-confirm").value;
  const errEl = document.getElementById("fp-error");
  errEl.textContent = "";
  if (new_password !== confirm_password) {
    errEl.textContent = "New password and confirmation don't match.";
    return;
  }
  try {
    await api("/api/auth/change-password", { method: "POST", body: { current_password, new_password } });
    state.user.must_change_password = false;
    document.getElementById("force-password-form").reset();
    await proceedAfterAuth();
  } catch (err) {
    errEl.textContent = err.message;
  }
});

/* ---------------------------------------------------------------------- */
/* Two-factor authentication (TOTP) - setup, confirm, verify               */
/* ---------------------------------------------------------------------- */
// Setup offers both a QR code to scan and the manual entry key to type -
// see totp.py's own module docstring for why manual entry is the primary,
// always-available path (no extra dependency for the text key, and it
// works identically with Microsoft Authenticator, Google Authenticator,
// Authy and others). The QR code is a same-origin image
// (/api/auth/2fa/setup/qr.png, rendered server-side) for the apps that can
// add an account by scanning instead.
async function renderTfaSetupCard() {
  const card = document.getElementById("tfa-setup-card");
  card.innerHTML = `<p class="hint">Loading your setup key…</p>`;
  let setup;
  try {
    setup = await api("/api/auth/2fa/setup");
  } catch (err) {
    card.innerHTML = `<h1>Two-Factor Authentication</h1><div class="login-error">${esc(err.message)}</div>`;
    return;
  }
  card.innerHTML = `
    <h1>Set Up Two-Factor Authentication</h1>
    <p class="sub">Every TEAMworks360 account needs this. Open an authenticator app on your phone -
      Microsoft Authenticator, Google Authenticator, Authy, or similar - choose "Add account", then
      either scan the QR code below or choose "Enter a setup key" (or "Enter code manually") and
      type in the key instead.</p>
    <div class="tfa-qr-box">
      <img src="/api/auth/2fa/setup/qr.png" alt="QR code for two-factor authentication setup" width="180" height="180">
    </div>
    <label>Setup Key</label>
    <div class="tfa-key-box" id="tfa-key-box">${esc(setup.manual_entry_key)}</div>
    <div class="tfa-key-meta">Account name: <b>TEAMworks360</b> &middot; Type: <b>Time based</b></div>
    <form id="tfa-confirm-form">
      <label>Enter the 6-digit code your app now shows, to confirm it's working</label>
      <input type="text" id="tfa-confirm-code" inputmode="numeric" autocomplete="one-time-code" required>
      <button type="submit">Confirm &amp; Enable Two-Factor Authentication</button>
      <div class="login-error" id="tfa-confirm-error"></div>
    </form>
  `;
  document.getElementById("tfa-confirm-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = document.getElementById("tfa-confirm-code").value.trim();
    const errEl = document.getElementById("tfa-confirm-error");
    errEl.textContent = "";
    try {
      const result = await api("/api/auth/2fa/setup", { method: "POST", body: { code } });
      state.user.must_setup_2fa = false;
      state.user.must_verify_2fa = false;
      renderBackupCodesStep(result.backup_codes);
    } catch (err) {
      errEl.textContent = err.message;
    }
  });
}

function renderBackupCodesStep(codes) {
  const card = document.getElementById("tfa-setup-card");
  card.innerHTML = `
    <h1>Save Your Backup Codes</h1>
    <p class="sub">If you ever lose access to your authenticator app, use one of these one-time codes to sign in
      instead. Each works once. <b>This is the only time they'll be shown</b> - write them down or store them
      somewhere safe (e.g. a password manager) before continuing. An Administrator can reset your two-factor
      authentication if you lose these too.</p>
    <div class="tfa-backup-codes">${codes.map(c => `<span>${esc(c)}</span>`).join("")}</div>
    <button type="button" class="btn" id="tfa-backup-continue" style="width:100%; margin-top:18px;">I've Saved These - Continue</button>
  `;
  document.getElementById("tfa-backup-continue").addEventListener("click", () => { proceedAfterAuth(); });
}

// The regular-login path once 2FA is already set up: a 6-digit code from
// the authenticator app, or (toggled below) a one-time backup code for
// when that app isn't available at all.
let _tfaUsingBackupCode = false;

function resetTfaVerifyForm() {
  _tfaUsingBackupCode = false;
  document.getElementById("tfa-verify-form").reset();
  document.getElementById("tfa-verify-error").textContent = "";
  _applyTfaVerifyMode();
}

function _applyTfaVerifyMode() {
  document.getElementById("tfa-verify-sub").textContent = _tfaUsingBackupCode
    ? "Enter one of the one-time backup codes you saved when you set up two-factor authentication."
    : "Enter the 6-digit code from your authenticator app.";
  document.getElementById("tfa-verify-label").textContent = _tfaUsingBackupCode ? "Backup Code" : "Authentication Code";
  document.getElementById("tfa-toggle-backup").textContent = _tfaUsingBackupCode
    ? "Use my authenticator app instead" : "Use a backup code instead";
}

document.getElementById("tfa-toggle-backup").addEventListener("click", (e) => {
  e.preventDefault();
  _tfaUsingBackupCode = !_tfaUsingBackupCode;
  document.getElementById("tfa-verify-code").value = "";
  document.getElementById("tfa-verify-error").textContent = "";
  _applyTfaVerifyMode();
});

document.getElementById("tfa-verify-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = document.getElementById("tfa-verify-code").value.trim();
  const errEl = document.getElementById("tfa-verify-error");
  errEl.textContent = "";
  try {
    await api("/api/auth/2fa/verify", { method: "POST", body: { code, is_backup_code: _tfaUsingBackupCode } });
    state.user.must_verify_2fa = false;
    await proceedAfterAuth();
  } catch (err) {
    errEl.textContent = err.message;
  }
});

document.getElementById("change-password-btn").addEventListener("click", () => {
  const body = `
    <div class="form-row"><label>Current Password</label><input type="password" name="current_password" autocomplete="current-password" required></div>
    <div class="form-row"><label>New Password</label><input type="password" name="new_password" autocomplete="new-password" required minlength="8"></div>
    <div class="form-row"><label>Confirm New Password</label><input type="password" name="confirm_password" autocomplete="new-password" required minlength="8"></div>
  `;
  openModal("Change Password", body, {
    submitLabel: "Change Password",
    onSubmit: async (fd) => {
      const current_password = fd.get("current_password");
      const new_password = fd.get("new_password");
      if (new_password !== fd.get("confirm_password")) throw new Error("New password and confirmation don't match.");
      await api("/api/auth/change-password", { method: "POST", body: { current_password, new_password } });
      closeModal();
    },
  });
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await api("/api/auth/logout", { method: "POST" });
  state.user = null;
  state.alerts = [];
  document.getElementById("topbar-right").innerHTML = "";
  location.hash = "";
  showLogin();
});

/* ---------------------------------------------------------------------- */
/* Navigation / routing                                                   */
/* ---------------------------------------------------------------------- */
function visibleNav() {
  if (state.user.role === "Service Engineer") return NAV.filter(n => SERVICE_ENGINEER_PAGES.includes(n.id));
  if (state.user.role === "Executive") return NAV.filter(n => ["dashboard", "reporting"].includes(n.id));
  return NAV.filter(n => !n.adminOnly || isAdmin());
}

function renderNav() {
  const nav = visibleNav();
  document.getElementById("nav-items").innerHTML = nav.map(n =>
    `<div class="nav-item" data-route="${n.id}"><span class="nav-icon">${n.icon}</span>${esc(n.label)}</div>`
  ).join("");
  document.querySelectorAll(".nav-item").forEach(el => {
    el.addEventListener("click", () => { location.hash = "#/" + el.dataset.route; });
  });
}

const ROUTES = {
  "dashboard": renderDashboard,
  "quotes": renderQuotes,
  "works-orders": renderWorksOrders,
  "purchase-orders": renderPurchaseOrders,
  "goods-in": renderGoodsIn,
  "despatch-notes": renderDespatchNotes,
  "inventory": renderInventory,
  "labour-entries": renderLabourEntries,
  "supplier-invoices": renderSupplierInvoices,
  "customer-invoices": renderCustomerInvoices,
  "customers": renderCustomers,
  "suppliers": renderSuppliers,
  "reporting": renderReporting,
  "users": renderUsers,
  "settings": renderSettings,
};

function route() {
  const hash = (location.hash || "#/" + visibleNav()[0].id).replace("#/", "");
  const routeId = hash.split("/")[0] || "dashboard";
  const allowed = visibleNav().map(n => n.id);
  const target = allowed.includes(routeId) ? routeId : allowed[0];
  document.querySelectorAll(".nav-item").forEach(el => el.classList.toggle("active", el.dataset.route === target));
  document.getElementById("view-title").textContent = VIEW_TITLES[target] || "Dashboard";
  const fn = ROUTES[target];
  if (fn) fn();
}
window.addEventListener("hashchange", route);

/* ---------------------------------------------------------------------- */
/* Modal helper                                                           */
/* ---------------------------------------------------------------------- */
function openModal(title, bodyHtml, { onSubmit, submitLabel = "Save", showFooter = true } = {}) {
  const root = document.getElementById("modal-root");
  root.innerHTML = `
    <div class="modal-overlay" id="modal-overlay">
      <div class="modal">
        <h3>${esc(title)}</h3>
        <form id="modal-form">
          ${bodyHtml}
          <div class="form-error" id="modal-error"></div>
          ${showFooter ? `
          <div class="modal-actions">
            <button type="button" class="btn secondary" id="modal-cancel">Cancel</button>
            <button type="submit" class="btn" id="modal-submit">${esc(submitLabel)}</button>
          </div>` : ""}
        </form>
      </div>
    </div>`;
  document.getElementById("modal-cancel")?.addEventListener("click", closeModal);
  document.getElementById("modal-overlay").addEventListener("click", (e) => { if (e.target.id === "modal-overlay") closeModal(); });
  const form = document.getElementById("modal-form");
  if (onSubmit) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const errEl = document.getElementById("modal-error");
      errEl.textContent = "";
      const btn = document.getElementById("modal-submit");
      btn.disabled = true;
      try {
        await onSubmit(new FormData(form));
      } catch (err) {
        errEl.textContent = err.message;
      } finally {
        btn.disabled = false;
      }
    });
  }
  return form;
}
function closeModal() { document.getElementById("modal-root").innerHTML = ""; }

/* ---------------------------------------------------------------------- */
/* Generic sortable/filterable table state helper                        */
/* ---------------------------------------------------------------------- */
function makeSorter(rows, key, numeric = false) {
  return [...rows].sort((a, b) => {
    const av = a[key], bv = b[key];
    if (numeric) return (Number(av) || 0) - (Number(bv) || 0);
    return String(av ?? "").localeCompare(String(bv ?? ""));
  });
}

/* ======================================================================
   DASHBOARD
   ====================================================================== */
async function renderDashboard() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading dashboard…</div>`;
  const [d, marginSettings] = await Promise.all([
    api("/api/dashboard"),
    api("/api/settings/margin-targets"),
  ]);
  const defaultTarget = marginSettings.default_target ?? 15;
  // The Dashboard always has the freshest alert data (it just recomputed
  // it) - feed it back into the shared topbar badge so it doesn't need a
  // separate fetch of its own.
  state.alerts = d.alerts;
  renderTopbarAlerts();

  const kpi = (label, value, cls = "", id = null) => {
    const tag = id ? "button" : "div";
    return `<${tag} class="kpi-card ${cls}${id ? " clickable" : ""}"${id ? ` id="${id}" type="button"` : ""}><div class="label">${label}</div><div class="value">${value}</div></${tag}>`;
  };

  root.innerHTML = `
    <div class="kpi-grid">
      ${kpi("Total Revenue", fmtMoney(d.total_revenue))}
      ${kpi("Total Costs", fmtMoney(d.total_cost))}
      ${kpi("Total Profit", fmtMoney(d.total_profit), d.total_profit >= 0 ? "green" : "red")}
      ${kpi("Average Margin", fmtPct(d.avg_margin), d.avg_margin >= defaultTarget ? "green" : "amber")}
      ${kpi("Active Jobs", d.active_jobs, "", d.active_jobs > 0 ? "dash-kpi-active-jobs" : null)}
      ${kpi("Loss-Making Jobs", d.loss_making_jobs, d.loss_making_jobs > 0 ? "red" : "green")}
    </div>
    <h3 style="margin-top:6px;font-size:13px;color:var(--navy);">Works Orders &amp; Stock</h3>
    <div class="kpi-grid" style="grid-template-columns:repeat(5,1fr);">
      ${kpi("Live Works Orders", d.live_works_orders, "", d.live_works_orders > 0 ? "dash-kpi-live-wo" : null)}
      ${kpi("Due — " + d.due_month_labels[0], d.due_this_month, "", d.due_this_month > 0 ? "dash-kpi-due-0" : null)}
      ${kpi("Due — " + d.due_month_labels[1], d.due_next_month, "", d.due_next_month > 0 ? "dash-kpi-due-1" : null)}
      ${kpi("Due — " + d.due_month_labels[2], d.due_month_after, "", d.due_month_after > 0 ? "dash-kpi-due-2" : null)}
      ${kpi("Total Stock Held", fmtMoney(d.total_stock_value), "", "dash-kpi-stock")}
    </div>
    ${lateWorksOrdersPanelHtml(d)}
    <div class="chart-grid">
      <div class="panel">
        <h3>Top 15 Jobs by Profit &amp; Loss-Making Jobs — ${d.current_year} to date</h3>
        <canvas id="chart-profit"></canvas>
      </div>
      <div class="panel">
        <h3>Profit vs Margin % — ${d.current_year} to date</h3>
        <div class="chart-legend">
          <span><span class="dot green"></span>At/above margin target</span>
          <span><span class="dot amber"></span>Below margin target</span>
          <span><span class="dot red"></span>Loss-making</span>
        </div>
        <canvas id="chart-scatter"></canvas>
      </div>
    </div>
    <div class="chart-grid">
      <div class="panel wide">
        <h3>Monthly Revenue, Cost &amp; Profit — ${d.current_year} to date</h3>
        <canvas id="chart-monthly"></canvas>
      </div>
    </div>
    <div class="panel alerts-panel">
      <h3>Alerts (${d.alerts.length})</h3>
      ${d.alerts.length ? d.alerts.slice(0, 100).map((a, i) => alertRowHtml(a, i)).join("") : `<div class="hint">No active alerts. All jobs are within budget and margin thresholds.</div>`}
      ${d.alerts.length > 100 ? `<div class="hint">Showing the first 100 of ${d.alerts.length} alerts — see the Reporting page to find the rest.</div>` : ""}
      <div style="margin-top:10px;"><a href="#" id="dash-show-accepted">View accepted alerts</a></div>
    </div>
  `;

  const goToActiveWorksOrders = () => {
    woState.late = false;
    woState.endYear = "";
    woState.status = "Active";
    woState.search = "";
    woState.page = 1;
    location.hash = "#/works-orders";
  };
  document.getElementById("dash-kpi-active-jobs")?.addEventListener("click", goToActiveWorksOrders);
  document.getElementById("dash-kpi-live-wo")?.addEventListener("click", goToActiveWorksOrders);
  const goToDueMonth = (index) => {
    woState.late = false;
    woState.endYear = "";
    woState.status = "Active";
    woState.search = "";
    woState.dueMonth = d.due_month_keys[index];
    woState.dueMonthLabel = d.due_month_labels[index];
    woState.page = 1;
    location.hash = "#/works-orders";
  };
  document.getElementById("dash-kpi-due-0")?.addEventListener("click", () => goToDueMonth(0));
  document.getElementById("dash-kpi-due-1")?.addEventListener("click", () => goToDueMonth(1));
  document.getElementById("dash-kpi-due-2")?.addEventListener("click", () => goToDueMonth(2));
  const goToLate = (jobNumber) => {
    woState.late = true;
    woState.endYear = "";
    woState.status = "";
    woState.dueMonth = "";
    woState.dueMonthLabel = "";
    woState.search = jobNumber || "";
    woState.page = 1;
    location.hash = "#/works-orders";
  };
  document.getElementById("dash-late-count")?.addEventListener("click", () => goToLate());
  document.getElementById("dash-late-all")?.addEventListener("click", e => { e.preventDefault(); goToLate(); });
  root.querySelectorAll("a.dash-late-job").forEach(a => a.addEventListener("click", e => { e.preventDefault(); goToLate(a.dataset.job); }));
  document.getElementById("dash-kpi-stock")?.addEventListener("click", () => { location.hash = "#/inventory"; });
  wireAlertRows(root.querySelector(".alerts-panel"), d.alerts.slice(0, 100), renderDashboard);
  document.getElementById("dash-show-accepted")?.addEventListener("click", (e) => { e.preventDefault(); showAcceptedAlerts(); });

  // Top 15 (green, left) followed by every job actually in loss this year
  // (red, right) - not a plain "top/bottom 15 by profit" split, since with
  // 1,700+ jobs a year most of the low end is just small jobs making a
  // trivial but still-positive profit, not jobs that need attention. See
  // the backend's job_profit_margin_for_year()/dashboard() for how these
  // two lists are built.
  const profitJobs = [...d.top_profit_jobs, ...d.bottom_profit_jobs];
  drawChart("chart-profit", "bar", profitJobs.map(j => j.job_number),
    [{ label: "Profit (£)", data: profitJobs.map(j => j.profit),
       backgroundColor: profitJobs.map(j => j.profit >= 0 ? "#15803d" : "#b91c1c") }],
    { onLabelClick: i => goToJob(profitJobs[i].job_number) });

  drawScatterChart("chart-scatter", d.job_scatter);

  drawChart("chart-monthly", "line", d.monthly_trend.map(r => fmtMonthLabel(r.month)), [
    { label: "Revenue (£)", data: d.monthly_trend.map(r => r.revenue), borderColor: "#2563eb", backgroundColor: "rgba(37,99,235,.12)", tension: .3 },
    { label: "Cost (£)", data: d.monthly_trend.map(r => r.cost), borderColor: "#b45309", backgroundColor: "rgba(180,83,9,.12)", tension: .3 },
    { label: "Profit (£)", data: d.monthly_trend.map(r => r.profit), borderColor: "#15803d", backgroundColor: "rgba(21,128,61,.12)", tension: .3 },
  ]);
}

// "Late Works Orders" dashboard panel: live jobs past their End Date, with the
// customer PO value and what has been spent so far (purchases, hours, stock).
// Amber up to 30 days late, red beyond that.
function lateWorksOrdersPanelHtml(d) {
  if (d.late_po_value === undefined) return "";   // money not sent to this role
  const n = d.late_count;
  const card = (label, value, extra = "") => `<div class="kpi-card ${extra}"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const pill = days => `<span class="late-pill ${days > 30 ? "late-red" : "late-amber"}">${days}</span>`;
  return `
    <div class="panel late-panel">
      <div class="late-head"><h3>Late Works Orders</h3><span class="hint">Open or In Progress, End Date before today</span></div>
      ${n === 0 ? `<div class="hint">No late works orders.</div>` : `
      <div class="kpi-grid late-kpis">
        <button type="button" id="dash-late-count" class="kpi-card clickable red"><div class="label">Late works orders</div><div class="value">${n.toLocaleString()}</div></button>
        ${card("Customer PO value", fmtMoney(d.late_po_value))}
        ${card("Cost to date", fmtMoney(d.late_cost_total))}
        ${card("Average days late", d.late_avg_days)}
      </div>
      <div class="hint" style="margin-bottom:8px;">Cost to date = purchases ${fmtMoney(d.late_purchases)} + hours ${fmtMoney(d.late_hours)} + stock booked ${fmtMoney(d.late_stock)}</div>
      <div class="table-wrap"><table class="data-table late-table">
        <thead><tr><th>Job</th><th>Customer</th><th>Owner</th><th class="num">Days late</th><th class="num">PO value</th><th class="num">Purchases</th><th class="num">Hours</th><th class="num">Stock</th><th class="num">Cost to date</th></tr></thead>
        <tbody>${d.late_jobs.map(j => `<tr>
          <td><a href="#" class="dash-late-job" data-job="${esc(j.job_number)}">${esc(j.job_number)}</a></td>
          <td>${esc(j.customer_name)}</td><td>${esc(j.project_manager || "")}</td>
          <td class="num">${pill(j.days_late)}</td>
          <td class="num">${fmtMoney(j.po_value)}</td><td class="num">${fmtMoney(j.purchases)}</td>
          <td class="num">${fmtMoney(j.hours)}</td><td class="num">${fmtMoney(j.stock)}</td><td class="num">${fmtMoney(j.cost)}</td></tr>`).join("")}</tbody>
      </table></div>
      <div style="margin-top:8px;"><a href="#" id="dash-late-all">View all ${n.toLocaleString()} late works orders${n > d.late_jobs.length ? ` (list above shows the ${d.late_jobs.length} latest)` : ""}</a></div>`}
    </div>`;
}

// Jumps to the Works Orders list filtered to one job, same drill-through
// pattern the KPI cards above already use (goToActiveWorksOrders etc.) -
// clicking a chart bar/point takes you straight to the job behind it.
function goToJob(jobNumber) {
  woState.late = false;
  woState.endYear = "";
  woState.status = "";
  woState.dueMonth = "";
  woState.search = jobNumber;
  woState.page = 1;
  location.hash = "#/works-orders";
}

function drawChart(canvasId, type, labels, datasets, { onLabelClick } = {}) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (state.charts[canvasId]) state.charts[canvasId].destroy();
  state.charts[canvasId] = new Chart(el.getContext("2d"), {
    type, data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: datasets.length > 1 } },
      scales: { y: { beginAtZero: true } },
      onClick: onLabelClick ? (evt, elements) => { if (elements.length) onLabelClick(elements[0].index); } : undefined,
      onHover: onLabelClick ? (evt, elements) => { evt.native.target.style.cursor = elements.length ? "pointer" : "default"; } : undefined,
    },
  });
  el.parentElement.style.height = "260px";
}

// Profit vs Margin %, one bubble per job active this year, sized by Revenue
// (area-proportional, not radius-proportional, so a bubble twice the
// revenue reads as twice the area rather than four times it) and coloured
// by the same red/amber/green status used everywhere else in the app
// (status_light from job_profit_margin_for_year, mirroring the Works
// Orders/Alerts convention). Replaces the old "Margin % by Job" bar chart,
// which plotted margin in isolation - a tiny job at 100% margin and a
// six-figure job at 20% margin looked identical on a bar chart; here the
// bubble size makes that difference visible instead of hiding it.
function drawScatterChart(canvasId, jobs) {
  const el = document.getElementById(canvasId);
  if (!el) return;
  if (state.charts[canvasId]) state.charts[canvasId].destroy();

  const maxRevenue = Math.max(1, ...jobs.map(j => j.revenue));
  const MIN_R = 4, MAX_R = 20;
  const colorFor = { green: "#15803d", amber: "#b45309", red: "#b91c1c" };
  const points = jobs.map(j => ({
    x: j.profit, y: j.margin_pct,
    r: MIN_R + (MAX_R - MIN_R) * Math.sqrt(j.revenue / maxRevenue),
    job_number: j.job_number, job_name: j.job_name, revenue: j.revenue, status_light: j.status_light,
  }));

  state.charts[canvasId] = new Chart(el.getContext("2d"), {
    type: "bubble",
    data: { datasets: [{
      data: points,
      backgroundColor: points.map(p => colorFor[p.status_light] + "b3"), // ~70% opacity, dense overlap stays readable
      borderColor: points.map(p => colorFor[p.status_light]),
      borderWidth: 1,
    }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {
        legend: { display: false }, // status colours are explained by the HTML legend above the canvas
        tooltip: {
          callbacks: {
            label: (ctx) => {
              const p = ctx.raw;
              return [`${p.job_number} — ${p.job_name}`, `Profit: ${fmtMoney(p.profit)} · Margin: ${fmtPct(p.margin_pct)} · Revenue: ${fmtMoney(p.revenue)}`];
            },
          },
        },
      },
      scales: {
        x: { title: { display: true, text: "Profit (£)" } },
        y: { title: { display: true, text: "Margin %" } },
      },
      onClick: (evt, elements) => { if (elements.length) goToJob(points[elements[0].index].job_number); },
      onHover: (evt, elements) => { evt.native.target.style.cursor = elements.length ? "pointer" : "default"; },
    },
  });
  el.parentElement.style.height = "260px";
}

/* ======================================================================
   WORKS ORDERS
   ====================================================================== */
let woState = { search: "", status: "", dueMonth: "", dueMonthLabel: "", late: false, endYear: "", contractCode: "", location: "", customer: "", owner: "", sortKey: "job_number", sortDir: -1, page: 1 };
const PAGE_SIZE = 50;

async function renderWorksOrders() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading works orders…</div>`;
  const params = new URLSearchParams();
  if (woState.search) params.set("q", woState.search);
  if (woState.status) params.set("status", woState.status);
  if (woState.dueMonth) params.set("due_month", woState.dueMonth);
  if (woState.late) params.set("late", "1");
  if (woState.endYear) params.set("end_year", woState.endYear);
  if (woState.contractCode) params.set("contract_code", woState.contractCode);
  if (woState.location) params.set("location", woState.location);
  let allRows = await api("/api/works-orders?" + params.toString());
  const sm = await loadScreenSummary();
  // Filter option lists come from the full reference dataset (every works
  // order, not just this page's filtered results), so picking one filter
  // never shrinks what the other dropdown offers.
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();
  const locationOptions = state.ref.locations || [];
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const ownerOptions = [...new Set((state.ref.works_orders || []).map(w => w.project_manager).filter(Boolean))].sort();
  // Customer and Owner are filtered client-side, on top of the server-side
  // status/contract-code/location/search filters above - the full
  // works_orders reference list doesn't carry live metrics, so these two
  // couldn't be pushed down to the same SQL query without duplicating the
  // profitability calculation there too.
  if (woState.customer) allRows = allRows.filter(r => r.customer_name === woState.customer);
  if (woState.owner) allRows = allRows.filter(r => (r.project_manager || "") === woState.owner);
  allRows.sort((a, b) => {
    const av = a[woState.sortKey], bv = b[woState.sortKey];
    const cmp = typeof av === "number" ? av - bv : String(av ?? "").localeCompare(String(bv ?? ""), undefined, { numeric: true });
    return cmp * woState.sortDir;
  });

  const totalPages = Math.max(1, Math.ceil(allRows.length / PAGE_SIZE));
  woState.page = Math.min(Math.max(1, woState.page), totalPages);
  const rows = allRows.slice((woState.page - 1) * PAGE_SIZE, woState.page * PAGE_SIZE);

  const statusOptions = ["Draft", "Open", "In Progress", "Completed", "Closed", "Cancelled"];

  root.innerHTML = worksOrdersSummaryHtml(sm) + `
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="wo-search" placeholder="Search job number, name, customer, owner…" value="${esc(woState.search)}">
        <select id="wo-status-filter">
          <option value="">All statuses</option>
          <option value="Active" ${woState.status === "Active" ? "selected" : ""}>Active (Open + In Progress)</option>
          ${statusOptions.map(s => `<option value="${s}" ${woState.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <select id="wo-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${woState.contractCode === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
        <select id="wo-location-filter">
          <option value="">All locations</option>
          ${locationOptions.map(loc => `<option value="${esc(loc)}" ${woState.location === loc ? "selected" : ""}>${esc(loc)}</option>`).join("")}
        </select>
        <select id="wo-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${woState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="wo-owner-filter">
          <option value="">All owners</option>
          ${ownerOptions.map(o => `<option value="${esc(o)}" ${woState.owner === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("works_orders") ? `<button class="btn" id="wo-new">+ New Works Order</button>` : ""}
    </div>
    <div class="hint">${allRows.length.toLocaleString()} works order${allRows.length === 1 ? "" : "s"}${woState.dueMonth ? ` · due in ${esc(woState.dueMonthLabel)} <a href="#" id="wo-clear-due-month">clear</a>` : ""}${woState.late ? ` · late (End Date passed) <a href="#" id="wo-clear-late">clear</a>` : ""}${woState.endYear ? ` · End Date in ${esc(woState.endYear)} <a href="#" id="wo-clear-endyear">clear</a>` : ""}${woState.contractCode ? ` · ${esc(contractCodeInfo(woState.contractCode).display)}` : ""}${woState.location ? ` · ${esc(woState.location)}` : ""}${woState.customer ? ` · ${esc(woState.customer)}` : ""}${woState.owner ? ` · ${esc(woState.owner)}` : ""}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>
        <th data-key="job_number">Job Number</th>
        <th data-key="job_name">Job Name</th>
        <th data-key="customer_name">Customer</th>
        <th data-key="contract_code">Code</th>
        <th data-key="project_manager">Owner</th>
        ${canSeeJobMoney() ? `
        <th data-key="contract_value" class="num">Purchase Order Value</th>
        <th data-key="actual_cost" class="num">Actual Cost</th>
        <th data-key="forecast_cost" class="num">Quoted</th>
        <th data-key="profit" class="num">Profit</th>
        <th data-key="margin_pct" class="num">Margin %</th>` : ""}
        <th data-key="status">Status</th>
        <th></th>
      </tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.job_number)}</b>${sharepointLink(r.sharepoint_url)}</td>
            <td>${esc(r.job_name)}</td>
            <td>${esc(r.customer_name)}</td>
            <td>${r.contract_code ? esc(contractCodeInfo(r.contract_code).display) : "—"}</td>
            <td>${esc(r.project_manager || "—")}</td>
            ${canSeeJobMoney() ? `
            <td class="num">${fmtMoney(r.contract_value)}</td>
            <td class="num">${fmtMoney(r.actual_cost)}</td>
            <td class="num">${fmtMoney(r.forecast_cost)}</td>
            <td class="num">${lightBadge(r.status_light, fmtMoney(r.profit))}</td>
            <td class="num">${fmtPct(r.margin_pct)}</td>` : ""}
            <td>${statusBadge(r.status)}</td>
            <td class="actions-cell">
              ${canSeeJobMoney() ? `<button class="btn small secondary" data-view="${r.works_order_id}">View</button>` : ""}
              <button class="btn small secondary" data-history="${r.works_order_id}">History</button>
              ${canWrite("works_orders") ? `<button class="btn small secondary" data-edit="${r.works_order_id}">Edit</button>` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.works_order_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="${canSeeJobMoney() ? 12 : 7}"><div class="empty-state">No works orders found.</div></td></tr>`}
      </tbody>
    </table></div>
    ${renderPager(woState.page, totalPages, "wo-page")}
  `;

  document.getElementById("wo-search").addEventListener("input", debounce(e => { woState.search = e.target.value; woState.page = 1; debouncedSearchRerender(e.target, renderWorksOrders); }, 500));
  document.getElementById("wo-status-filter").addEventListener("change", e => { woState.status = e.target.value; woState.page = 1; renderWorksOrders(); });
  document.getElementById("wo-code-filter").addEventListener("change", e => { woState.contractCode = e.target.value; woState.page = 1; renderWorksOrders(); });
  document.getElementById("wo-location-filter").addEventListener("change", e => { woState.location = e.target.value; woState.page = 1; renderWorksOrders(); });
  document.getElementById("wo-customer-filter").addEventListener("change", e => { woState.customer = e.target.value; woState.page = 1; renderWorksOrders(); });
  document.getElementById("wo-owner-filter").addEventListener("change", e => { woState.owner = e.target.value; woState.page = 1; renderWorksOrders(); });
  document.getElementById("wo-clear-endyear")?.addEventListener("click", e => {
    e.preventDefault();
    woState.endYear = "";
    woState.status = "";
    woState.page = 1;
    renderWorksOrders();
  });
  document.getElementById("wo-clear-late")?.addEventListener("click", e => {
    e.preventDefault();
    woState.late = false;
    woState.status = "";
    woState.page = 1;
    renderWorksOrders();
  });
  document.getElementById("wo-clear-due-month")?.addEventListener("click", e => {
    e.preventDefault();
    woState.dueMonth = "";
    woState.dueMonthLabel = "";
    woState.page = 1;
    renderWorksOrders();
  });
  document.getElementById("wo-new")?.addEventListener("click", () => openWorksOrderForm());
  wirePager("wo-page", woState.page, totalPages, p => { woState.page = p; renderWorksOrders(); });
  root.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.key;
    woState.sortDir = woState.sortKey === key ? -woState.sortDir : 1;
    woState.sortKey = key;
    woState.page = 1;
    renderWorksOrders();
  }));
  root.querySelectorAll("[data-view]").forEach(b => b.addEventListener("click", () => viewWorksOrder(b.dataset.view)));
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const wo = allRows.find(r => String(r.works_order_id) === b.dataset.edit);
    openWorksOrderForm(wo);
  }));
  root.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    const wo = allRows.find(r => String(r.works_order_id) === b.dataset.history);
    showAuditHistory("works_orders", wo.works_order_id, wo.job_number);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this works order and all linked POs, invoices and booked hours?", "/api/works-orders/" + b.dataset.del, renderWorksOrders)
  ));
}

function renderPager(page, totalPages, idPrefix) {
  if (totalPages <= 1) return "";
  return `
    <div class="toolbar" style="justify-content:center;gap:14px;margin-top:14px;">
      <button class="btn secondary small" id="${idPrefix}-prev" ${page <= 1 ? "disabled" : ""}>&larr; Prev</button>
      <span class="hint" style="margin:0;">Page ${page} of ${totalPages}</span>
      <button class="btn secondary small" id="${idPrefix}-next" ${page >= totalPages ? "disabled" : ""}>Next &rarr;</button>
    </div>`;
}
function wirePager(idPrefix, page, totalPages, onChange) {
  document.getElementById(`${idPrefix}-prev`)?.addEventListener("click", () => onChange(Math.max(1, page - 1)));
  document.getElementById(`${idPrefix}-next`)?.addEventListener("click", () => onChange(Math.min(totalPages, page + 1)));
}

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// A search-triggered re-render replaces the whole view, including the
// search input itself - meaning the browser treats it as a brand new
// element and drops focus, forcing a click back into the box to keep
// typing. This restores focus and cursor position on the new element
// once the render (and its data fetch) has actually finished.
async function debouncedSearchRerender(inputEl, renderFn) {
  const cursorPos = inputEl.selectionStart;
  const inputId = inputEl.id;
  await renderFn();
  const newInput = document.getElementById(inputId);
  if (newInput) {
    newInput.focus();
    newInput.setSelectionRange(cursorPos, cursorPos);
  }
}

// Many list pages (Purchase Orders, Goods In, Despatch Notes, Supplier
// Invoices, Customer Invoices, Hours Quoted & Booked) only carry a job
// number, not a customer or contract code of their own - both live on the
// linked Works Order. This looks that up from the full works_orders
// reference list (state.ref.works_orders), so every page can offer a
// Customer / Contract Code filter purely client-side, with no backend
// changes or extra joins needed. Built fresh each call rather than cached,
// since state.ref can be reloaded (e.g. after adding a Works Order).
function woRefForJobNumber(jobNumber) {
  return (state.ref.works_orders || []).find(w => w.job_number === jobNumber)
    || (state.ref.placeholder_jobs || []).find(w => w.job_number === jobNumber);
}
function customerNameForJobNumber(jobNumber) {
  const wo = woRefForJobNumber(jobNumber);
  const cust = wo && (state.ref.customers || []).find(c => c.customer_id === wo.customer_id);
  return cust?.customer_name || "";
}

async function handleDelete(confirmMsg, url, refreshFn) {
  if (!confirm(confirmMsg)) return;
  try {
    await api(url, { method: "DELETE" });
    refreshFn();
  } catch (err) {
    alert(err.message);
  }
}

async function openWorksOrderForm(wo = null) {
  const customers = state.ref.customers;
  let nextJobNumber = wo ? wo.job_number : "…";
  if (!wo) {
    try { nextJobNumber = (await api("/api/works-orders/next-number")).job_number; } catch (e) { nextJobNumber = "TM-????"; }
  }
  const body = `
    <div class="form-grid-2">
      <div class="form-row"><label>Job Number</label><input value="${esc(nextJobNumber)}" disabled>
        ${!wo ? `<div class="hint" style="margin:6px 0 0;">Assigned automatically when saved — the next available number.</div>` : ""}</div>
      <div class="form-row"><label>Status</label>
        <select name="status">${["Draft", "Open", "In Progress", "Completed", "Closed", "Cancelled"].map(s => `<option ${wo?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    <div class="form-row"><label>Job Name</label><input name="job_name" value="${esc(wo?.job_name || "")}" required></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Customer</label>
        <select name="customer_id" required>${customers.map(c => `<option value="${c.customer_id}" ${wo?.customer_id === c.customer_id ? "selected" : ""}>${esc(c.customer_name)}</option>`).join("")}</select>
      </div>
      <div class="form-row"><label>Project Manager</label><input name="project_manager" value="${esc(wo?.project_manager || "")}"></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Start Date</label><input type="date" name="start_date" value="${fmtDateInput(wo?.start_date)}"></div>
      <div class="form-row"><label>End Date</label><input type="date" name="end_date" value="${fmtDateInput(wo?.end_date)}"></div>
    </div>
    <div class="form-row"><label>Contract Code</label>
      <select name="contract_code" id="wo-contract-code">
        <option value="">—</option>
        ${Object.values(CONTRACT_CODE_INFO).map(c => `<option value="${esc(c.display)}" ${wo?.contract_code === c.display ? "selected" : ""}>${esc(c.display)}${c.label ? ` — ${esc(c.label)}` : ""}</option>`).join("")}
        ${wo?.contract_code && !Object.values(CONTRACT_CODE_INFO).some(c => c.display === wo.contract_code) ? `<option value="${esc(wo.contract_code)}" selected>${esc(wo.contract_code)}</option>` : ""}
      </select>
      ${canSeeJobMoney() ? `<div class="hint" style="margin-top:6px;">Determines which margin target this job is measured against — see Settings → Per-contract-code margin targets. Picking a fixed-price code fills in Purchase Order Value and Budget Cost below automatically.</div>` : ""}
      <div class="hint" id="wo-recipe-hint" style="margin-top:6px;display:none;"></div>
    </div>
    ${canSeeJobMoney() ? `
    <div class="form-grid-2">
      <div class="form-row"><label>Purchase Order Value (£)</label><input type="number" step="0.01" name="contract_value" id="wo-contract-value" value="${wo?.contract_value ?? ""}" required></div>
      <div class="form-row"><label>Budget Cost (£)</label><input type="number" step="0.01" name="budget_cost" id="wo-budget-cost" value="${wo?.budget_cost ?? ""}" required>
        ${wo?.budget_is_estimated ? `<div class="hint" style="margin-top:6px;color:var(--amber);">This budget was estimated at import (35% of invoice value) — no confirmed figure exists for this job. Saving this form will mark it as confirmed.</div>` : ""}
      </div>
    </div>
    ` : ""}
    <div class="form-grid-2">
      <div class="form-row"><label>Location</label>
        <input name="location" list="wo-location-list" value="${esc(wo?.location || "")}" placeholder="e.g. Wolverhampton">
        <datalist id="wo-location-list">${(state.ref.locations || []).map(l => `<option value="${esc(l)}">`).join("")}</datalist>
      </div>
      <div class="form-row"><label>SharePoint Folder URL</label><input type="url" name="sharepoint_url" placeholder="https://…" value="${esc(wo?.sharepoint_url || "")}"></div>
    </div>
    ${auditHint(wo)}
  `;
  openModal(wo ? "Edit Works Order" : "New Works Order", body, {
    submitLabel: wo ? "Save Changes" : "Create Works Order",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      payload.customer_id = Number(payload.customer_id);
      if (wo) await api("/api/works-orders/" + wo.works_order_id, { method: "PUT", body: payload });
      else await api("/api/works-orders", { method: "POST", body: payload });
      state.ref = await api("/api/reference");
      closeModal();
      renderWorksOrders();
    },
  });
  document.getElementById("wo-contract-code").addEventListener("change", (e) => {
    const target = (state.ref.contract_code_targets || []).find(t => t.contract_code === e.target.value.toUpperCase());
    if (target && target.standard_value != null && target.standard_cost != null && document.getElementById("wo-contract-value")) {
      document.getElementById("wo-contract-value").value = target.standard_value;
      document.getElementById("wo-budget-cost").value = target.standard_cost;
    }
    updateRecipeHint(e.target.value);
  });
  updateRecipeHint(wo?.contract_code);
}
function updateRecipeHint(code) {
  const el = document.getElementById("wo-recipe-hint");
  const recipe = code && FS_TIER_RECIPES[code];
  if (recipe) {
    el.textContent = "Standard recipe for " + code + ": " + recipe;
    el.style.display = "";
  } else {
    el.style.display = "none";
  }
}

async function viewWorksOrder(id) {
  const wo = await api("/api/works-orders/" + id);
  const noActivity = wo.revenue === 0 && wo.actual_cost === 0;
  const alertsHtml = wo.alerts.length ? wo.alerts.map(a => `<div class="alert-row"><span class="dot ${a.level}"></span><div>${esc(a.type)}: ${esc(a.message)}</div></div>`).join("") : `<div class="hint">No alerts for this job.</div>`;
  const costParts = [];
  if (wo.labour_total > 0) costParts.push(`Labour: ${fmtMoney(wo.labour_total)}`);
  if (wo.supplier_invoice_total > 0) costParts.push(`Supplier Invoices: ${fmtMoney(wo.supplier_invoice_total)}`);
  if (wo.stock_cost > 0) costParts.push(`Stock Allocated: ${fmtMoney(wo.stock_cost)}`);
  const body = `
    <div class="hint">${esc(wo.job_number)} — ${esc(wo.job_name)} (${esc(wo.customer_name)})${sharepointLink(wo.sharepoint_url)}</div>
    ${noActivity ? `<div class="readonly-note" style="margin-top:8px;">No Booked Hours, Purchase Orders, Supplier Invoices, Customer Invoices, or Stock Allocations have been recorded against this job yet — that's why everything below shows £0.00. Add some from their own tabs and these figures will update.</div>` : ""}
    <div class="form-grid-2">
      <div class="kpi-card"><div class="label">Revenue</div><div class="value">${fmtMoney(wo.revenue)}</div></div>
      <div class="kpi-card"><div class="label">Actual Cost</div><div class="value">${fmtMoney(wo.actual_cost)}</div></div>
      <div class="kpi-card"><div class="label">Total Purchases</div><div class="value">${fmtMoney(wo.committed_cost)}</div></div>
      <div class="kpi-card"><div class="label">Quoted</div><div class="value">${fmtMoney(wo.forecast_cost)}</div></div>
      <div class="kpi-card ${wo.profit >= 0 ? 'green' : 'red'}"><div class="label">Profit</div><div class="value">${fmtMoney(wo.profit)}</div></div>
      <div class="kpi-card"><div class="label">Forecast Profit</div><div class="value">${fmtMoney(wo.forecast_profit)}</div></div>
      <div class="kpi-card ${wo.margin_pct < wo.margin_target ? 'amber' : 'green'}"><div class="label">Margin % (target ${fmtPct(wo.margin_target)})</div><div class="value">${fmtPct(wo.margin_pct)}</div></div>
      <div class="kpi-card ${wo.forecast_margin_pct < wo.margin_target ? 'amber' : 'green'}"><div class="label">Forecast Margin %</div><div class="value">${fmtPct(wo.forecast_margin_pct)}</div></div>
    </div>
    ${costParts.length ? `<div class="hint" style="margin-top:8px;">Actual Cost breakdown — ${costParts.join(" · ")}</div>` : ""}
    <h3 style="margin-top:16px;font-size:13.5px;color:var(--navy)">Alerts</h3>
    ${alertsHtml}
  `;
  openModal("Job Profitability — " + wo.job_number, body, { showFooter: false });
}

/* ======================================================================
   PURCHASE ORDERS
   ====================================================================== */
let poState = { search: "", workOrder: "", supplier: "", raisedBy: "", status: "", customer: "", contractCode: "", history: "live" };

async function renderPurchaseOrders() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading purchase orders…</div>`;
  const allRows = await api("/api/purchase-orders");
  const sm = await loadScreenSummary();

  let rows = allRows;
  if (poState.search) {
    const q = poState.search.toLowerCase();
    rows = rows.filter(r =>
      r.po_number.toLowerCase().includes(q) ||
      (r.job_number || "").toLowerCase().includes(q) ||
      r.supplier_name.toLowerCase().includes(q) ||
      (r.description || "").toLowerCase().includes(q)
    );
  }
  if (poState.workOrder) rows = rows.filter(r => r.job_number === poState.workOrder);
  if (poState.supplier) rows = rows.filter(r => r.supplier_name === poState.supplier);
  if (poState.raisedBy === "__other__") rows = rows.filter(r => r.raised_by && !(state.ref.staff_names || []).includes(r.raised_by));
  else if (poState.raisedBy) rows = rows.filter(r => (r.raised_by || "") === poState.raisedBy);
  if (poState.status) rows = rows.filter(r => r.status === poState.status);
  if (poState.history === "live") rows = rows.filter(r => !r.is_historic);
  if (poState.history === "historic") rows = rows.filter(r => r.is_historic);
  if (poState.customer) rows = rows.filter(r => customerNameForJobNumber(r.job_number) === poState.customer);
  if (poState.contractCode) rows = rows.filter(r => (woRefForJobNumber(r.job_number)?.contract_code || "") === poState.contractCode);

  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const woOptions = (state.ref.works_orders || []).map(w => w.job_number).sort();
  const supplierOptions = (state.ref.suppliers || []).map(s => s.supplier_name).sort();
  // Only real people in the list; anything else (initials, combined entries, notes) sits under "Other".
  const staffNames = new Set(state.ref.staff_names || []);
  const usedNames = [...new Set(allRows.map(r => r.raised_by).filter(Boolean))];
  const raisedByOptions = usedNames.filter(n => staffNames.has(n)).sort();
  const hasOtherRaisedBy = usedNames.some(n => !staffNames.has(n));
  state.poRaisedByNames = raisedByOptions;
  const statusOptions = ["Draft", "Submitted", "Approved", "Issued", "Completed", "Cancelled"];
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();

  root.innerHTML = purchaseOrdersSummaryHtml(sm) + `
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="po-search" placeholder="Search PO number, job, supplier, description…" value="${esc(poState.search)}">
        <select id="po-wo-filter">
          <option value="">All works orders</option>
          ${woOptions.map(j => `<option value="${esc(j)}" ${poState.workOrder === j ? "selected" : ""}>${esc(j)}</option>`).join("")}
        </select>
        <select id="po-supplier-filter">
          <option value="">All suppliers</option>
          ${supplierOptions.map(s => `<option value="${esc(s)}" ${poState.supplier === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
        </select>
        <select id="po-raisedby-filter">
          <option value="">All raised by</option>
          ${raisedByOptions.map(n => `<option value="${esc(n)}" ${poState.raisedBy === n ? "selected" : ""}>${esc(n)}</option>`).join("")}
          ${hasOtherRaisedBy ? `<option value="__other__" ${poState.raisedBy === "__other__" ? "selected" : ""}>Other (not a listed person)</option>` : ""}
        </select>
        <select id="po-history-filter" title="Historic POs were imported from the old PO register for reference and are not counted in job costs">
          <option value="">Live + historic POs</option>
          <option value="live" ${poState.history === "live" ? "selected" : ""}>Live POs only</option>
          <option value="historic" ${poState.history === "historic" ? "selected" : ""}>Historic POs only</option>
        </select>
        <select id="po-status-filter">
          <option value="">All statuses</option>
          ${statusOptions.map(s => `<option value="${s}" ${poState.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <select id="po-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${poState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="po-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${poState.contractCode === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("purchase_orders") ? `<button class="btn" id="po-new">+ New Purchase Order</button>` : ""}
    </div>
    <div class="hint">${rows.length.toLocaleString()} purchase order${rows.length === 1 ? "" : "s"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>PO Number</th><th>Works Order</th><th>Supplier</th><th>Ship To</th><th>Description</th><th>Order Date</th><th class="num">PO Value</th><th class="num">Invoiced</th><th>Status</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.po_number)}</b>${sharepointLink(r.sharepoint_url)}</td>
            <td>${r.job_number ? `${esc(r.job_number)} — ${esc(r.job_name)}` : `<span class="muted">—</span>`}</td>
            <td>${esc(r.supplier_name)}</td>
            <td>${esc(r.ship_to_label || "—")}</td>
            <td>${esc(r.description || "")}</td>
            <td>${fmtDate(r.order_date)}</td>
            <td class="num">${fmtMoney(r.po_value)}</td>
            <td class="num">${r.invoiced_total > r.po_value ? lightBadge("red", fmtMoney(r.invoiced_total)) : fmtMoney(r.invoiced_total)}</td>
            <td>${statusBadge(r.status)}${r.is_historic ? ` <span class="badge grey" title="Imported from the old PO register - for reference only, not counted in job costs">Historic</span>` : ""}</td>
            <td class="actions-cell">
              <a class="btn small secondary" href="/api/purchase-orders/${r.purchase_order_id}/pdf" title="Download PO PDF">PDF</a>
              <button class="btn small secondary" data-history="${r.purchase_order_id}">History</button>
              ${canWrite("purchase_orders") ? `
                <button class="btn small secondary" data-edit="${r.purchase_order_id}">Edit</button>
                ${r.status !== "Approved" ? `<button class="btn small" data-approve="${r.purchase_order_id}">Approve</button>` : ""}
              ` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.purchase_order_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="10"><div class="empty-state">No purchase orders found.</div></td></tr>`}
      </tbody>
    </table></div>
  `;

  document.getElementById("po-search").addEventListener("input", debounce(e => { poState.search = e.target.value; debouncedSearchRerender(e.target, renderPurchaseOrders); }, 500));
  document.getElementById("po-wo-filter").addEventListener("change", e => { poState.workOrder = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-supplier-filter").addEventListener("change", e => { poState.supplier = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-raisedby-filter").addEventListener("change", e => { poState.raisedBy = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-history-filter").addEventListener("change", e => { poState.history = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-status-filter").addEventListener("change", e => { poState.status = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-customer-filter").addEventListener("change", e => { poState.customer = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-code-filter").addEventListener("change", e => { poState.contractCode = e.target.value; renderPurchaseOrders(); });
  document.getElementById("po-new")?.addEventListener("click", () => openPurchaseOrderForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const po = rows.find(r => String(r.purchase_order_id) === b.dataset.edit);
    openPurchaseOrderForm(po);
  }));
  root.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    const po = rows.find(r => String(r.purchase_order_id) === b.dataset.history);
    showAuditHistory("purchase_orders", po.purchase_order_id, po.po_number);
  }));
  root.querySelectorAll("[data-approve]").forEach(b => b.addEventListener("click", async () => {
    await api("/api/purchase-orders/" + b.dataset.approve + "/approve", { method: "POST" });
    renderPurchaseOrders();
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this purchase order?", "/api/purchase-orders/" + b.dataset.del, renderPurchaseOrders)
  ));
}

let poFormLines = [];

function poLineTotal(line) { return (Number(line.qty) || 0) * (Number(line.unit_price) || 0); }
function poLinesSubtotal() { return poFormLines.reduce((s, l) => s + poLineTotal(l), 0); }

async function openPurchaseOrderForm(po = null) {
  const wos = state.ref.works_orders, suppliers = state.ref.suppliers, shipToAddresses = state.ref.ship_to_addresses || [];
  poFormLines = (po?.lines || []).map(l => ({ ...l }));
  let nextPoNumber = po ? po.po_number : "…";
  if (!po) {
    try { nextPoNumber = (await api("/api/purchase-orders/next-number")).po_number; } catch (e) { nextPoNumber = "PO-????"; }
  }
  const body = `
    <div class="form-grid-2">
      <div class="form-row"><label>PO Number</label><input value="${esc(nextPoNumber)}" disabled>
        ${!po ? `<div class="hint" style="margin:6px 0 0;">Assigned automatically when saved — the next available number.</div>` : ""}</div>
      <div class="form-row"><label>Status</label>
        <select name="status">${["Draft", "Submitted", "Approved", "Issued", "Completed", "Cancelled"].map(s => `<option ${po?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    <div class="form-row"><label>Works Order</label>
      <select name="works_order_id" ${po?.is_historic ? "" : "required"}>${po?.is_historic ? `<option value="">— none (history only) —</option>` : ""}${wos.map(w => `<option value="${w.works_order_id}" ${po?.works_order_id === w.works_order_id ? "selected" : ""}>${esc(w.job_number)} — ${esc(w.job_name)}</option>`).join("")}</select>
    </div>
    <div class="form-row"><label>Supplier</label>
      <select name="supplier_id" required>${suppliers.map(s => `<option value="${s.supplier_id}" ${po?.supplier_id === s.supplier_id ? "selected" : ""}>${esc(s.supplier_name)}</option>`).join("")}</select>
    </div>
    <div class="form-row"><label>Ship To</label>
      <select name="ship_to_id">
        <option value="">—</option>
        ${shipToAddresses.map(a => `<option value="${a.ship_to_id}" ${po?.ship_to_id === a.ship_to_id ? "selected" : ""}>${esc(a.label)}</option>`).join("")}
      </select>
      ${shipToAddresses.length ? "" : `<div class="hint" style="margin-top:6px;">No delivery addresses set up yet — add one from Settings.</div>`}
    </div>
    <div class="form-row"><label>Description</label><input name="description" value="${esc(po?.description || "")}"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Order Date</label><input type="date" name="order_date" value="${fmtDateInput(po?.order_date)}"></div>
      <div class="form-row"><label>Date Required</label><input type="date" name="date_required" value="${fmtDateInput(po?.date_required)}"></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Account No.</label><input name="account_number" value="${esc(po?.account_number || "")}"></div>
      <div class="form-row"><label>Raised By</label><input name="raised_by" list="po-raisedby-names" value="${esc(po?.raised_by ?? (state.user?.full_name || ""))}">
        <datalist id="po-raisedby-names">${[...new Set((state.poRaisedByNames || []))].map(n => `<option value="${esc(n)}">`).join("")}</datalist></div>
    </div>
    <div class="form-row"><label>SharePoint Folder URL</label><input type="url" name="sharepoint_url" placeholder="https://…" value="${esc(po?.sharepoint_url || "")}"></div>

    <h3 style="margin-top:16px;font-size:13px;color:var(--navy);">Items</h3>
    <div class="hint">Add each item on this PO — Qty, Part No., Item #, Description, Unit Price. PO Value totals automatically from these once any are added; leave empty and enter PO Value directly below for a simple, non-itemised PO.</div>
    <div id="po-lines-rows" style="margin-top:8px;"></div>
    <button class="btn secondary small" id="po-add-line" type="button" style="margin-top:6px;">+ Add Line</button>
    <div class="wizard-totals" id="po-lines-totals" style="margin-top:10px;"></div>

    <div class="form-grid-2" style="margin-top:12px;">
      <div class="form-row" id="po-value-row">
        <label>PO Value (£)${poFormLines.length ? " (from items, above)" : ""}</label>
        <input type="number" step="0.01" name="po_value" id="po-value-input" value="${po?.po_value ?? ""}" ${poFormLines.length ? "readonly" : ""} required>
      </div>
      <div class="form-row"><label>VAT Rate (%)</label><input type="number" step="0.1" name="vat_rate" value="${po?.vat_rate ?? 20}"></div>
    </div>
  `;
  openModal(po ? "Edit Purchase Order" : "New Purchase Order", body, {
    submitLabel: po ? "Save Changes" : "Create Purchase Order",
    onSubmit: async (fd) => {
      if (!confirm("Have all items been added to this purchase order?")) return;
      const payload = Object.fromEntries(fd.entries());
      payload.works_order_id = payload.works_order_id ? Number(payload.works_order_id) : null;
      payload.supplier_id = Number(payload.supplier_id);
      payload.ship_to_id = payload.ship_to_id ? Number(payload.ship_to_id) : null;
      payload.lines = poFormLines.filter(l => (Number(l.qty) || 0) > 0 || l.description || l.part_number);
      if (po) await api("/api/purchase-orders/" + po.purchase_order_id, { method: "PUT", body: payload });
      else await api("/api/purchase-orders", { method: "POST", body: payload });
      state.ref = await api("/api/reference");
      closeModal();
      renderPurchaseOrders();
    },
  });
  renderPoLinesUI();
}

function renderPoLinesUI() {
  const rowsEl = document.getElementById("po-lines-rows");
  if (!rowsEl) return;
  rowsEl.innerHTML = poFormLines.length ? `
    <div class="purchase-line-row" style="grid-template-columns:.7fr 1fr 1fr 1.6fr .9fr auto;margin-bottom:2px;">
      <span class="wizard-step-label" style="margin:0;">Qty</span>
      <span class="wizard-step-label" style="margin:0;">Part No.</span>
      <span class="wizard-step-label" style="margin:0;">Item #</span>
      <span class="wizard-step-label" style="margin:0;">Description</span>
      <span class="wizard-step-label" style="margin:0;">Unit Price £</span>
      <span></span>
    </div>` : "";
  rowsEl.innerHTML += poFormLines.map((l, idx) => `
    <div class="purchase-line-row" data-row="${idx}" style="grid-template-columns:.7fr 1fr 1fr 1.6fr .9fr auto;">
      <input type="number" step="1" min="0" placeholder="Qty" data-field="qty" value="${l.qty ?? ""}">
      <input placeholder="Part No." data-field="part_number" value="${esc(l.part_number || "")}">
      <input placeholder="Item #" data-field="item_number" value="${esc(l.item_number || "")}">
      <input placeholder="Description" data-field="description" value="${esc(l.description || "")}">
      <input type="number" step="0.01" placeholder="Unit Price" data-field="unit_price" value="${l.unit_price ?? ""}">
      <button class="btn small danger" type="button" data-remove-line="${idx}">✕</button>
    </div>`).join("") || (poFormLines.length ? "" : `<div class="hint">No items added — enter PO Value directly below for a simple PO.</div>`);

  const subtotal = poLinesSubtotal();
  document.getElementById("po-lines-totals").innerHTML = poFormLines.length
    ? `<span>Items Subtotal: <b>${fmtMoney(subtotal)}</b></span>` : "";

  const valueInput = document.getElementById("po-value-input");
  const valueLabel = document.querySelector('#po-value-row label');
  if (poFormLines.length) {
    valueInput.value = subtotal.toFixed(2);
    valueInput.readOnly = true;
    valueLabel.textContent = "PO Value (£) (from items, above)";
  } else {
    valueInput.readOnly = false;
    valueLabel.textContent = "PO Value (£)";
  }

  rowsEl.querySelectorAll(".purchase-line-row input").forEach(inp => inp.addEventListener("input", () => {
    const idx = Number(inp.closest(".purchase-line-row").dataset.row);
    poFormLines[idx][inp.dataset.field] = inp.value;
    const subtotal2 = poLinesSubtotal();
    document.getElementById("po-lines-totals").innerHTML = `<span>Items Subtotal: <b>${fmtMoney(subtotal2)}</b></span>`;
    document.getElementById("po-value-input").value = subtotal2.toFixed(2);
  }));
  rowsEl.querySelectorAll("[data-remove-line]").forEach(b => b.addEventListener("click", () => {
    poFormLines.splice(Number(b.dataset.removeLine), 1);
    renderPoLinesUI();
  }));
  document.getElementById("po-add-line").addEventListener("click", () => {
    poFormLines.push({ qty: "", part_number: "", item_number: "", description: "", unit_price: "" });
    renderPoLinesUI();
  });
}

/* ======================================================================
   GOODS IN
   ====================================================================== */
let giState = { search: "", workOrder: "", supplier: "", receivedBy: "", customer: "" };

async function renderGoodsIn() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading goods in…</div>`;
  const allRows = await api("/api/goods-in");
  const sm = await loadScreenSummary();

  let rows = allRows;
  if (giState.search) {
    const q = giState.search.toLowerCase();
    rows = rows.filter(r =>
      r.po_number.toLowerCase().includes(q) ||
      r.job_number.toLowerCase().includes(q) ||
      r.supplier_name.toLowerCase().includes(q) ||
      (r.received_by || "").toLowerCase().includes(q) ||
      (r.comment || "").toLowerCase().includes(q)
    );
  }
  if (giState.workOrder) rows = rows.filter(r => r.job_number === giState.workOrder);
  if (giState.supplier) rows = rows.filter(r => r.supplier_name === giState.supplier);
  if (giState.receivedBy) rows = rows.filter(r => (r.received_by || "") === giState.receivedBy);
  if (giState.customer) rows = rows.filter(r => customerNameForJobNumber(r.job_number) === giState.customer);

  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const woOptions = (state.ref.works_orders || []).map(w => w.job_number).sort();
  const supplierOptions = (state.ref.suppliers || []).map(s => s.supplier_name).sort();
  const receivedByOptions = [...new Set(allRows.map(r => r.received_by).filter(Boolean))].sort();
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();

  root.innerHTML = goodsInSummaryHtml(sm) + `
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="gi-search" placeholder="Search PO, job, supplier, received by, comment…" value="${esc(giState.search)}">
        <select id="gi-wo-filter">
          <option value="">All works orders</option>
          ${woOptions.map(j => `<option value="${esc(j)}" ${giState.workOrder === j ? "selected" : ""}>${esc(j)}</option>`).join("")}
        </select>
        <select id="gi-supplier-filter">
          <option value="">All suppliers</option>
          ${supplierOptions.map(s => `<option value="${esc(s)}" ${giState.supplier === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
        </select>
        <select id="gi-receivedby-filter">
          <option value="">All received by</option>
          ${receivedByOptions.map(n => `<option value="${esc(n)}" ${giState.receivedBy === n ? "selected" : ""}>${esc(n)}</option>`).join("")}
        </select>
        <select id="gi-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${giState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
      </div>
      <button class="btn" id="gi-new">+ New Goods In</button>
    </div>
    <div class="hint">Acknowledge a delivery against a Purchase Order. Open to everyone — whoever's there to sign for it can log it. Every new record needs a goods received document (delivery note, packing slip, or photo) attached for audit. · ${rows.length.toLocaleString()} record${rows.length === 1 ? "" : "s"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>PO Number</th><th>Supplier</th><th>Destination</th><th>Date</th><th>Quantity</th><th>Received By</th><th>Document</th><th>Comment</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.po_number)}</b></td>
            <td>${esc(r.supplier_name)}</td>
            <td>${r.destination_type === "Stock" ? `Stock — ${esc(r.ship_to_label || "")}` : `WO — ${esc(r.job_number)}`}</td>
            <td>${fmtDate(r.received_date)}</td>
            <td>${statusBadge(r.quantity_status)}${r.quantity_received != null ? ` (${r.quantity_received})` : ""}</td>
            <td>${esc(r.received_by)}</td>
            <td>${r.document_count > 0 ? `📄 ${r.document_count} file${r.document_count === 1 ? "" : "s"}` : `<span class="hint" style="color:#b3261e;">None on file</span>`}</td>
            <td>${esc(r.comment || "")}</td>
            <td class="actions-cell">
              <button class="btn small secondary" data-edit="${r.goods_in_id}">Edit</button>
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.goods_in_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="9"><div class="empty-state">No goods in recorded yet.</div></td></tr>`}
      </tbody>
    </table></div>
  `;

  document.getElementById("gi-search").addEventListener("input", debounce(e => { giState.search = e.target.value; debouncedSearchRerender(e.target, renderGoodsIn); }, 500));
  document.getElementById("gi-wo-filter").addEventListener("change", e => { giState.workOrder = e.target.value; renderGoodsIn(); });
  document.getElementById("gi-supplier-filter").addEventListener("change", e => { giState.supplier = e.target.value; renderGoodsIn(); });
  document.getElementById("gi-receivedby-filter").addEventListener("change", e => { giState.receivedBy = e.target.value; renderGoodsIn(); });
  document.getElementById("gi-customer-filter").addEventListener("change", e => { giState.customer = e.target.value; renderGoodsIn(); });
  document.getElementById("gi-new").addEventListener("click", () => openGoodsInForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const gi = rows.find(r => String(r.goods_in_id) === b.dataset.edit);
    openGoodsInForm(gi);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this goods in record?", "/api/goods-in/" + b.dataset.del, renderGoodsIn)
  ));
}

// Accepted Goods In document types, shared between the file input's
// "accept" attribute here and GOODS_IN_ALLOWED_EXTENSIONS in app.py - keep
// the two in sync if either ever changes. "accept" is only ever a
// convenience filter in the OS file picker, never a security control, so
// the real enforcement is still the server-side check.
const GOODS_IN_ACCEPTED_FILE_TYPES = ".pdf,.jpg,.jpeg,.png,.heic,.heif,.tif,.tiff,.doc,.docx";

function renderGoodsInDocumentsListHtml(documents) {
  if (!documents.length) {
    return `<div class="hint" style="color:#b3261e;">No document on file for this record (it predates this feature) — attach one below.</div>`;
  }
  return `<ul style="margin:0 0 8px;padding-left:18px;">
    ${documents.map(d => `
      <li data-doc-id="${d.document_id}" style="margin-bottom:2px;">
        <a href="/api/goods-in/documents/${d.document_id}/download" target="_blank" rel="noopener">${esc(d.original_filename)}</a>
        <span class="hint" style="display:inline;"> — ${esc(d.uploaded_by)}, ${fmtDateTime(d.uploaded_date)}${d.file_size ? ` (${fmtFileSize(d.file_size)})` : ""}</span>
        ${isAdmin() ? ` <button type="button" class="btn small danger" data-del-doc="${d.document_id}" style="padding:1px 8px;">Remove</button>` : ""}
      </li>`).join("")}
  </ul>`;
}

function wireGoodsInDocumentDeletes(gi) {
  if (!gi) return;
  document.querySelectorAll("[data-del-doc]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Remove this document? This can't be undone.")) return;
    try {
      await api(`/api/goods-in/documents/${b.dataset.delDoc}`, { method: "DELETE" });
      const documents = await api(`/api/goods-in/${gi.goods_in_id}/documents`);
      document.getElementById("gi-documents-list").innerHTML = renderGoodsInDocumentsListHtml(documents);
      wireGoodsInDocumentDeletes(gi);
    } catch (err) {
      alert(err.message);
    }
  }));
}

async function openGoodsInForm(gi = null) {
  const pos = state.ref.purchase_orders || [];
  const shipToAddresses = state.ref.ship_to_addresses || [];
  const isStock = gi ? gi.destination_type === "Stock" : false;
  let documents = [];
  if (gi) {
    try { documents = await api(`/api/goods-in/${gi.goods_in_id}/documents`); } catch (e) { documents = []; }
  }
  const body = `
    <div class="form-row"><label>Purchase Order</label>
      <select name="purchase_order_id" required>
        <option value="">—</option>
        ${pos.map(p => `<option value="${p.purchase_order_id}" ${gi?.purchase_order_id === p.purchase_order_id ? "selected" : ""}>${esc(p.po_number)}</option>`).join("")}
      </select>
    </div>
    <div class="form-row"><label>Destination</label>
      <select name="destination_type" id="gi-destination-type">
        <option value="Works Order" ${!isStock ? "selected" : ""}>Works Order (the job this PO was raised against)</option>
        <option value="Stock" ${isStock ? "selected" : ""}>Stock (going into general inventory)</option>
      </select>
    </div>
    <div class="form-row" id="gi-ship-to-row" style="${isStock ? "" : "display:none;"}">
      <label>Site</label>
      <select name="ship_to_id">
        <option value="">—</option>
        ${shipToAddresses.map(a => `<option value="${a.ship_to_id}" ${gi?.ship_to_id === a.ship_to_id ? "selected" : ""}>${esc(a.label)}</option>`).join("")}
      </select>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Date Received</label><input type="date" name="received_date" value="${fmtDateInput(gi?.received_date) || fmtDateInput(new Date().toISOString())}" required></div>
      <div class="form-row"><label>Quantity</label>
        <select name="quantity_status" required>
          <option value="Full" ${gi?.quantity_status === "Full" ? "selected" : ""}>Full</option>
          <option value="Partial" ${gi?.quantity_status === "Partial" ? "selected" : ""}>Partial</option>
        </select>
      </div>
    </div>
    <div class="form-row"><label>Quantity Received (optional)</label><input type="number" step="0.01" name="quantity_received" value="${gi?.quantity_received ?? ""}" placeholder="e.g. how many, if useful to note"></div>
    <div class="form-row"><label>Received By</label><input name="received_by" value="${esc(gi?.received_by ?? (state.user?.full_name || ""))}" required></div>
    <div class="form-row"><label>Comment</label><textarea name="comment" rows="2">${esc(gi?.comment || "")}</textarea></div>
    <div class="form-row" style="margin-top:6px;">
      <label>${gi ? "Attached Documents" : "Goods Received Document"}</label>
      ${gi ? `<div id="gi-documents-list">${renderGoodsInDocumentsListHtml(documents)}</div>` : ""}
      <div class="file-drop-zone" id="gi-drop-zone">
        <input type="file" name="documents" id="gi-documents-input" multiple accept="${GOODS_IN_ACCEPTED_FILE_TYPES}" ${gi ? "" : "required"}>
        <div class="file-drop-hint">or drag a file here</div>
      </div>
      ${gi
        ? `<div class="hint" style="margin-top:4px;">Add another delivery note, packing slip, or photo if needed (optional).</div>`
        : `<div class="form-warning" style="margin-top:8px;">Required — attach the delivery note, packing slip, or a photo of the goods/paperwork. You can't save a Goods In record without one.</div>`}
    </div>
  `;
  openModal(gi ? "Edit Goods In" : "New Goods In", body, {
    submitLabel: "Save",
    onSubmit: async (fd) => {
      const newFiles = document.getElementById("gi-documents-input").files;
      if (gi) {
        const payload = Object.fromEntries(fd.entries());
        delete payload.documents;
        payload.purchase_order_id = Number(payload.purchase_order_id);
        payload.ship_to_id = payload.destination_type === "Stock" && payload.ship_to_id ? Number(payload.ship_to_id) : null;
        await api("/api/goods-in/" + gi.goods_in_id, { method: "PUT", body: payload });
        if (newFiles.length) {
          const docFd = new FormData();
          for (const f of newFiles) docFd.append("documents", f);
          await api(`/api/goods-in/${gi.goods_in_id}/documents`, { method: "POST", body: docFd });
        }
      } else {
        // fd is the modal's own FormData, built straight from this <form> -
        // it already carries every field above (purchase_order_id,
        // destination_type, ... and the required "documents" file(s)), so
        // it can go to the API as multipart/form-data exactly as-is. That
        // way the record and its required document are created together,
        // in one request, atomically on the server (see create_goods_in).
        await api("/api/goods-in", { method: "POST", body: fd });
      }
      closeModal();
      renderGoodsIn();
    },
  });
  document.getElementById("gi-destination-type").addEventListener("change", (e) => {
    document.getElementById("gi-ship-to-row").style.display = e.target.value === "Stock" ? "" : "none";
  });
  wireGoodsInDocumentDeletes(gi);
  wireFileDropZone("gi-drop-zone", "gi-documents-input");
}

// Lets a file (or several) be dragged straight from a folder window onto
// the upload box, as an alternative to clicking it and using the
// operating system's own "Choose File" dialog - useful in its own right,
// and a real workaround for anyone whose Windows file-picker is stuck or
// unreliable (a known OS/browser-level issue entirely outside this app's
// control - see README.md, "A frozen file-picker dialog"). Sets the real
// input's .files via DataTransfer, so everything downstream (the browser's
// own "n files selected" display, the required-field check, and reading
// the files back out on submit) keeps working exactly as if they'd been
// picked the normal way.
function wireFileDropZone(zoneId, inputId) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  if (!zone || !input) return;
  ["dragenter", "dragover"].forEach(evt => zone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.add("drag-over");
  }));
  ["dragleave", "dragend"].forEach(evt => zone.addEventListener(evt, (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove("drag-over");
  }));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove("drag-over");
    const dropped = e.dataTransfer?.files;
    if (!dropped || !dropped.length) return;
    // Appends to whatever's already chosen, rather than replacing it, so
    // dragging a second file in doesn't silently lose the first one.
    const merged = new DataTransfer();
    for (const f of input.files) merged.items.add(f);
    for (const f of dropped) merged.items.add(f);
    input.files = merged.files;
  });
}

/* ======================================================================
   DESPATCH NOTES
   ====================================================================== */
let dnState = { search: "", workOrder: "", customer: "", despatchedBy: "", status: "", contractCode: "" };

async function renderDespatchNotes() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading despatch notes…</div>`;
  const allRows = await api("/api/despatch-notes");
  const sm = await loadScreenSummary();

  let rows = allRows;
  if (dnState.search) {
    const q = dnState.search.toLowerCase();
    rows = rows.filter(r =>
      r.despatch_number.toLowerCase().includes(q) ||
      r.job_number.toLowerCase().includes(q) ||
      (r.customer_name || "").toLowerCase().includes(q) ||
      (r.despatched_by || "").toLowerCase().includes(q) ||
      (r.carrier || "").toLowerCase().includes(q)
    );
  }
  if (dnState.workOrder) rows = rows.filter(r => r.job_number === dnState.workOrder);
  if (dnState.customer) rows = rows.filter(r => r.customer_name === dnState.customer);
  if (dnState.despatchedBy) rows = rows.filter(r => (r.despatched_by || "") === dnState.despatchedBy);
  if (dnState.status) rows = rows.filter(r => r.status === dnState.status);
  if (dnState.contractCode) rows = rows.filter(r => (woRefForJobNumber(r.job_number)?.contract_code || "") === dnState.contractCode);

  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const woOptions = (state.ref.works_orders || []).map(w => w.job_number).sort();
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const despatchedByOptions = [...new Set(allRows.map(r => r.despatched_by).filter(Boolean))].sort();
  const statusOptions = ["Draft", "Despatched", "Cancelled"];
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();

  root.innerHTML = despatchSummaryHtml(sm) + `
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="dn-search" placeholder="Search despatch no., job, customer, despatched by, carrier…" value="${esc(dnState.search)}">
        <select id="dn-wo-filter">
          <option value="">All works orders</option>
          ${woOptions.map(j => `<option value="${esc(j)}" ${dnState.workOrder === j ? "selected" : ""}>${esc(j)}</option>`).join("")}
        </select>
        <select id="dn-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${dnState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="dn-despatchedby-filter">
          <option value="">All despatched by</option>
          ${despatchedByOptions.map(n => `<option value="${esc(n)}" ${dnState.despatchedBy === n ? "selected" : ""}>${esc(n)}</option>`).join("")}
        </select>
        <select id="dn-status-filter">
          <option value="">All statuses</option>
          ${statusOptions.map(s => `<option value="${s}" ${dnState.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <select id="dn-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${dnState.contractCode === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("despatch_notes") ? `<button class="btn" id="dn-new">+ New Despatch Note</button>` : ""}
    </div>
    <div class="hint">Goods despatched to customers against a Works Order. · ${rows.length.toLocaleString()} record${rows.length === 1 ? "" : "s"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Despatch No.</th><th>Works Order</th><th>Customer</th><th>Despatch Date</th><th>Carrier</th><th>Despatched By</th><th>Status</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.despatch_number)}</b></td>
            <td>${esc(r.job_number)} — ${esc(r.job_name)}</td>
            <td>${esc(r.customer_name)}</td>
            <td>${fmtDate(r.despatch_date)}</td>
            <td>${esc(r.carrier || "")}</td>
            <td>${esc(r.despatched_by || "")}</td>
            <td>${statusBadge(r.status)}</td>
            <td class="actions-cell">
              <a class="btn small secondary" href="/api/despatch-notes/${r.despatch_note_id}/pdf" title="Download Despatch Note PDF">PDF</a>
              ${r.signature_filename
                ? `<a class="btn small secondary" href="/api/despatch-notes/${r.despatch_note_id}/signature" target="_blank" rel="noopener" title="Signed by ${esc(r.signature_name || "")} on ${fmtDate(r.signature_date)}">Signed &#10003;</a>`
                : (r.status === "Despatched" && canWrite("despatch_notes") ? `<button class="btn small secondary" data-sign="${r.despatch_note_id}">Sign</button>` : "")}
              ${canWrite("despatch_notes") ? `<button class="btn small secondary" data-edit="${r.despatch_note_id}">Edit</button>` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.despatch_note_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="8"><div class="empty-state">No despatch notes raised yet.</div></td></tr>`}
      </tbody>
    </table></div>
  `;

  document.getElementById("dn-search").addEventListener("input", debounce(e => { dnState.search = e.target.value; debouncedSearchRerender(e.target, renderDespatchNotes); }, 500));
  document.getElementById("dn-wo-filter").addEventListener("change", e => { dnState.workOrder = e.target.value; renderDespatchNotes(); });
  document.getElementById("dn-customer-filter").addEventListener("change", e => { dnState.customer = e.target.value; renderDespatchNotes(); });
  document.getElementById("dn-despatchedby-filter").addEventListener("change", e => { dnState.despatchedBy = e.target.value; renderDespatchNotes(); });
  document.getElementById("dn-status-filter").addEventListener("change", e => { dnState.status = e.target.value; renderDespatchNotes(); });
  document.getElementById("dn-code-filter").addEventListener("change", e => { dnState.contractCode = e.target.value; renderDespatchNotes(); });
  document.getElementById("dn-new")?.addEventListener("click", () => openDespatchNoteForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const dn = rows.find(r => String(r.despatch_note_id) === b.dataset.edit);
    openDespatchNoteForm(dn);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this despatch note?", "/api/despatch-notes/" + b.dataset.del, renderDespatchNotes)
  ));
  root.querySelectorAll("[data-sign]").forEach(b => b.addEventListener("click", () => {
    const dn = rows.find(r => String(r.despatch_note_id) === b.dataset.sign);
    openDespatchSignOverlay(dn);
  }));
}

let dnFormLines = [];

function customerAddressForWorksOrder(worksOrderId) {
  const wo = state.ref.works_orders.find(w => w.works_order_id === Number(worksOrderId));
  const cust = wo && state.ref.customers.find(c => c.customer_id === wo.customer_id);
  return cust?.address || "";
}

async function openDespatchNoteForm(dn = null) {
  const wos = state.ref.works_orders;
  let nextNumberPreview = dn ? dn.despatch_number : "…";
  if (!dn) {
    try { nextNumberPreview = (await api("/api/despatch-notes/next-number")).despatch_number; } catch (e) { nextNumberPreview = "TMD-????"; }
  }
  dnFormLines = (dn?.lines || []).map(l => ({ ...l }));
  const body = `
    <div class="form-row"><label>Despatch Number</label><input value="${esc(nextNumberPreview)}" disabled>
      ${!dn ? `<div class="hint" style="margin:6px 0 0;">Assigned automatically when saved — the next available number.</div>` : ""}
    </div>
    <div class="form-row"><label>Works Order</label>
      <select name="works_order_id" id="dn-works-order" required>
        <option value="">—</option>
        ${wos.map(w => `<option value="${w.works_order_id}" ${dn?.works_order_id === w.works_order_id ? "selected" : ""}>${esc(w.job_number)} — ${esc(w.job_name)}</option>`).join("")}
      </select>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Despatch Date</label><input type="date" name="despatch_date" value="${fmtDateInput(dn?.despatch_date) || fmtDateInput(new Date().toISOString())}" required></div>
      <div class="form-row"><label>Status</label>
        <select name="status">${["Draft", "Despatched", "Cancelled"].map(s => `<option ${dn?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    <div class="form-row"><label>Delivery Address</label><textarea name="delivery_address" id="dn-delivery-address" rows="3" placeholder="Defaults to the customer's address once a Works Order is picked">${esc(dn?.delivery_address || "")}</textarea></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Carrier / Method</label><input name="carrier" value="${esc(dn?.carrier || "")}" placeholder="e.g. Own vehicle, DPD, Customer collection"></div>
      <div class="form-row"><label>Despatched By</label><input name="despatched_by" value="${esc(dn?.despatched_by ?? (state.user?.full_name || ""))}"></div>
    </div>

    <h3 style="margin-top:16px;font-size:13px;color:var(--navy);">Items</h3>
    <div class="hint">Add each item being despatched — Qty, Part No., Description.</div>
    <div id="dn-lines-rows" style="margin-top:8px;"></div>
    <button class="btn secondary small" id="dn-add-line" type="button" style="margin-top:6px;">+ Add Line</button>

    <div class="form-row" style="margin-top:12px;"><label>Notes</label><textarea name="notes" rows="2">${esc(dn?.notes || "")}</textarea></div>
  `;
  openModal(dn ? `Edit Despatch Note — ${dn.despatch_number}` : "New Despatch Note", body, {
    submitLabel: dn ? "Save Changes" : "Create Despatch Note",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      payload.works_order_id = Number(payload.works_order_id);
      payload.lines = dnFormLines.filter(l => (Number(l.qty) || 0) > 0 || l.description || l.part_number);
      if (dn) await api("/api/despatch-notes/" + dn.despatch_note_id, { method: "PUT", body: payload });
      else await api("/api/despatch-notes", { method: "POST", body: payload });
      closeModal();
      renderDespatchNotes();
    },
  });
  renderDnLinesUI();
  // Auto-fill the delivery address from the selected Works Order's customer
  // - but only while the field is still untouched, so picking a different
  // job never clobbers an address someone's already deliberately edited.
  const addressField = document.getElementById("dn-delivery-address");
  let addressTouched = !!dn?.delivery_address;
  addressField.addEventListener("input", () => { addressTouched = true; });
  document.getElementById("dn-works-order").addEventListener("change", (e) => {
    if (!addressTouched) {
      addressField.value = customerAddressForWorksOrder(e.target.value);
    }
  });
}

function renderDnLinesUI() {
  const rowsEl = document.getElementById("dn-lines-rows");
  if (!rowsEl) return;
  rowsEl.innerHTML = dnFormLines.length ? `
    <div class="purchase-line-row" style="grid-template-columns:.7fr 1fr 2.2fr auto;margin-bottom:2px;">
      <span class="wizard-step-label" style="margin:0;">Qty</span>
      <span class="wizard-step-label" style="margin:0;">Part No.</span>
      <span class="wizard-step-label" style="margin:0;">Description</span>
      <span></span>
    </div>` : `<div class="hint">No items added yet.</div>`;
  rowsEl.innerHTML += dnFormLines.map((l, idx) => `
    <div class="purchase-line-row" data-row="${idx}" style="grid-template-columns:.7fr 1fr 2.2fr auto;">
      <input type="number" step="1" min="0" placeholder="Qty" data-field="qty" value="${l.qty ?? ""}">
      <input placeholder="Part No." data-field="part_number" value="${esc(l.part_number || "")}">
      <input placeholder="Description" data-field="description" value="${esc(l.description || "")}">
      <button class="btn small danger" type="button" data-remove-line="${idx}">✕</button>
    </div>`).join("");

  rowsEl.querySelectorAll(".purchase-line-row input").forEach(inp => inp.addEventListener("input", () => {
    const idx = Number(inp.closest(".purchase-line-row").dataset.row);
    dnFormLines[idx][inp.dataset.field] = inp.value;
  }));
  rowsEl.querySelectorAll("[data-remove-line]").forEach(b => b.addEventListener("click", () => {
    dnFormLines.splice(Number(b.dataset.removeLine), 1);
    renderDnLinesUI();
  }));
  document.getElementById("dn-add-line").addEventListener("click", () => {
    dnFormLines.push({ qty: "", part_number: "", description: "" });
    renderDnLinesUI();
  });
}

/* ------------------------------------------------------------------------
   Despatch Note signature capture - a dedicated full-screen, mobile-first
   screen (not the centered #modal-root dialog used everywhere else),
   meant to be used on the despatching staff member's own already-logged-in
   phone or tablet at the point of delivery. Draws on an HTML5 canvas via
   pointer events (covers touch, mouse and pen in one set of listeners),
   then POSTs the drawing as a PNG data URL to
   /api/despatch-notes/<id>/signature. See README.md, "Despatch Note
   signatures" for the manual SharePoint hand-off this leads into.
   ------------------------------------------------------------------------ */
function closeDespatchSignOverlay() {
  document.getElementById("sign-overlay-root").innerHTML = "";
}

function openDespatchSignOverlay(dn) {
  const root = document.getElementById("sign-overlay-root");
  const today = fmtDateInput(new Date().toISOString());
  root.innerHTML = `
    <div class="sign-overlay">
      <div class="sign-overlay-header">
        <div>
          <div class="sign-overlay-title">${esc(dn.despatch_number)}</div>
          <div class="sign-overlay-subtitle">${esc(dn.job_number)} — ${esc(dn.customer_name)}</div>
        </div>
        <button type="button" class="btn secondary small" id="sign-cancel-top">Cancel</button>
      </div>
      <div class="sign-overlay-body">
        <div class="form-row"><label>Name</label><input id="sign-name" placeholder="Print name of whoever received the goods" value="${esc(dn.despatched_by || "")}" required></div>
        <div class="form-row"><label>Date</label><input type="date" id="sign-date" value="${today}"></div>
        <div class="form-row">
          <label>Signature</label>
          <div class="sign-canvas-wrap"><canvas id="sign-canvas"></canvas></div>
          <button type="button" class="btn secondary small" id="sign-clear" style="margin-top:8px;">Clear</button>
        </div>
        <div class="form-error" id="sign-error"></div>
      </div>
      <div class="sign-overlay-actions">
        <button type="button" class="btn secondary" id="sign-cancel">Cancel</button>
        <button type="button" class="btn" id="sign-save">Save Signature</button>
      </div>
    </div>`;

  const canvas = document.getElementById("sign-canvas");
  const ctx = canvas.getContext("2d");
  let hasDrawn = false, drawing = false, lastX = 0, lastY = 0, cssW = 0, cssH = 0;

  function sizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    cssW = rect.width; cssH = rect.height;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  // The wrapper's size depends on layout that's only final once this is in
  // the DOM, so size the canvas on the next frame rather than immediately.
  requestAnimationFrame(sizeCanvas);
  window.addEventListener("resize", sizeCanvas);

  function posFromEvent(e) {
    const rect = canvas.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }
  function strokeTo(x, y) {
    ctx.beginPath();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.lineWidth = 2.5;
    ctx.strokeStyle = "#0f2942";
    ctx.moveTo(lastX, lastY);
    ctx.lineTo(x, y);
    ctx.stroke();
    lastX = x; lastY = y;
  }
  canvas.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    drawing = true; hasDrawn = true;
    const p = posFromEvent(e);
    lastX = p.x; lastY = p.y;
    canvas.setPointerCapture(e.pointerId);
    // A dot for a single tap/click, not just a line between two moves.
    ctx.beginPath();
    ctx.fillStyle = "#0f2942";
    ctx.arc(p.x, p.y, 1.3, 0, Math.PI * 2);
    ctx.fill();
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    e.preventDefault();
    const p = posFromEvent(e);
    strokeTo(p.x, p.y);
  });
  ["pointerup", "pointercancel", "pointerleave"].forEach(evt =>
    canvas.addEventListener(evt, () => { drawing = false; })
  );

  document.getElementById("sign-clear").addEventListener("click", () => {
    ctx.clearRect(0, 0, cssW, cssH);
    hasDrawn = false;
  });
  document.getElementById("sign-cancel").addEventListener("click", closeDespatchSignOverlay);
  document.getElementById("sign-cancel-top").addEventListener("click", closeDespatchSignOverlay);

  document.getElementById("sign-save").addEventListener("click", async () => {
    const errEl = document.getElementById("sign-error");
    errEl.textContent = "";
    const name = document.getElementById("sign-name").value.trim();
    if (!name) { errEl.textContent = "Enter the name of whoever received the goods."; return; }
    if (!hasDrawn) { errEl.textContent = "Please draw a signature before saving."; return; }
    const btn = document.getElementById("sign-save");
    btn.disabled = true;
    try {
      await api(`/api/despatch-notes/${dn.despatch_note_id}/signature`, {
        method: "POST",
        body: { name, date: document.getElementById("sign-date").value, signature: canvas.toDataURL("image/png") },
      });
      closeDespatchSignOverlay();
      renderDespatchNotes();
    } catch (err) {
      errEl.textContent = err.message;
    } finally {
      btn.disabled = false;
    }
  });
}

/* ======================================================================
   INVENTORY
   ====================================================================== */
let inventoryState = { search: "", view: "items", supplier: "", lowOnly: false };

// An item is "low" once its total quantity is at or below its Reorder Level (a level of 0/blank means no limit is set).
function stockLevel(r) {
  if (!(r.reorder_level > 0)) return "ok";
  if (r.total_quantity <= 0) return "out";
  return r.total_quantity <= r.reorder_level ? "low" : "ok";
}

function aggregateStockBySite(items) {
  const map = new Map();
  for (const item of items) {
    for (const a of item.allocations) {
      if (!a.ship_to_id) continue;
      if (!map.has(a.ship_to_id)) map.set(a.ship_to_id, { key: a.ship_to_label, lines: [], total_value: 0 });
      const agg = map.get(a.ship_to_id);
      const value = a.quantity * item.purchase_cost;
      agg.lines.push({ part_number: item.part_number, description: item.description, quantity: a.quantity, unit: item.unit, value: round2(value) });
      agg.total_value += value;
    }
  }
  return [...map.values()].map(v => ({
    label: v.key,
    lines: v.lines.sort((a, b) => a.part_number.localeCompare(b.part_number)),
    item_count: v.lines.length,
    total_value: round2(v.total_value),
  }));
}

function aggregateStockByWorksOrder(items) {
  const map = new Map();
  for (const item of items) {
    for (const a of item.allocations) {
      if (!a.works_order_id) continue;
      if (!map.has(a.works_order_id)) map.set(a.works_order_id, { job_number: a.job_number, job_name: a.job_name, lines: [], total_value: 0 });
      const agg = map.get(a.works_order_id);
      const value = a.quantity * item.purchase_cost;
      agg.lines.push({ part_number: item.part_number, description: item.description, quantity: a.quantity, unit: item.unit, value: round2(value) });
      agg.total_value += value;
    }
  }
  return [...map.values()].map(v => ({
    job_number: v.job_number,
    job_name: v.job_name,
    lines: v.lines.sort((a, b) => a.part_number.localeCompare(b.part_number)),
    item_count: v.lines.length,
    total_value: round2(v.total_value),
  }));
}

function round2(n) { return Math.round(n * 100) / 100; }

async function renderInventory() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading inventory…</div>`;
  const items = await api("/api/stock-items");
  const view = inventoryState.view;
  const q = inventoryState.search.toLowerCase();

  const toggleHtml = `
    <div class="toggle-group">
      <button class="toggle-btn ${view === "items" ? "active" : ""}" id="inv-view-items">Items</button>
      <button class="toggle-btn ${view === "site" ? "active" : ""}" id="inv-view-site">By Site</button>
      <button class="toggle-btn ${view === "wo" ? "active" : ""}" id="inv-view-wo">By Works Order</button>
      <button class="toggle-btn ${view === "movements" ? "active" : ""}" id="inv-view-movements">Movements</button>
    </div>`;

  if (view === "items") {
    let filtered = items;
    if (q) filtered = filtered.filter(r => r.part_number.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q));
    if (inventoryState.supplier) filtered = filtered.filter(r => (r.supplier_name || "") === inventoryState.supplier);
    const lowItems = items.filter(r => stockLevel(r) !== "ok").sort((a, b) => (a.total_quantity - a.reorder_level) - (b.total_quantity - b.reorder_level));
    if (inventoryState.lowOnly) filtered = filtered.filter(r => stockLevel(r) !== "ok");
    const totalValue = items.reduce((s, r) => s + r.total_value, 0);
    const totalSaleValue = items.reduce((s, r) => s + r.total_sale_value, 0);
    const lowStockCount = lowItems.length;
    const supplierOptions = (state.ref.suppliers || []).map(s => s.supplier_name).sort();
    root.innerHTML = `
      <div class="kpi-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:16px;">
        <div class="kpi-card"><div class="label">Stock Items</div><div class="value">${items.length.toLocaleString()}</div></div>
        <div class="kpi-card"><div class="label">Total Stock Value (Cost)</div><div class="value">${fmtMoney(totalValue)}</div></div>
        <div class="kpi-card"><div class="label">Potential Value (Sale Price)</div><div class="value">${fmtMoney(totalSaleValue)}</div></div>
        <div class="kpi-card ${lowStockCount > 0 ? "amber" : ""}"><div class="label">Low Stock Items</div><div class="value">${lowStockCount}</div></div>
      </div>
      ${lowItems.length ? `
      <div class="low-stock-warning" role="alert">
        <div class="lsw-head">
          <b>⚠ Low stock warning: ${lowItems.length} item${lowItems.length === 1 ? " is" : "s are"} at or below the reorder level</b>
          <button class="btn small secondary" id="inv-low-toggle">${inventoryState.lowOnly ? "Show all items" : "Show only low stock"}</button>
        </div>
        <div class="lsw-list">
          ${lowItems.slice(0, 8).map(r => `<span class="lsw-chip ${stockLevel(r)}">${esc(r.part_number)}: ${r.total_quantity} of ${r.reorder_level} ${esc(r.unit || "")}</span>`).join("")}
          ${lowItems.length > 8 ? `<span class="lsw-more">+ ${lowItems.length - 8} more</span>` : ""}
        </div>
      </div>` : ""}
      <div class="toolbar">
        <div class="filters">
          ${toggleHtml}
          <input type="text" id="inv-search" placeholder="Search part number or description…" value="${esc(inventoryState.search)}">
          <select id="inv-supplier-filter">
            <option value="">All suppliers</option>
            ${supplierOptions.map(s => `<option value="${esc(s)}" ${inventoryState.supplier === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
          </select>
        </div>
        ${canWrite("inventory") ? `<button class="btn" id="inv-new">+ New Stock Item</button>` : ""}
      </div>
      <div class="hint">${filtered.length.toLocaleString()} stock item${filtered.length === 1 ? "" : "s"}</div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr>
          <th>Part Number</th><th>Description</th><th>Supplier</th>
          <th class="num">Total Qty</th><th class="num">Reorder Level</th><th>Stock</th><th class="num">Purchase Cost</th><th class="num">Sale Price</th><th class="num">Total Value</th><th></th>
        </tr></thead>
        <tbody>
          ${filtered.length ? filtered.map(r => `
            <tr>
              <td><b>${esc(r.part_number)}</b></td>
              <td>${esc(r.description || "")}</td>
              <td>${esc(r.supplier_name || "")}</td>
              <td class="num">${stockLevel(r) !== "ok" ? lightBadge(stockLevel(r) === "out" ? "red" : "amber", `${r.total_quantity} ${r.unit || ""}`) : `${r.total_quantity} ${esc(r.unit)}`}</td>
              <td class="num">${r.reorder_level > 0 ? r.reorder_level : `<span class="hint">not set</span>`}</td>
              <td>${stockLevel(r) === "out" ? lightBadge("red", "Out of stock") : stockLevel(r) === "low" ? lightBadge("amber", "Low stock") : ""}</td>
              <td class="num">${fmtMoney(r.purchase_cost)}</td>
              <td class="num">${fmtMoney(r.sale_price)}</td>
              <td class="num">${fmtMoney(r.total_value)}</td>
              <td class="actions-cell">
                <button class="btn small secondary" data-allocations="${r.stock_item_id}">Allocations</button>
                ${canWrite("inventory") ? `<button class="btn small secondary" data-edit="${r.stock_item_id}">Edit</button>` : ""}
                ${isAdmin() ? `<button class="btn small danger" data-del="${r.stock_item_id}">Delete</button>` : ""}
              </td>
            </tr>`).join("") : `<tr><td colspan="10"><div class="empty-state">${inventoryState.lowOnly ? "No items are low on stock." : "No stock items yet."} ${canWrite("inventory") ? "Create the first one above." : ""}</div></td></tr>`}
        </tbody>
      </table></div>
    `;
    document.getElementById("inv-search").addEventListener("input", debounce(e => { inventoryState.search = e.target.value; debouncedSearchRerender(e.target, renderInventory); }, 500));
    document.getElementById("inv-supplier-filter").addEventListener("change", e => { inventoryState.supplier = e.target.value; renderInventory(); });
    document.getElementById("inv-low-toggle")?.addEventListener("click", () => { inventoryState.lowOnly = !inventoryState.lowOnly; renderInventory(); });
    document.getElementById("inv-new")?.addEventListener("click", () => openStockItemForm());
    root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
      openStockItemForm(items.find(r => String(r.stock_item_id) === b.dataset.edit));
    }));
    root.querySelectorAll("[data-allocations]").forEach(b => b.addEventListener("click", () => {
      openAllocationsModal(items.find(r => String(r.stock_item_id) === b.dataset.allocations));
    }));
    root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
      handleDelete("Delete this stock item and all its allocations?", "/api/stock-items/" + b.dataset.del, renderInventory)
    ));

  } else if (view === "site") {
    let rows = aggregateStockBySite(items);
    if (q) rows = rows.filter(r => r.label.toLowerCase().includes(q) || r.lines.some(l => l.part_number.toLowerCase().includes(q) || (l.description || "").toLowerCase().includes(q)));
    rows.sort((a, b) => b.total_value - a.total_value);
    const groupRows = (r) => r.lines.map(l => `
      <tr>
        <td></td>
        <td>${esc(l.part_number)}</td>
        <td>${esc(l.description || "")}</td>
        <td class="num">${l.quantity} ${esc(l.unit)}</td>
        <td class="num">${fmtMoney(l.value)}</td>
      </tr>`).join("") + `
      <tr class="subtotal-row">
        <td colspan="4" style="text-align:right;"><b>${esc(r.label)} total</b></td>
        <td class="num"><b>${fmtMoney(r.total_value)}</b></td>
      </tr>`;
    root.innerHTML = `
      <div class="toolbar">
        <div class="filters">${toggleHtml}<input type="text" id="inv-search" placeholder="Search site, part number or description…" value="${esc(inventoryState.search)}"></div>
        <button class="btn secondary" id="inv-export">Export to CSV</button>
      </div>
      <div class="hint">${rows.length.toLocaleString()} site${rows.length === 1 ? "" : "s"} holding stock</div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Site</th><th>Part Number</th><th>Description</th><th class="num">Quantity</th><th class="num">Value</th></tr></thead>
        <tbody>${rows.length ? rows.map(r => `<tr><td colspan="5" class="group-header"><b>${esc(r.label)}</b></td></tr>${groupRows(r)}`).join("") : `<tr><td colspan="5"><div class="empty-state">No stock currently allocated to any site.</div></td></tr>`}</tbody>
      </table></div>
    `;
    document.getElementById("inv-search").addEventListener("input", debounce(e => { inventoryState.search = e.target.value; debouncedSearchRerender(e.target, renderInventory); }, 500));
    document.getElementById("inv-export").addEventListener("click", () => {
      const csvRows = [];
      rows.forEach(r => {
        r.lines.forEach(l => csvRows.push([r.label, l.part_number, l.description || "", l.quantity, l.unit, l.value.toFixed(2)]));
        csvRows.push([r.label + " total", "", "", "", "", r.total_value.toFixed(2)]);
      });
      downloadCSV("stock-by-site.csv", ["Site", "Part Number", "Description", "Quantity", "Unit", "Value"], csvRows);
    });

  } else if (view === "wo") {
    let rows = aggregateStockByWorksOrder(items);
    if (q) rows = rows.filter(r => r.job_number.toLowerCase().includes(q) || (r.job_name || "").toLowerCase().includes(q) || r.lines.some(l => l.part_number.toLowerCase().includes(q) || (l.description || "").toLowerCase().includes(q)));
    rows.sort((a, b) => b.total_value - a.total_value);
    const groupRowsWo = (r) => r.lines.map(l => `
      <tr>
        <td></td>
        <td>${esc(l.part_number)}</td>
        <td>${esc(l.description || "")}</td>
        <td class="num">${l.quantity} ${esc(l.unit)}</td>
        <td class="num">${fmtMoney(l.value)}</td>
      </tr>`).join("") + `
      <tr class="subtotal-row">
        <td colspan="4" style="text-align:right;"><b>${esc(r.job_number)} total</b></td>
        <td class="num"><b>${fmtMoney(r.total_value)}</b></td>
      </tr>`;
    root.innerHTML = `
      <div class="toolbar">
        <div class="filters">${toggleHtml}<input type="text" id="inv-search" placeholder="Search works order, part number or description…" value="${esc(inventoryState.search)}"></div>
        <button class="btn secondary" id="inv-export">Export to CSV</button>
      </div>
      <div class="hint">${rows.length.toLocaleString()} works order${rows.length === 1 ? "" : "s"} with stock allocated</div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Works Order</th><th>Part Number</th><th>Description</th><th class="num">Quantity</th><th class="num">Value</th></tr></thead>
        <tbody>${rows.length ? rows.map(r => `<tr><td colspan="5" class="group-header"><b>${esc(r.job_number)}</b> — ${esc(r.job_name || "")}</td></tr>${groupRowsWo(r)}`).join("") : `<tr><td colspan="5"><div class="empty-state">No stock currently allocated to any works order.</div></td></tr>`}</tbody>
      </table></div>
    `;
    document.getElementById("inv-search").addEventListener("input", debounce(e => { inventoryState.search = e.target.value; debouncedSearchRerender(e.target, renderInventory); }, 500));
    document.getElementById("inv-export").addEventListener("click", () => {
      const csvRows = [];
      rows.forEach(r => {
        r.lines.forEach(l => csvRows.push([r.job_number, r.job_name || "", l.part_number, l.description || "", l.quantity, l.unit, l.value.toFixed(2)]));
        csvRows.push([r.job_number + " total", "", "", "", "", "", r.total_value.toFixed(2)]);
      });
      downloadCSV("stock-by-works-order.csv", ["Works Order", "Job Name", "Part Number", "Description", "Quantity", "Unit", "Value"], csvRows);
    });

  } else {
    const movements = await api("/api/stock-movements");
    let rows = movements;
    if (q) rows = rows.filter(r => r.part_number.toLowerCase().includes(q) || (r.description || "").toLowerCase().includes(q));
    const destLabel = (r) => r.to_ship_to_label ? `Site: ${r.to_ship_to_label}` : `Job: ${r.to_job_number}`;
    const srcLabel = (r) => r.from_ship_to_label ? `Site: ${r.from_ship_to_label}` : r.from_job_number ? `Job: ${r.from_job_number}` : "New Stock (Receipt)";
    root.innerHTML = `
      <div class="toolbar">
        <div class="filters">${toggleHtml}<input type="text" id="inv-search" placeholder="Search part number or description…" value="${esc(inventoryState.search)}"></div>
        <button class="btn secondary" id="inv-export">Export to CSV</button>
      </div>
      <div class="hint">${rows.length.toLocaleString()} movement${rows.length === 1 ? "" : "s"}</div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Date</th><th>Part Number</th><th>From</th><th>To</th><th class="num">Qty</th><th>Moved By</th><th>Notes</th></tr></thead>
        <tbody>${rows.length ? rows.map(r => `
          <tr>
            <td>${fmtDate(r.movement_date)}</td>
            <td><b>${esc(r.part_number)}</b></td>
            <td>${esc(srcLabel(r))}</td>
            <td>${esc(destLabel(r))}</td>
            <td class="num">${r.quantity}</td>
            <td>${esc(r.moved_by || "")}</td>
            <td>${esc(r.notes || "")}</td>
          </tr>`).join("") : `<tr><td colspan="7"><div class="empty-state">No stock movements recorded yet.</div></td></tr>`}</tbody>
      </table></div>
    `;
    document.getElementById("inv-search").addEventListener("input", debounce(e => { inventoryState.search = e.target.value; debouncedSearchRerender(e.target, renderInventory); }, 500));
    document.getElementById("inv-export").addEventListener("click", () => {
      downloadCSV("stock-movements.csv", ["Date", "Part Number", "From", "To", "Quantity", "Moved By", "Notes"],
        rows.map(r => [fmtDate(r.movement_date), r.part_number, srcLabel(r), destLabel(r), r.quantity, r.moved_by || "", r.notes || ""]));
    });
  }

  root.querySelectorAll("#inv-view-items,#inv-view-site,#inv-view-wo,#inv-view-movements").forEach(b => {
    b.addEventListener("click", () => {
      inventoryState.view = b.id.replace("inv-view-", "");
      inventoryState.search = "";
      inventoryState.supplier = "";
      renderInventory();
    });
  });
}

function openStockItemForm(item = null) {
  const suppliers = state.ref.suppliers;
  const shipToAddresses = state.ref.ship_to_addresses || [];
  const body = `
    <div class="form-grid-2">
      <div class="form-row"><label>Part Number</label><input name="part_number" value="${esc(item?.part_number || "")}" required></div>
      <div class="form-row"><label>Unit</label><input name="unit" value="${esc(item?.unit || "each")}"></div>
    </div>
    <div class="form-row"><label>Description</label><input name="description" value="${esc(item?.description || "")}"></div>
    <div class="form-row"><label>Supplier</label>
      <select name="supplier_id">
        <option value="">—</option>
        ${suppliers.map(s => `<option value="${s.supplier_id}" ${item?.supplier_id === s.supplier_id ? "selected" : ""}>${esc(s.supplier_name)}</option>`).join("")}
      </select>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Purchase Cost (£)</label><input type="number" step="0.01" name="purchase_cost" value="${item?.purchase_cost ?? ""}"></div>
      <div class="form-row"><label>Sale Price (£)</label><input type="number" step="0.01" name="sale_price" value="${item?.sale_price ?? ""}"></div>
    </div>
    <div class="form-row" style="max-width:200px;"><label>Reorder Level (warn when stock reaches this)</label><input type="number" step="1" min="0" name="reorder_level" value="${item?.reorder_level ?? ""}"></div>
    <div class="form-row"><label>Notes</label><textarea name="notes" rows="2">${esc(item?.notes || "")}</textarea></div>
    ${!item ? `
    <div class="hint" style="margin-top:4px;">Optional — if you already have some of this in stock, record it here so it's not sitting at zero. For adding stock to an existing item later, use its Allocations → Move Stock instead.</div>
    <div class="form-grid-2">
      <div class="form-row"><label>Initial Quantity</label><input type="number" step="1" min="0" name="initial_quantity"></div>
      <div class="form-row"><label>Held At (Site)</label>
        <select name="initial_ship_to_id">
          <option value="">—</option>
          ${shipToAddresses.map(a => `<option value="${a.ship_to_id}">${esc(a.label)}</option>`).join("")}
        </select>
      </div>
    </div>` : ""}
  `;
  openModal(item ? `Edit Stock Item — ${item.part_number}` : "New Stock Item", body, {
    submitLabel: "Save",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      payload.supplier_id = payload.supplier_id ? Number(payload.supplier_id) : null;
      if (!item) payload.initial_ship_to_id = payload.initial_ship_to_id ? Number(payload.initial_ship_to_id) : null;
      if (item) await api("/api/stock-items/" + item.stock_item_id, { method: "PUT", body: payload });
      else await api("/api/stock-items", { method: "POST", body: payload });
      closeModal();
      renderInventory();
    },
  });
}

async function openAllocationsModal(item) {
  const shipToAddresses = state.ref.ship_to_addresses || [];
  const worksOrders = state.ref.works_orders || [];
  const rowsHtml = item.allocations.map(a => `
    <tr>
      <td>${a.ship_to_id ? `Site: ${esc(a.ship_to_label)}` : `Job: ${esc(a.job_number)} — ${esc(a.job_name || "")}`}</td>
      <td class="num">${a.quantity} ${esc(item.unit)}</td>
      <td>${esc(a.notes || "")}</td>
      <td class="actions-cell">
        ${canWrite("inventory") ? `<button class="btn small danger" type="button" data-remove-alloc="${a.allocation_id}">Remove</button>` : ""}
      </td>
    </tr>`).join("");
  const sourceOptions = [`<option value="receipt">New Stock (Receipt)</option>`].concat(
    item.allocations.map(a => {
      const label = a.ship_to_id ? `Site: ${a.ship_to_label}` : `Job: ${a.job_number}`;
      const value = a.ship_to_id ? `site:${a.ship_to_id}` : `wo:${a.works_order_id}`;
      return `<option value="${value}" data-max="${a.quantity}">${esc(label)} (${a.quantity} ${esc(item.unit)} available)</option>`;
    })
  ).join("");
  const body = `
    <div class="hint">${esc(item.part_number)} — ${esc(item.description || "")} · Total: <b>${item.total_quantity} ${esc(item.unit)}</b> (${fmtMoney(item.total_value)})</div>
    <div class="table-wrap" style="margin-top:10px;"><table class="data-table">
      <thead><tr><th>Allocated To</th><th class="num">Qty</th><th>Notes</th><th></th></tr></thead>
      <tbody>${rowsHtml || `<tr><td colspan="4"><div class="empty-state">Not allocated anywhere yet.</div></td></tr>`}</tbody>
    </table></div>
    ${canWrite("inventory") ? `
    <h3 style="margin-top:16px;font-size:13px;color:var(--navy);">Move Stock</h3>
    <div class="hint">Receive new stock, issue it to a job, or transfer it between sites — pick where it's coming from and where it's going.</div>
    <div class="form-row" style="margin-top:8px;"><label>From</label>
      <select id="mv-source">${sourceOptions}</select>
    </div>
    <div class="form-row"><label>To</label>
      <select id="mv-dest-type">
        <option value="site">A Site</option>
        <option value="wo">A Works Order</option>
      </select>
    </div>
    <div class="form-row" id="mv-site-row"><label>Site</label>
      <select id="mv-site">${shipToAddresses.map(a => `<option value="${a.ship_to_id}">${esc(a.label)}</option>`).join("")}</select>
    </div>
    <div class="form-row" id="mv-wo-row" style="display:none;"><label>Works Order</label>
      <select id="mv-wo">${worksOrders.map(w => `<option value="${w.works_order_id}">${esc(w.job_number)} — ${esc(w.job_name)}</option>`).join("")}</select>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Quantity</label><input type="number" step="1" min="0" id="mv-qty"></div>
      <div class="form-row"><label>Notes</label><input id="mv-notes"></div>
    </div>
    <div class="form-error" id="mv-error"></div>
    <div class="modal-actions"><button class="btn" type="button" id="mv-submit">Move Stock</button></div>
    ` : ""}
  `;
  openModal(`Allocations — ${item.part_number}`, body, { showFooter: false });

  document.getElementById("mv-dest-type")?.addEventListener("change", (e) => {
    const isSite = e.target.value === "site";
    document.getElementById("mv-site-row").style.display = isSite ? "" : "none";
    document.getElementById("mv-wo-row").style.display = isSite ? "none" : "";
  });
  document.getElementById("mv-submit")?.addEventListener("click", async () => {
    const errEl = document.getElementById("mv-error");
    errEl.textContent = "";
    const sourceVal = document.getElementById("mv-source").value;
    const isSiteDest = document.getElementById("mv-dest-type").value === "site";
    const payload = {
      quantity: Number(document.getElementById("mv-qty").value) || 0,
      notes: document.getElementById("mv-notes").value,
      from_ship_to_id: sourceVal.startsWith("site:") ? Number(sourceVal.split(":")[1]) : null,
      from_works_order_id: sourceVal.startsWith("wo:") ? Number(sourceVal.split(":")[1]) : null,
      to_ship_to_id: isSiteDest ? Number(document.getElementById("mv-site").value) : null,
      to_works_order_id: isSiteDest ? null : Number(document.getElementById("mv-wo").value),
    };
    try {
      await api(`/api/stock-items/${item.stock_item_id}/transfer`, { method: "POST", body: payload });
      const fresh = await api(`/api/stock-items/${item.stock_item_id}`);
      openAllocationsModal(fresh);
      renderInventory();
    } catch (err) {
      errEl.textContent = err.message;
    }
  });
  document.querySelectorAll("[data-remove-alloc]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("Remove this allocation?")) return;
    await api("/api/stock-allocations/" + b.dataset.removeAlloc, { method: "DELETE" });
    const fresh = await api(`/api/stock-items/${item.stock_item_id}`);
    openAllocationsModal(fresh);
    renderInventory();
  }));
}

/* ======================================================================
   LABOUR ENTRIES
   ====================================================================== */
let labourViewState = "entries";
let leState = { workOrder: "", employee: "", customer: "", contractCode: "" };

async function renderLabourEntries() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading hours…</div>`;

  const toggleHtml = `
    <div class="toggle-group">
      <button class="toggle-btn ${labourViewState === "entries" ? "active" : ""}" id="le-view-entries">Entries</button>
      <button class="toggle-btn ${labourViewState === "comparison" ? "active" : ""}" id="le-view-comparison">Quoted vs Booked</button>
    </div>`;

  if (labourViewState === "comparison") {
    const rows = await api("/api/labour-comparison");
    root.innerHTML = `
      <div class="toolbar">
        <div class="filters">${toggleHtml}</div>
      </div>
      <div class="hint">Quoted hours come from any Quote linked to a Works Order (see a Quote's Edit form). A Works Order with no linked Quote won't appear here, even if hours have been booked against it.</div>
      <div class="table-wrap"><table class="data-table">
        <thead><tr><th>Works Order</th><th class="num">Quoted Hours</th><th class="num">Booked Hours</th><th class="num">Variance</th><th class="num">Quoted Cost</th><th class="num">Booked Cost</th></tr></thead>
        <tbody>
          ${rows.length ? rows.map(r => `
            <tr>
              <td><b>${esc(r.job_number)}</b> — ${esc(r.job_name)}</td>
              <td class="num">${r.quoted_hours}</td>
              <td class="num">${r.booked_hours}</td>
              <td class="num">${r.variance_hours < 0 ? lightBadge("red", r.variance_hours) : r.variance_hours}</td>
              <td class="num">${fmtMoney(r.quoted_cost)}</td>
              <td class="num">${fmtMoney(r.booked_cost)}</td>
            </tr>`).join("") : `<tr><td colspan="6"><div class="empty-state">No Works Orders with a linked Quote or booked hours yet.</div></td></tr>`}
        </tbody>
      </table></div>
    `;
    document.getElementById("le-view-entries").addEventListener("click", () => { labourViewState = "entries"; renderLabourEntries(); });
    document.getElementById("le-view-comparison").addEventListener("click", () => { labourViewState = "comparison"; renderLabourEntries(); });
    return;
  }

  const allRows = await api("/api/labour-entries");
  let rows = allRows;
  if (leState.workOrder) rows = rows.filter(r => r.job_number === leState.workOrder);
  if (leState.employee) rows = rows.filter(r => r.employee_name === leState.employee);
  if (leState.customer) rows = rows.filter(r => customerNameForJobNumber(r.job_number) === leState.customer);
  if (leState.contractCode) rows = rows.filter(r => (woRefForJobNumber(r.job_number)?.contract_code || "") === leState.contractCode);

  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const woOptions = (state.ref.works_orders || []).map(w => w.job_number).sort();
  const employeeOptions = [...new Set(allRows.map(r => r.employee_name).filter(Boolean))].sort();
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();

  root.innerHTML = `
    <div class="toolbar">
      <div class="filters">
        ${toggleHtml}
        <select id="le-wo-filter">
          <option value="">All works orders</option>
          ${woOptions.map(j => `<option value="${esc(j)}" ${leState.workOrder === j ? "selected" : ""}>${esc(j)}</option>`).join("")}
        </select>
        <select id="le-employee-filter">
          <option value="">All employees</option>
          ${employeeOptions.map(n => `<option value="${esc(n)}" ${leState.employee === n ? "selected" : ""}>${esc(n)}</option>`).join("")}
        </select>
        <select id="le-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${leState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="le-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${leState.contractCode === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("labour_entries") ? `<button class="btn" id="le-new">+ New Booked Hours</button>` : ""}
    </div>
    <div class="hint">Time booked against jobs. Labour cost = hours × rate, calculated automatically. · ${rows.length.toLocaleString()} entr${rows.length === 1 ? "y" : "ies"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Works Order</th><th>Employee</th><th>Date</th><th class="num">Hours</th><th class="num">Rate</th><th class="num">Labour Cost</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td>${esc(r.job_number)}</td>
            <td>${esc(r.employee_name)}</td>
            <td>${fmtDate(r.work_date)}</td>
            <td class="num">${r.hours_worked}</td>
            <td class="num">${fmtMoney(r.hourly_rate)}</td>
            <td class="num"><b>${fmtMoney(r.labour_cost)}</b></td>
            <td class="actions-cell">
              <button class="btn small secondary" data-history="${r.labour_entry_id}">History</button>
              ${canWrite("labour_entries") ? `<button class="btn small secondary" data-edit="${r.labour_entry_id}">Edit</button>` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.labour_entry_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="7"><div class="empty-state">No booked hours found.</div></td></tr>`}
      </tbody>
    </table></div>
  `;
  document.getElementById("le-view-entries").addEventListener("click", () => { labourViewState = "entries"; renderLabourEntries(); });
  document.getElementById("le-view-comparison").addEventListener("click", () => { labourViewState = "comparison"; renderLabourEntries(); });
  document.getElementById("le-wo-filter").addEventListener("change", e => { leState.workOrder = e.target.value; renderLabourEntries(); });
  document.getElementById("le-employee-filter").addEventListener("change", e => { leState.employee = e.target.value; renderLabourEntries(); });
  document.getElementById("le-customer-filter").addEventListener("change", e => { leState.customer = e.target.value; renderLabourEntries(); });
  document.getElementById("le-code-filter").addEventListener("change", e => { leState.contractCode = e.target.value; renderLabourEntries(); });
  document.getElementById("le-new")?.addEventListener("click", () => openLabourForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const le = rows.find(r => String(r.labour_entry_id) === b.dataset.edit);
    openLabourForm(le);
  }));
  root.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    const le = rows.find(r => String(r.labour_entry_id) === b.dataset.history);
    showAuditHistory("labour_entries", le.labour_entry_id, `${le.employee_name} — ${fmtDate(le.work_date)}`);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this booked hours entry?", "/api/labour-entries/" + b.dataset.del, renderLabourEntries)
  ));
}

function openLabourForm(le = null) {
  const wos = state.ref.works_orders;
  const body = `
    <div class="form-row"><label>Works Order (Job)</label>
      <select name="works_order_id" id="le-works-order" required>${wos.map(w => `<option value="${w.works_order_id}" ${le?.works_order_id === w.works_order_id ? "selected" : ""}>${esc(w.job_number)} — ${esc(w.job_name)}</option>`).join("")}</select>
    </div>
    <div class="hint" id="le-recipe-hint" style="margin-top:-8px;margin-bottom:8px;display:none;"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Employee Name</label><input name="employee_name" value="${esc(le?.employee_name || "")}" required></div>
      <div class="form-row"><label>Date</label><input type="date" name="work_date" value="${fmtDateInput(le?.work_date)}" required></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Hours Worked</label><input type="number" step="0.1" id="le-hours" name="hours_worked" value="${le?.hours_worked ?? ""}" required></div>
      <div class="form-row"><label>Hourly Rate (£)</label><input type="number" step="0.01" id="le-rate" name="hourly_rate" value="${le?.hourly_rate ?? ""}" required></div>
    </div>
    <div class="form-row"><label>Labour Cost (auto-calculated)</label><input type="text" id="le-cost-preview" value="${fmtMoney((le?.hours_worked || 0) * (le?.hourly_rate || 0))}" disabled></div>
    ${auditHint(le)}
  `;
  const form = openModal(le ? "Edit Booked Hours" : "New Booked Hours", body, {
    submitLabel: le ? "Save Changes" : "Save Entry",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      payload.works_order_id = Number(payload.works_order_id);
      if (le) await api("/api/labour-entries/" + le.labour_entry_id, { method: "PUT", body: payload });
      else await api("/api/labour-entries", { method: "POST", body: payload });
      closeModal();
      renderLabourEntries();
    },
  });
  const updateWoRecipeHint = () => {
    const wo = wos.find(w => String(w.works_order_id) === form.querySelector("#le-works-order").value);
    const el = form.querySelector("#le-recipe-hint");
    const recipe = wo && FS_TIER_RECIPES[wo.contract_code];
    if (recipe) {
      el.textContent = "This job is " + wo.contract_code + " (fixed-price). Standard recipe: " + recipe;
      el.style.display = "";
    } else {
      el.style.display = "none";
    }
  };
  form.querySelector("#le-works-order").addEventListener("change", updateWoRecipeHint);
  updateWoRecipeHint();
  const updatePreview = () => {
    const h = Number(form.querySelector("#le-hours").value) || 0;
    const r = Number(form.querySelector("#le-rate").value) || 0;
    form.querySelector("#le-cost-preview").value = fmtMoney(h * r);
  };
  form.querySelector("#le-hours").addEventListener("input", updatePreview);
  form.querySelector("#le-rate").addEventListener("input", updatePreview);
}

/* ======================================================================
   SUPPLIER INVOICES
   ====================================================================== */
let siState = { search: "", workOrder: "", supplier: "", status: "", customer: "", contractCode: "" };

async function renderSupplierInvoices() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading supplier invoices…</div>`;
  const allRows = await api("/api/supplier-invoices");

  let rows = allRows;
  if (siState.search) {
    const q = siState.search.toLowerCase();
    rows = rows.filter(r =>
      r.invoice_number.toLowerCase().includes(q) ||
      r.job_number.toLowerCase().includes(q) ||
      r.po_number.toLowerCase().includes(q) ||
      r.supplier_name.toLowerCase().includes(q)
    );
  }
  if (siState.workOrder) rows = rows.filter(r => r.job_number === siState.workOrder);
  if (siState.supplier) rows = rows.filter(r => r.supplier_name === siState.supplier);
  if (siState.status) rows = rows.filter(r => r.status === siState.status);
  if (siState.customer) rows = rows.filter(r => customerNameForJobNumber(r.job_number) === siState.customer);
  if (siState.contractCode) rows = rows.filter(r => (woRefForJobNumber(r.job_number)?.contract_code || "") === siState.contractCode);

  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const woOptions = (state.ref.works_orders || []).map(w => w.job_number).sort();
  const supplierOptions = (state.ref.suppliers || []).map(s => s.supplier_name).sort();
  const statusOptions = ["Pending", "Approved", "Paid", "Disputed", "Cancelled"];
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();

  root.innerHTML = `
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="si-search" placeholder="Search invoice #, job, PO, supplier…" value="${esc(siState.search)}">
        <select id="si-wo-filter">
          <option value="">All works orders</option>
          ${woOptions.map(j => `<option value="${esc(j)}" ${siState.workOrder === j ? "selected" : ""}>${esc(j)}</option>`).join("")}
        </select>
        <select id="si-supplier-filter">
          <option value="">All suppliers</option>
          ${supplierOptions.map(s => `<option value="${esc(s)}" ${siState.supplier === s ? "selected" : ""}>${esc(s)}</option>`).join("")}
        </select>
        <select id="si-status-filter">
          <option value="">All statuses</option>
          ${statusOptions.map(s => `<option value="${s}" ${siState.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <select id="si-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${siState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="si-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${siState.contractCode === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("supplier_invoices") ? `<button class="btn" id="si-new">+ New Supplier Invoice</button>` : ""}
    </div>
    <div class="hint">Supplier invoices, validated against Purchase Order value. · ${rows.length.toLocaleString()} invoice${rows.length === 1 ? "" : "s"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Invoice #</th><th>Job</th><th>PO Number</th><th>Supplier</th><th>Date</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.invoice_number)}</b></td>
            <td>${esc(r.job_number)}</td>
            <td>${esc(r.po_number)}</td>
            <td>${esc(r.supplier_name)}</td>
            <td>${fmtDate(r.invoice_date)}</td>
            <td class="num">${r.invoice_amount > r.po_value ? lightBadge("red", fmtMoney(r.invoice_amount)) : fmtMoney(r.invoice_amount)}</td>
            <td>${statusBadge(r.status)}</td>
            <td class="actions-cell">
              <button class="btn small secondary" data-history="${r.supplier_invoice_id}">History</button>
              ${canWrite("supplier_invoices") ? `<button class="btn small secondary" data-edit="${r.supplier_invoice_id}">Edit</button>` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.supplier_invoice_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="8"><div class="empty-state">No supplier invoices found.</div></td></tr>`}
      </tbody>
    </table></div>
  `;
  document.getElementById("si-search").addEventListener("input", debounce(e => { siState.search = e.target.value; debouncedSearchRerender(e.target, renderSupplierInvoices); }, 500));
  document.getElementById("si-wo-filter").addEventListener("change", e => { siState.workOrder = e.target.value; renderSupplierInvoices(); });
  document.getElementById("si-supplier-filter").addEventListener("change", e => { siState.supplier = e.target.value; renderSupplierInvoices(); });
  document.getElementById("si-status-filter").addEventListener("change", e => { siState.status = e.target.value; renderSupplierInvoices(); });
  document.getElementById("si-customer-filter").addEventListener("change", e => { siState.customer = e.target.value; renderSupplierInvoices(); });
  document.getElementById("si-code-filter").addEventListener("change", e => { siState.contractCode = e.target.value; renderSupplierInvoices(); });
  document.getElementById("si-new")?.addEventListener("click", () => openSupplierInvoiceForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const si = rows.find(r => String(r.supplier_invoice_id) === b.dataset.edit);
    openSupplierInvoiceForm(si);
  }));
  root.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    const si = rows.find(r => String(r.supplier_invoice_id) === b.dataset.history);
    showAuditHistory("supplier_invoices", si.supplier_invoice_id, si.invoice_number);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this supplier invoice?", "/api/supplier-invoices/" + b.dataset.del, renderSupplierInvoices)
  ));
}

function openSupplierInvoiceForm(si = null) {
  const pos = state.ref.purchase_orders;
  const body = `
    <div class="form-row"><label>Purchase Order</label>
      <select name="purchase_order_id" id="si-po" ${si ? "disabled" : "required"}>
        ${pos.map(p => `<option value="${p.purchase_order_id}" data-value="${p.po_value}" ${si?.purchase_order_id === p.purchase_order_id ? "selected" : ""}>${esc(p.po_number)} (${fmtMoney(p.po_value)})</option>`).join("")}
      </select>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Invoice Number</label><input name="invoice_number" value="${esc(si?.invoice_number || "")}" required></div>
      <div class="form-row"><label>Invoice Date</label><input type="date" name="invoice_date" value="${fmtDateInput(si?.invoice_date)}" required></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Invoice Amount (£)</label><input type="number" step="0.01" id="si-amount" name="invoice_amount" value="${si?.invoice_amount ?? ""}" required></div>
      <div class="form-row"><label>Status</label>
        <select name="status">${["Pending", "Approved", "Paid", "Disputed", "Cancelled"].map(s => `<option ${si?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    <div class="form-warning" id="si-warning" style="display:none"></div>
    ${auditHint(si)}
  `;
  const form = openModal(si ? "Edit Supplier Invoice" : "New Supplier Invoice", body, {
    submitLabel: si ? "Save Changes" : "Save Invoice",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      let result;
      if (si) {
        result = await api("/api/supplier-invoices/" + si.supplier_invoice_id, { method: "PUT", body: payload });
      } else {
        payload.purchase_order_id = Number(payload.purchase_order_id);
        result = await api("/api/supplier-invoices", { method: "POST", body: payload });
      }
      closeModal();
      renderSupplierInvoices();
      if (result.warning) setTimeout(() => alert("Warning: " + result.warning), 200);
    },
  });
  const checkAgainstPO = () => {
    const sel = form.querySelector("#si-po");
    const poValue = Number(sel.selectedOptions[0]?.dataset.value || 0);
    const amount = Number(form.querySelector("#si-amount").value) || 0;
    const warn = form.querySelector("#si-warning");
    if (amount > poValue && poValue > 0) {
      warn.style.display = "block";
      warn.textContent = `This amount exceeds the PO value of ${fmtMoney(poValue)} — approval will be required.`;
    } else warn.style.display = "none";
  };
  form.querySelector("#si-po")?.addEventListener("change", checkAgainstPO);
  form.querySelector("#si-amount").addEventListener("input", checkAgainstPO);
}

/* ======================================================================
   CUSTOMER INVOICES
   ====================================================================== */
let ciState = { search: "", workOrder: "", status: "", customer: "", contractCode: "" };

async function renderCustomerInvoices() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading customer invoices…</div>`;
  const allRows = await api("/api/customer-invoices");

  let rows = allRows;
  if (ciState.search) {
    const q = ciState.search.toLowerCase();
    rows = rows.filter(r => r.invoice_number.toLowerCase().includes(q) || r.job_number.toLowerCase().includes(q));
  }
  if (ciState.workOrder) rows = rows.filter(r => r.job_number === ciState.workOrder);
  if (ciState.status) rows = rows.filter(r => r.status === ciState.status);
  if (ciState.customer) rows = rows.filter(r => customerNameForJobNumber(r.job_number) === ciState.customer);
  if (ciState.contractCode) rows = rows.filter(r => (woRefForJobNumber(r.job_number)?.contract_code || "") === ciState.contractCode);

  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const woOptions = (state.ref.works_orders || []).map(w => w.job_number).sort();
  const statusOptions = ["Draft", "Sent", "Paid", "Overdue", "Cancelled"];
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();

  root.innerHTML = `
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="ci-search" placeholder="Search invoice # or job…" value="${esc(ciState.search)}">
        <select id="ci-wo-filter">
          <option value="">All works orders</option>
          ${woOptions.map(j => `<option value="${esc(j)}" ${ciState.workOrder === j ? "selected" : ""}>${esc(j)}</option>`).join("")}
        </select>
        <select id="ci-status-filter">
          <option value="">All statuses</option>
          ${statusOptions.map(s => `<option value="${s}" ${ciState.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <select id="ci-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${ciState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="ci-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${ciState.contractCode === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("customer_invoices") ? `<button class="btn" id="ci-new">+ New Customer Invoice</button>` : ""}
    </div>
    <div class="hint">Customer invoices raised against Works Orders (drives Revenue). · ${rows.length.toLocaleString()} invoice${rows.length === 1 ? "" : "s"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Invoice #</th><th>Job</th><th>Date</th><th class="num">Amount</th><th>Status</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.invoice_number)}</b></td>
            <td>${esc(r.job_number)}</td>
            <td>${fmtDate(r.invoice_date)}</td>
            <td class="num">${fmtMoney(r.invoice_amount)}</td>
            <td>${statusBadge(r.status)}</td>
            <td class="actions-cell">
              <button class="btn small secondary" data-history="${r.customer_invoice_id}">History</button>
              ${canWrite("customer_invoices") ? `<button class="btn small secondary" data-edit="${r.customer_invoice_id}">Edit</button>` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.customer_invoice_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="6"><div class="empty-state">No customer invoices found.</div></td></tr>`}
      </tbody>
    </table></div>
  `;
  document.getElementById("ci-search").addEventListener("input", debounce(e => { ciState.search = e.target.value; debouncedSearchRerender(e.target, renderCustomerInvoices); }, 500));
  document.getElementById("ci-wo-filter").addEventListener("change", e => { ciState.workOrder = e.target.value; renderCustomerInvoices(); });
  document.getElementById("ci-status-filter").addEventListener("change", e => { ciState.status = e.target.value; renderCustomerInvoices(); });
  document.getElementById("ci-customer-filter").addEventListener("change", e => { ciState.customer = e.target.value; renderCustomerInvoices(); });
  document.getElementById("ci-code-filter").addEventListener("change", e => { ciState.contractCode = e.target.value; renderCustomerInvoices(); });
  document.getElementById("ci-new")?.addEventListener("click", () => openCustomerInvoiceForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    const ci = rows.find(r => String(r.customer_invoice_id) === b.dataset.edit);
    openCustomerInvoiceForm(ci);
  }));
  root.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    const ci = rows.find(r => String(r.customer_invoice_id) === b.dataset.history);
    showAuditHistory("customer_invoices", ci.customer_invoice_id, ci.invoice_number);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this customer invoice?", "/api/customer-invoices/" + b.dataset.del, renderCustomerInvoices)
  ));
}

function openCustomerInvoiceForm(ci = null) {
  const wos = state.ref.works_orders;
  const body = `
    <div class="form-row"><label>Works Order (Job)</label>
      <select name="works_order_id" required>${wos.map(w => `<option value="${w.works_order_id}" ${ci?.works_order_id === w.works_order_id ? "selected" : ""}>${esc(w.job_number)} — ${esc(w.job_name)}</option>`).join("")}</select>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Invoice Number</label><input name="invoice_number" value="${esc(ci?.invoice_number || "")}" required></div>
      <div class="form-row"><label>Invoice Date</label><input type="date" name="invoice_date" value="${fmtDateInput(ci?.invoice_date)}" required></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Invoice Amount (£)</label><input type="number" step="0.01" name="invoice_amount" value="${ci?.invoice_amount ?? ""}" required></div>
      <div class="form-row"><label>Status</label>
        <select name="status">${["Draft", "Sent", "Paid", "Overdue", "Cancelled"].map(s => `<option ${ci?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    ${auditHint(ci)}
  `;
  openModal(ci ? "Edit Customer Invoice" : "New Customer Invoice", body, {
    submitLabel: ci ? "Save Changes" : "Save Invoice",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      payload.works_order_id = Number(payload.works_order_id);
      if (ci) await api("/api/customer-invoices/" + ci.customer_invoice_id, { method: "PUT", body: payload });
      else await api("/api/customer-invoices", { method: "POST", body: payload });
      closeModal();
      renderCustomerInvoices();
    },
  });
}

/* ======================================================================
   QUOTES
   ====================================================================== */
/* ----------------------------------------------------------------------
   Summary panels at the top of each screen. Figures come from one call to
   /api/screen-summaries; cards with a data-sum-action click through to the
   matching filtered list (handled once, by the delegated listener below).
   ---------------------------------------------------------------------- */
async function loadScreenSummary() {
  try { return await api("/api/screen-summaries"); } catch (e) { return null; }
}
function summaryPanelHtml(cards) {
  const cols = Math.min(cards.length, 3);
  return `<div class="kpi-grid summary-panels" style="grid-template-columns:repeat(${cols},1fr);${cards.length === 1 ? "max-width:320px;" : ""}">${cards.map(c => {
    const tag = c.action ? "button" : "div";
    return `<${tag} class="kpi-card${c.action ? " clickable" : ""}"${c.action ? ` type="button" data-sum-action="${c.action}"` : ""}>
      <div class="label">${c.label}</div><div class="value">${c.value}</div>${c.note ? `<div class="hint" style="margin:2px 0 0;">${c.note}</div>` : ""}</${tag}>`;
  }).join("")}</div>`;
}
const sumMoney = v => (v === null || v === undefined) ? "" : fmtMoney(v);
const sumNote = (text, v) => (v === null || v === undefined) ? text : `${text} · value ${fmtMoney(v)}`;
const n0 = v => (v || 0).toLocaleString();

function quotesSummaryHtml(sm) {
  if (!sm || !sm.quotes) return "";
  const q = sm.quotes;
  return summaryPanelHtml([
    { label: "Quotes awaiting authorisation", value: n0(q.awaiting_count), note: `Status Sent · value ${fmtMoney(q.awaiting_value)}`, action: "quotes-sent" },
    { label: "Customer purchase order received", value: n0(q.po_received_count), note: `Status Won, ${sm.year} · value ${fmtMoney(q.po_received_value)}`, action: "quotes-won" },
  ]);
}
function worksOrdersSummaryHtml(sm) {
  if (!sm || !sm.works_orders) return "";
  const w = sm.works_orders;
  return summaryPanelHtml([
    { label: "Open works orders", value: n0(w.open_count), note: sumNote("Open and In Progress", w.open_value), action: "wo-open" },
    { label: "Completed year to date", value: n0(w.completed_ytd_count), note: sumNote(`End Date in ${sm.year}`, w.completed_ytd_value), action: "wo-done" },
  ]);
}
function purchaseOrdersSummaryHtml(sm) {
  if (!sm || !sm.purchase_orders) return "";
  const p = sm.purchase_orders;
  return summaryPanelHtml([
    { label: "Open POs awaiting goods", value: n0(p.awaiting_count), note: sumNote("Issued, nothing booked in yet", p.awaiting_value) },
    { label: "Part received", value: n0(p.part_received_count), note: "Partial deliveries still outstanding" },
  ]);
}
function goodsInSummaryHtml(sm) {
  if (!sm || !sm.goods_in) return "";
  const g = sm.goods_in;
  return summaryPanelHtml([
    { label: "Total goods received", value: n0(g.total), note: "All receipts" },
    { label: "This year to date", value: n0(g.ytd) },
    { label: "This month", value: n0(g.month) },
  ]);
}
function despatchSummaryHtml(sm) {
  if (!sm || !sm.despatch_notes) return "";
  const d = sm.despatch_notes;
  return summaryPanelHtml([
    { label: "Total jobs complete", value: n0(d.jobs_complete), note: "Jobs with a Despatched note" },
    { label: "This year to date", value: n0(d.ytd) },
    { label: "This month", value: n0(d.month) },
  ]);
}
function totalSummaryHtml(label, count) {
  return count === undefined ? "" : summaryPanelHtml([{ label, value: n0(count) }]);
}
document.getElementById("view-root")?.addEventListener("click", e => {
  const el = e.target.closest("[data-sum-action]");
  if (!el) return;
  switch (el.dataset.sumAction) {
    case "quotes-sent": quoteState.status = "Sent"; quoteState.search = ""; renderQuotes(); break;
    case "quotes-won": quoteState.status = "Won"; quoteState.search = ""; renderQuotes(); break;
    case "wo-open": woState.status = "Active"; woState.late = false; woState.endYear = ""; woState.dueMonth = ""; woState.search = ""; woState.page = 1; renderWorksOrders(); break;
    case "wo-done": woState.status = "Completed"; woState.late = false; woState.endYear = String(new Date().getFullYear()); woState.dueMonth = ""; woState.search = ""; woState.page = 1; renderWorksOrders(); break;
  }
});

let quoteState = { search: "", status: "", customer: "", owner: "" };

async function renderQuotes() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading quotes…</div>`;
  const rows = await api("/api/quotes");
  const sm = await loadScreenSummary();
  let filtered = rows;
  if (quoteState.search) {
    const q = quoteState.search.toLowerCase();
    filtered = filtered.filter(r =>
      r.quote_number.toLowerCase().includes(q) ||
      (r.customer_name || "").toLowerCase().includes(q) ||
      (r.description || "").toLowerCase().includes(q)
    );
  }
  if (quoteState.status) filtered = filtered.filter(r => r.status === quoteState.status);
  if (quoteState.customer) filtered = filtered.filter(r => r.customer_name === quoteState.customer);
  if (quoteState.owner) filtered = filtered.filter(r => r.owner === quoteState.owner);

  const statusOptions = ["Draft", "Sent", "On Hold", "Won", "Lost", "Expired", "Cancelled"];
  // Dropdown option lists come from the full reference/unfiltered dataset,
  // so picking one filter never shrinks what another dropdown offers.
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const ownerOptions = [...new Set(rows.map(r => r.owner).filter(Boolean))].sort();

  root.innerHTML = quotesSummaryHtml(sm) + `
    <div class="readonly-note">Quote numbers are assigned automatically and sequentially (e.g. <b>TMQ-3000</b>, then <b>TMQ-3001</b>...).
      In full deployment, creating a quote here will also create the matching SharePoint folder — the same approach is planned for
      Works Order and Purchase Order numbering too.</div>
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="quote-search" placeholder="Search quote number, customer, description…" value="${esc(quoteState.search)}">
        <select id="quote-status-filter">
          <option value="">All statuses</option>
          ${statusOptions.map(s => `<option value="${s}" ${quoteState.status === s ? "selected" : ""}>${s}</option>`).join("")}
        </select>
        <select id="quote-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${quoteState.customer === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="quote-owner-filter">
          <option value="">All owners</option>
          ${ownerOptions.map(o => `<option value="${esc(o)}" ${quoteState.owner === o ? "selected" : ""}>${esc(o)}</option>`).join("")}
        </select>
      </div>
      ${canWrite("quotes") ? `<button class="btn" id="quote-new">+ New Quote</button>` : ""}
    </div>
    <div class="hint">${filtered.length.toLocaleString()} quote${filtered.length === 1 ? "" : "s"}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>
        <th>Quote Number</th>
        <th>Customer</th>
        <th>Description</th>
        <th>Owner</th>
        <th>Quote Date</th>
        <th class="num">Estimated Value</th>
        <th>Status</th>
        <th></th>
      </tr></thead>
      <tbody>
        ${filtered.length ? filtered.map(r => `
          <tr>
            <td><b>${esc(r.quote_number)}</b>${sharepointLink(r.sharepoint_url)}</td>
            <td>${esc(r.customer_name || "—")}</td>
            <td>${esc(r.description || "")}</td>
            <td>${esc(r.owner || "")}</td>
            <td>${fmtDate(r.quote_date)}</td>
            <td class="num">${fmtMoney(r.estimated_value)}</td>
            <td>${statusBadge(r.status)}${r.is_past_validity ? " " + lightBadge("amber", "Past validity") : ""}</td>
            <td class="actions-cell">
              <a class="btn small secondary" href="/api/quotes/${r.quote_id}/pdf" title="Download quote PDF">PDF</a>
              <button class="btn small secondary" data-breakdown="${r.quote_id}">Breakdown</button>
              <button class="btn small secondary" data-history="${r.quote_id}">History</button>
              ${canWrite("quotes") ? `<button class="btn small secondary" data-edit="${r.quote_id}">Edit</button>` : ""}
              ${isAdmin() ? `<button class="btn small danger" data-del="${r.quote_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="8"><div class="empty-state">No quotes yet. ${canWrite("quotes") ? "Create the first one above." : ""}</div></td></tr>`}
      </tbody>
    </table></div>
  `;

  document.getElementById("quote-search").addEventListener("input", debounce(e => { quoteState.search = e.target.value; debouncedSearchRerender(e.target, renderQuotes); }, 500));
  document.getElementById("quote-status-filter").addEventListener("change", e => { quoteState.status = e.target.value; renderQuotes(); });
  document.getElementById("quote-customer-filter").addEventListener("change", e => { quoteState.customer = e.target.value; renderQuotes(); });
  document.getElementById("quote-owner-filter").addEventListener("change", e => { quoteState.owner = e.target.value; renderQuotes(); });
  document.getElementById("quote-new")?.addEventListener("click", () => openQuoteWizard());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    openQuoteForm(rows.find(r => String(r.quote_id) === b.dataset.edit));
  }));
  root.querySelectorAll("[data-breakdown]").forEach(b => b.addEventListener("click", () => {
    viewQuoteBreakdown(rows.find(r => String(r.quote_id) === b.dataset.breakdown));
  }));
  root.querySelectorAll("[data-history]").forEach(b => b.addEventListener("click", () => {
    const q = rows.find(r => String(r.quote_id) === b.dataset.history);
    showAuditHistory("quotes", q.quote_id, q.quote_number);
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this quote?", "/api/quotes/" + b.dataset.del, renderQuotes)
  ));
}

async function openQuoteForm(q = null) {
  const customers = state.ref.customers;
  const statusOptions = ["Draft", "Sent", "On Hold", "Won", "Lost", "Expired", "Cancelled"];
  let nextNumberPreview = q ? q.quote_number : "…";
  if (!q) {
    try { nextNumberPreview = (await api("/api/quotes/next-number")).quote_number; } catch (e) { nextNumberPreview = "TMQ-????"; }
  }
  const body = `
    <div class="form-row"><label>Quote Number</label><input value="${esc(nextNumberPreview)}" disabled>
      ${!q ? `<div class="hint" style="margin:6px 0 0;">Assigned automatically when saved — the next available number.</div>` : ""}
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Customer</label>
        <select name="customer_id">
          <option value="">—</option>
          ${customers.map(c => `<option value="${c.customer_id}" ${q?.customer_id === c.customer_id ? "selected" : ""}>${esc(c.customer_name)}</option>`).join("")}
        </select>
      </div>
      <div class="form-row"><label>Owner</label><input name="owner" value="${esc(q?.owner || "")}"></div>
    </div>
    <div class="form-row"><label>Description</label><input name="description" value="${esc(q?.description || "")}"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Quote Date</label><input type="date" name="quote_date" value="${fmtDateInput(q?.quote_date)}"></div>
      <div class="form-row"><label>Valid Until</label><input type="date" name="valid_until" value="${fmtDateInput(q?.valid_until)}"></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Estimated Value (£)</label><input type="number" step="0.01" name="estimated_value" value="${q?.estimated_value ?? ""}"></div>
      <div class="form-row"><label>Status</label>
        <select name="status">${statusOptions.map(s => `<option ${q?.status === s ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>
    <div class="form-row"><label>Notes</label><textarea name="notes" rows="3">${esc(q?.notes || "")}</textarea></div>
    <div class="hint" style="margin-top:4px;">The fields below feed the customer-facing quote PDF.</div>
    <div class="form-grid-2">
      <div class="form-row"><label>Contact Name</label><input name="contact_name" value="${esc(q?.contact_name || "")}"></div>
      <div class="form-row"><label>Your Ref (customer's reference)</label><input name="your_ref" value="${esc(q?.your_ref || "")}"></div>
    </div>
    <div class="form-row"><label>Works Order Ref</label><input name="works_order_ref" value="${esc(q?.works_order_ref || "")}" placeholder="e.g. TM-9098, if known"></div>
    <div class="form-row"><label>Linked Works Order (for Quoted vs Booked tracking)</label>
      <select name="works_order_id">
        <option value="">— none yet —</option>
        ${state.ref.works_orders.map(w => `<option value="${w.works_order_id}" ${q?.works_order_id === w.works_order_id ? "selected" : ""}>${esc(w.job_number)} — ${esc(w.job_name)}</option>`).join("")}
      </select>
      <div class="hint" style="margin-top:6px;">Optional, separate from Works Order Ref above (which is just what prints on the PDF). Link the actual Works Order once one exists, so this quote's hours show up on the Hours Quoted &amp; Booked page for comparison against what's actually been booked.</div>
    </div>
    <div class="form-row"><label>Scope Items (one per line)</label><textarea name="scope_items" rows="3">${esc(q?.scope_items || "")}</textarea></div>
    <div class="form-row"><label>Delivery Terms</label><input name="delivery_terms" value="${esc(q?.delivery_terms || "")}" placeholder="Invoiced in Arrears."></div>
    <div class="form-row"><label>SharePoint Folder URL</label><input type="url" name="sharepoint_url" placeholder="https://…" value="${esc(q?.sharepoint_url || "")}"></div>
    ${auditHint(q)}
  `;
  openModal(q ? `Edit Quote — ${q.quote_number}` : "New Quote", body, {
    submitLabel: q ? "Save Changes" : "Create Quote",
    onSubmit: async (fd) => {
      if (!confirm("Have all purchases and hours been recorded for this quote?")) return;
      const payload = Object.fromEntries(fd.entries());
      payload.customer_id = payload.customer_id ? Number(payload.customer_id) : null;
      payload.works_order_id = payload.works_order_id ? Number(payload.works_order_id) : null;
      if (q) await api("/api/quotes/" + q.quote_id, { method: "PUT", body: payload });
      else await api("/api/quotes", { method: "POST", body: payload });
      closeModal();
      renderQuotes();
    },
  });
}

/* ---------------------------------------------------------------------
   NEW QUOTE WIZARD — labour cost buildup + purchases, from the business's
   own Master_Repair_Quote_Cost_Tracker.xlsx. Custom multi-step modal
   (openModal() only handles single-step forms) sharing the same overlay
   styling. State lives in quoteWizard for the life of the wizard.
   --------------------------------------------------------------------- */
let quoteWizard = null;

async function openQuoteWizard() {
  const [rateCard, marginSettings] = await Promise.all([
    api("/api/rate-card"),
    api("/api/settings/margin-targets"),
  ]);
  quoteWizard = {
    step: 1,
    customer_id: "", description: "", owner: "", quote_date: "", valid_until: "", notes: "",
    contact_name: "", your_ref: "", works_order_ref: "", scope_items: "", delivery_terms: "",
    labourLines: rateCard.map(r => ({ ...r, standard_hours: "", ot_hours: "" })),
    purchaseLines: [],
    orderValue: "",
    marginTarget: marginSettings.default_target ?? 15,
  };
  renderQuoteWizardModal();
}

function closeQuoteWizard() {
  quoteWizard = null;
  document.getElementById("modal-root").innerHTML = "";
}

function wizardLabourTotal() {
  return quoteWizard.labourLines.reduce((sum, l) =>
    sum + (Number(l.standard_hours) || 0) * l.standard_rate + (Number(l.ot_hours) || 0) * l.ot_rate, 0);
}
function wizardPurchaseTotal() {
  return quoteWizard.purchaseLines.reduce((sum, l) => sum + (Number(l.purchase_amount) || 0), 0);
}
function wizardSalesValueTotal() {
  return quoteWizard.purchaseLines.reduce((sum, l) => sum + (Number(l.sales_value) || 0), 0);
}

const WIZARD_STEP_LABELS = ["Quote Details", "Labour Costs", "Purchases", "Summary"];

function renderQuoteWizardModal() {
  const root = document.getElementById("modal-root");
  root.innerHTML = `
    <div class="modal-overlay" id="wizard-overlay">
      <div class="modal wide">
        <div class="wizard-step-label">Step ${quoteWizard.step} of 4 — ${WIZARD_STEP_LABELS[quoteWizard.step - 1]}</div>
        <h3 style="margin-top:2px;">New Quote</h3>
        <div class="wizard-steps">
          ${[1, 2, 3, 4].map(n => `<div class="wizard-step-dot ${n < quoteWizard.step ? "done" : n === quoteWizard.step ? "active" : ""}"></div>`).join("")}
        </div>
        <div id="wizard-body"></div>
        <div class="form-error" id="wizard-error"></div>
      </div>
    </div>`;
  document.getElementById("wizard-overlay").addEventListener("click", (e) => { if (e.target.id === "wizard-overlay") closeQuoteWizard(); });
  renderWizardStepBody();
}

function renderWizardStepBody() {
  const body = document.getElementById("wizard-body");
  const w = quoteWizard;
  if (w.step === 1) {
    const customers = state.ref.customers;
    body.innerHTML = `
      <div class="form-grid-2">
        <div class="form-row"><label>Customer</label>
          <select id="wz-customer">
            <option value="">—</option>
            ${customers.map(c => `<option value="${c.customer_id}" ${String(w.customer_id) === String(c.customer_id) ? "selected" : ""}>${esc(c.customer_name)}</option>`).join("")}
          </select>
        </div>
        <div class="form-row"><label>Owner</label><input id="wz-owner" value="${esc(w.owner)}"></div>
      </div>
      <div class="form-row"><label>Description</label><input id="wz-description" value="${esc(w.description)}"></div>
      <div class="form-grid-2">
        <div class="form-row"><label>Quote Date</label><input type="date" id="wz-quote_date" value="${esc(w.quote_date)}"></div>
        <div class="form-row"><label>Valid Until</label><input type="date" id="wz-valid_until" value="${esc(w.valid_until)}"></div>
      </div>
      <div class="form-row"><label>Notes</label><textarea id="wz-notes" rows="3">${esc(w.notes)}</textarea></div>

      <div class="hint" style="margin-top:4px;">The fields below feed the customer-facing quote PDF (Breakdown → PDF button once saved) — all optional, but the more filled in, the closer the PDF matches a finished quote letter.</div>
      <div class="form-grid-2">
        <div class="form-row"><label>Contact Name</label><input id="wz-contact_name" value="${esc(w.contact_name)}" placeholder="Auto-fills from customer if on file"></div>
        <div class="form-row"><label>Your Ref (customer's reference)</label><input id="wz-your_ref" value="${esc(w.your_ref)}"></div>
      </div>
      <div class="form-row"><label>Works Order Ref</label><input id="wz-works_order_ref" value="${esc(w.works_order_ref)}" placeholder="e.g. TM-9098, if known"></div>
      <div class="form-row"><label>Linked Works Order (for Quoted vs Booked tracking)</label>
        <select id="wz-works_order_id">
          <option value="">— none yet —</option>
          ${state.ref.works_orders.map(wo => `<option value="${wo.works_order_id}" ${String(w.works_order_id) === String(wo.works_order_id) ? "selected" : ""}>${esc(wo.job_number)} — ${esc(wo.job_name)}</option>`).join("")}
        </select>
        <div class="hint" style="margin-top:6px;">Optional — only if the actual Works Order already exists. Separate from Works Order Ref above, which is just what prints on the PDF.</div>
      </div>
      <div class="form-row"><label>Scope Items (one per line — shown as sub-bullets under the description)</label><textarea id="wz-scope_items" rows="3">${esc(w.scope_items)}</textarea></div>
      <div class="form-row"><label>Delivery Terms</label><input id="wz-delivery_terms" value="${esc(w.delivery_terms)}" placeholder="Invoiced in Arrears."></div>

      <div class="wizard-actions">
        <button class="btn secondary" id="wz-cancel" type="button">Cancel</button>
        <button class="btn" id="wz-next" type="button">Next: Labour Costs →</button>
      </div>`;
    document.getElementById("wz-cancel").addEventListener("click", closeQuoteWizard);
    document.getElementById("wz-customer").addEventListener("change", (e) => {
      if (w.contact_name) return; // don't clobber something the user already typed
      const c = customers.find(c => String(c.customer_id) === e.target.value);
      if (c?.contact_name) {
        document.getElementById("wz-contact_name").value = c.contact_name;
      }
    });
    document.getElementById("wz-next").addEventListener("click", () => {
      w.customer_id = document.getElementById("wz-customer").value;
      w.owner = document.getElementById("wz-owner").value;
      w.description = document.getElementById("wz-description").value;
      w.quote_date = document.getElementById("wz-quote_date").value;
      w.valid_until = document.getElementById("wz-valid_until").value;
      w.notes = document.getElementById("wz-notes").value;
      w.contact_name = document.getElementById("wz-contact_name").value;
      w.your_ref = document.getElementById("wz-your_ref").value;
      w.works_order_ref = document.getElementById("wz-works_order_ref").value;
      w.works_order_id = document.getElementById("wz-works_order_id").value || null;
      w.scope_items = document.getElementById("wz-scope_items").value;
      w.delivery_terms = document.getElementById("wz-delivery_terms").value;
      w.step = 2;
      renderQuoteWizardModal();
    });
    return;
  }

  if (w.step === 2) {
    const categories = [...new Set(w.labourLines.map(l => l.category))];
    let tables = "";
    categories.forEach(cat => {
      tables += `<div class="wizard-cat-heading">${esc(cat)}</div>
        <table class="wizard-table"><thead><tr>
          <th>Activity</th><th class="num">Std Rate</th><th class="num">Std Hrs</th><th class="num">OT Rate</th><th class="num">OT Hrs</th><th class="num">Total</th>
        </tr></thead><tbody>`;
      w.labourLines.forEach((line, idx) => {
        if (line.category !== cat) return;
        const total = (Number(line.standard_hours) || 0) * line.standard_rate + (Number(line.ot_hours) || 0) * line.ot_rate;
        tables += `<tr>
          <td>${esc(line.activity)}</td>
          <td class="num">${fmtMoney(line.standard_rate)}</td>
          <td class="num"><input type="number" step="0.5" min="0" data-idx="${idx}" data-field="standard_hours" value="${line.standard_hours}"></td>
          <td class="num">${fmtMoney(line.ot_rate)}</td>
          <td class="num"><input type="number" step="0.5" min="0" data-idx="${idx}" data-field="ot_hours" value="${line.ot_hours}"></td>
          <td class="num" id="wz-row-total-${idx}">${fmtMoney(total)}</td>
        </tr>`;
      });
      tables += `</tbody></table>`;
    });
    body.innerHTML = `
      <div class="hint">Enter hours only for the activities that apply — everything else can stay blank.</div>
      <div style="max-height:360px;overflow-y:auto;">${tables}</div>
      <div class="wizard-totals"><span>Total Labour Cost: <b id="wz-labour-total">${fmtMoney(wizardLabourTotal())}</b></span></div>
      <div class="wizard-actions">
        <button class="btn secondary" id="wz-back" type="button">← Back</button>
        <button class="btn" id="wz-next" type="button">Next: Purchases →</button>
      </div>`;
    body.querySelectorAll("input[data-idx]").forEach(inp => inp.addEventListener("input", () => {
      const idx = Number(inp.dataset.idx), field = inp.dataset.field;
      w.labourLines[idx][field] = inp.value;
      const line = w.labourLines[idx];
      const total = (Number(line.standard_hours) || 0) * line.standard_rate + (Number(line.ot_hours) || 0) * line.ot_rate;
      document.getElementById(`wz-row-total-${idx}`).textContent = fmtMoney(total);
      document.getElementById("wz-labour-total").textContent = fmtMoney(wizardLabourTotal());
    }));
    document.getElementById("wz-back").addEventListener("click", () => { w.step = 1; renderQuoteWizardModal(); });
    document.getElementById("wz-next").addEventListener("click", () => { w.step = 3; renderQuoteWizardModal(); });
    return;
  }

  if (w.step === 3) {
    renderWizardPurchasesStep(body);
    return;
  }

  // Step 4 - Summary
  const labourTotal = wizardLabourTotal();
  const purchaseTotal = wizardPurchaseTotal();
  const salesValueTotal = wizardSalesValueTotal();
  const totalCost = labourTotal + purchaseTotal;
  const suggestedPrice = labourTotal + salesValueTotal; // labour at cost + purchases at their priced-to-customer value
  const orderValue = Number(w.orderValue) || 0;
  const profit = orderValue - totalCost;
  const profitPct = orderValue ? (profit / orderValue * 100) : 0;
  body.innerHTML = `
    <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr);">
      <div class="kpi-card"><div class="label">Total Labour Cost</div><div class="value">${fmtMoney(labourTotal)}</div></div>
      <div class="kpi-card"><div class="label">Total Purchases (Cost)</div><div class="value">${fmtMoney(purchaseTotal)}</div></div>
      <div class="kpi-card"><div class="label">Total Cost</div><div class="value">${fmtMoney(totalCost)}</div></div>
    </div>
    <div class="form-row" style="margin-top:16px;max-width:320px;">
      <label>Price to Customer (£) — what you're quoting the customer</label>
      <input type="number" step="0.01" id="wz-order-value" value="${w.orderValue}">
      ${salesValueTotal > 0 ? `<div class="hint" style="margin-top:6px;">Suggested from your figures: <b>${fmtMoney(suggestedPrice)}</b> (labour at cost + purchases' Price to Customer total of ${fmtMoney(salesValueTotal)}) <button class="btn secondary small" id="wz-use-suggested" type="button" style="margin-left:6px;">Use this</button></div>` : ""}
    </div>
    <div class="kpi-grid" style="grid-template-columns:repeat(2,1fr);margin-top:14px;">
      <div class="kpi-card ${profit >= 0 ? "green" : "red"}" id="wz-profit-card"><div class="label">Profit / Loss</div><div class="value" id="wz-profit">${fmtMoney(profit)}</div></div>
      <div class="kpi-card ${profitPct >= w.marginTarget ? "green" : "amber"}" id="wz-profit-pct-card"><div class="label">Profit % (target ${fmtPct(w.marginTarget)})</div><div class="value" id="wz-profit-pct">${fmtPct(profitPct)}</div></div>
    </div>
    <div class="wizard-actions">
      <button class="btn secondary" id="wz-back" type="button">← Back</button>
      <button class="btn" id="wz-save" type="button">Create Quote</button>
    </div>`;
  const updatePriceDependentUI = () => {
    const ov = Number(w.orderValue) || 0;
    const p = ov - totalCost;
    const pPct = ov ? (p / ov * 100) : 0;
    document.getElementById("wz-profit").textContent = fmtMoney(p);
    document.getElementById("wz-profit-pct").textContent = fmtPct(pPct);
    document.getElementById("wz-profit-card").className = `kpi-card ${p >= 0 ? "green" : "red"}`;
    document.getElementById("wz-profit-pct-card").className = `kpi-card ${pPct >= w.marginTarget ? "green" : "amber"}`;
  };
  document.getElementById("wz-order-value").addEventListener("input", (e) => {
    w.orderValue = e.target.value;
    updatePriceDependentUI();
  });
  document.getElementById("wz-use-suggested")?.addEventListener("click", () => {
    w.orderValue = suggestedPrice.toFixed(2);
    document.getElementById("wz-order-value").value = w.orderValue;
    updatePriceDependentUI();
  });
  document.getElementById("wz-back").addEventListener("click", () => { w.step = 3; renderQuoteWizardModal(); });
  document.getElementById("wz-save").addEventListener("click", saveQuoteWizard);
}

function renderWizardPurchasesStep(body) {
  const w = quoteWizard;
  const rowsHtml = w.purchaseLines.map((line, idx) => `
    <div class="purchase-line-row" data-row="${idx}">
      <input placeholder="Supplier" data-field="supplier" value="${esc(line.supplier || "")}">
      <input placeholder="Description" data-field="description" value="${esc(line.description || "")}">
      <input type="number" step="1" placeholder="Qty" data-field="quantity" value="${line.quantity ?? ""}">
      <input type="number" step="0.01" placeholder="Cost £" data-field="purchase_amount" value="${line.purchase_amount ?? ""}">
      <input type="number" step="0.01" placeholder="Price to Customer £" data-field="sales_value" value="${line.sales_value ?? ""}">
      <button class="btn small danger" type="button" data-remove="${idx}">✕</button>
    </div>`).join("");
  body.innerHTML = `
    <div class="hint">Services and brought-out items for this quote — what the supplier charges you (Cost) and what you'll charge the customer for it (Price to Customer).</div>
    ${w.purchaseLines.length ? `
    <div class="purchase-line-row" style="margin-top:10px;margin-bottom:2px;">
      <span class="wizard-step-label" style="margin:0;">Supplier</span>
      <span class="wizard-step-label" style="margin:0;">Description</span>
      <span class="wizard-step-label" style="margin:0;">Qty</span>
      <span class="wizard-step-label" style="margin:0;">Cost £</span>
      <span class="wizard-step-label" style="margin:0;">Price to Customer £</span>
      <span></span>
    </div>` : ""}
    <div id="wz-purchase-rows">${rowsHtml || '<div class="hint" style="margin-top:10px;">No purchase lines yet.</div>'}</div>
    <button class="btn secondary small" id="wz-add-purchase" type="button" style="margin-top:8px;">+ Add Line</button>
    <div class="wizard-totals">
      <span>Total Cost: <b id="wz-purchase-total">${fmtMoney(wizardPurchaseTotal())}</b></span>
      <span>Total Price to Customer: <b id="wz-sales-total">${fmtMoney(wizardSalesValueTotal())}</b></span>
    </div>
    <div class="wizard-actions">
      <button class="btn secondary" id="wz-back" type="button">← Back</button>
      <button class="btn" id="wz-next" type="button">Next: Summary →</button>
    </div>`;

  body.querySelectorAll(".purchase-line-row input").forEach(inp => inp.addEventListener("input", () => {
    const idx = Number(inp.closest(".purchase-line-row").dataset.row);
    w.purchaseLines[idx][inp.dataset.field] = inp.value;
    document.getElementById("wz-purchase-total").textContent = fmtMoney(wizardPurchaseTotal());
    document.getElementById("wz-sales-total").textContent = fmtMoney(wizardSalesValueTotal());
  }));
  body.querySelectorAll("[data-remove]").forEach(b => b.addEventListener("click", () => {
    w.purchaseLines.splice(Number(b.dataset.remove), 1);
    renderWizardPurchasesStep(body);
  }));
  document.getElementById("wz-add-purchase").addEventListener("click", () => {
    w.purchaseLines.push({ supplier: "", description: "", quantity: "", purchase_amount: "", sales_value: "" });
    renderWizardPurchasesStep(body);
  });
  document.getElementById("wz-back").addEventListener("click", () => { w.step = 2; renderQuoteWizardModal(); });
  document.getElementById("wz-next").addEventListener("click", () => { w.step = 4; renderQuoteWizardModal(); });
}

async function saveQuoteWizard() {
  if (!confirm("Have all purchases and hours been recorded for this quote?")) return;
  const errEl = document.getElementById("wizard-error");
  errEl.textContent = "";
  const saveBtn = document.getElementById("wz-save");
  saveBtn.disabled = true;
  const w = quoteWizard;
  const payload = {
    customer_id: w.customer_id ? Number(w.customer_id) : null,
    description: w.description,
    owner: w.owner,
    quote_date: w.quote_date,
    valid_until: w.valid_until,
    notes: w.notes,
    contact_name: w.contact_name,
    your_ref: w.your_ref,
    works_order_ref: w.works_order_ref,
    works_order_id: w.works_order_id ? Number(w.works_order_id) : null,
    scope_items: w.scope_items,
    delivery_terms: w.delivery_terms,
    estimated_value: Number(w.orderValue) || 0,
    status: "Draft",
    labour_lines: w.labourLines.map(l => ({
      category: l.category, activity: l.activity,
      standard_rate: l.standard_rate, ot_rate: l.ot_rate,
      standard_hours: Number(l.standard_hours) || 0, ot_hours: Number(l.ot_hours) || 0,
    })),
    purchase_lines: w.purchaseLines.map(l => ({
      supplier: l.supplier, description: l.description,
      quantity: Number(l.quantity) || 0, purchase_amount: Number(l.purchase_amount) || 0,
      sales_value: Number(l.sales_value) || 0,
    })),
  };
  try {
    await api("/api/quotes", { method: "POST", body: payload });
    closeQuoteWizard();
    renderQuotes();
  } catch (err) {
    errEl.textContent = err.message;
    saveBtn.disabled = false;
  }
}

async function viewQuoteBreakdown(q) {
  const data = await api(`/api/quotes/${q.quote_id}/cost-breakdown`);
  const labourTotal = data.labour_lines.reduce((s, l) => s + l.standard_hours * l.standard_rate + l.ot_hours * l.ot_rate, 0);
  const purchaseTotal = data.purchase_lines.reduce((s, l) => s + l.purchase_amount, 0);
  const salesValueTotal = data.purchase_lines.reduce((s, l) => s + l.sales_value, 0);
  const totalCost = labourTotal + purchaseTotal;
  const profit = q.estimated_value - totalCost;
  const profitPct = q.estimated_value ? (profit / q.estimated_value * 100) : 0;
  const hasData = data.labour_lines.length || data.purchase_lines.length;
  const body = hasData ? `
    ${data.labour_lines.length ? `
      <h3 style="font-size:13px;color:var(--navy);margin-bottom:6px;">Labour</h3>
      <table class="data-table"><thead><tr><th>Activity</th><th class="num">Std Hrs</th><th class="num">OT Hrs</th><th class="num">Total</th></tr></thead>
      <tbody>${data.labour_lines.map(l => `<tr><td>${esc(l.activity)}</td><td class="num">${l.standard_hours}</td><td class="num">${l.ot_hours}</td><td class="num">${fmtMoney(l.standard_hours * l.standard_rate + l.ot_hours * l.ot_rate)}</td></tr>`).join("")}</tbody></table>` : ""}
    ${data.purchase_lines.length ? `
      <h3 style="font-size:13px;color:var(--navy);margin:14px 0 6px;">Purchases</h3>
      <table class="data-table"><thead><tr><th>Supplier</th><th>Description</th><th class="num">Qty</th><th class="num">Cost</th><th class="num">Price to Customer</th></tr></thead>
      <tbody>${data.purchase_lines.map(l => `<tr><td>${esc(l.supplier || "")}</td><td>${esc(l.description || "")}</td><td class="num">${l.quantity}</td><td class="num">${fmtMoney(l.purchase_amount)}</td><td class="num">${fmtMoney(l.sales_value)}</td></tr>`).join("")}</tbody></table>` : ""}
    <div class="wizard-totals" style="margin-top:14px;">
      <span>Total Cost: <b>${fmtMoney(totalCost)}</b></span>
      ${salesValueTotal > 0 ? `<span>Purchases Priced to Customer: <b>${fmtMoney(salesValueTotal)}</b></span>` : ""}
      <span>Price to Customer: <b>${fmtMoney(q.estimated_value)}</b></span>
      <span>Profit: <b>${fmtMoney(profit)} (${fmtPct(profitPct)})</b></span>
    </div>
    <div style="margin-top:16px;"><a class="btn secondary" href="/api/quotes/${q.quote_id}/pdf">Download Quote PDF</a></div>
  ` : `<div class="empty-state">No cost buildup recorded for this quote — it was created without the wizard, or before this feature existed.</div>
    <div style="margin-top:16px;"><a class="btn secondary" href="/api/quotes/${q.quote_id}/pdf">Download Quote PDF</a></div>`;
  openModal(`Cost Breakdown — ${q.quote_number}`, body, { showFooter: false });
}

/* ======================================================================
   CUSTOMERS
   ====================================================================== */
async function renderCustomers() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading customers…</div>`;
  const rows = await api("/api/customers");
  root.innerHTML = totalSummaryHtml("Total customers", rows.length) + `
    <div class="toolbar">
      <div class="hint">${isAdmin() ? "Manage customer records." : "Read-only — only Administrators can edit customers."}</div>
      ${isAdmin() ? `<button class="btn" id="cust-new">+ New Customer</button>` : ""}
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Name</th><th>Contact</th><th>Email</th><th>Phone</th><th>Address</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.customer_name)}</b></td>
            <td>${esc(r.contact_name || "")}</td>
            <td>${esc(r.email || "")}</td>
            <td>${esc(r.phone || "")}</td>
            <td>${esc(r.address || "")}</td>
            <td class="actions-cell">
              ${isAdmin() ? `<button class="btn small secondary" data-edit="${r.customer_id}">Edit</button><button class="btn small danger" data-del="${r.customer_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="6"><div class="empty-state">No customers found.</div></td></tr>`}
      </tbody>
    </table></div>
  `;
  document.getElementById("cust-new")?.addEventListener("click", () => openCustomerForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    openCustomerForm(rows.find(r => String(r.customer_id) === b.dataset.edit));
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this customer?", "/api/customers/" + b.dataset.del, renderCustomers)
  ));
}

function openCustomerForm(c = null) {
  const body = `
    <div class="form-row"><label>Customer Name</label><input name="customer_name" value="${esc(c?.customer_name || "")}" required></div>
    <div class="form-row"><label>Contact Name</label><input name="contact_name" value="${esc(c?.contact_name || "")}"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Email</label><input type="email" name="email" value="${esc(c?.email || "")}"></div>
      <div class="form-row"><label>Phone</label><input name="phone" value="${esc(c?.phone || "")}"></div>
    </div>
    <div class="form-row"><label>Address</label><input name="address" value="${esc(c?.address || "")}"></div>
    ${auditHint(c)}
  `;
  openModal(c ? "Edit Customer" : "New Customer", body, {
    submitLabel: "Save",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      if (c) await api("/api/customers/" + c.customer_id, { method: "PUT", body: payload });
      else await api("/api/customers", { method: "POST", body: payload });
      state.ref = await api("/api/reference");
      closeModal();
      renderCustomers();
    },
  });
}

/* ======================================================================
   SUPPLIERS
   ====================================================================== */
async function renderSuppliers() {
  const root = document.getElementById("view-root");
  root.innerHTML = `<div class="empty-state">Loading suppliers…</div>`;
  const rows = await api("/api/suppliers");
  root.innerHTML = totalSummaryHtml("Total suppliers", rows.length) + `
    <div class="toolbar">
      <div class="hint">${isAdmin() ? "Manage supplier records." : "Read-only — only Administrators can edit suppliers."}</div>
      ${isAdmin() ? `<button class="btn" id="sup-new">+ New Supplier</button>` : ""}
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Name</th><th>Supply Of</th><th>Reason For Use</th><th>Authorised By</th><th>Contact</th><th>Email</th><th>VAT Number</th><th>Address</th><th></th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(r => `
          <tr>
            <td><b>${esc(r.supplier_name)}</b></td>
            <td>${esc(r.supply_of || "")}</td>
            <td>${esc(r.reason_for_use || "")}</td>
            <td>${esc(r.authorised_by || "")}</td>
            <td>${esc(r.contact_name || "")}</td>
            <td>${esc(r.email || "")}</td>
            <td>${esc(r.vat_number || "")}</td>
            <td>${esc(r.address || "")}</td>
            <td class="actions-cell">
              ${isAdmin() ? `<button class="btn small secondary" data-edit="${r.supplier_id}">Edit</button><button class="btn small danger" data-del="${r.supplier_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("") : `<tr><td colspan="9"><div class="empty-state">No suppliers found.</div></td></tr>`}
      </tbody>
    </table></div>
  `;
  document.getElementById("sup-new")?.addEventListener("click", () => openSupplierForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    openSupplierForm(rows.find(r => String(r.supplier_id) === b.dataset.edit));
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this supplier?", "/api/suppliers/" + b.dataset.del, renderSuppliers)
  ));
}

function openSupplierForm(s = null) {
  const body = `
    <div class="form-row"><label>Supplier Name</label><input name="supplier_name" value="${esc(s?.supplier_name || "")}" required></div>
    <div class="form-row"><label>Supply Of</label><input name="supply_of" value="${esc(s?.supply_of || "")}" placeholder="What they supply, e.g. specialist fasteners"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Reason For Use</label><input name="reason_for_use" value="${esc(s?.reason_for_use || "")}" placeholder="e.g. only supplier available, local, OEM"></div>
      <div class="form-row"><label>Authorised By</label><input name="authorised_by" value="${esc(s?.authorised_by || "")}"></div>
    </div>
    <div class="form-row"><label>Contact Name</label><input name="contact_name" value="${esc(s?.contact_name || "")}"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Email</label><input type="email" name="email" value="${esc(s?.email || "")}"></div>
      <div class="form-row"><label>VAT Number</label><input name="vat_number" value="${esc(s?.vat_number || "")}"></div>
    </div>
    <div class="form-row"><label>Address</label><input name="address" value="${esc(s?.address || "")}"></div>
    ${auditHint(s)}
  `;
  openModal(s ? "Edit Supplier" : "New Supplier", body, {
    submitLabel: "Save",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      if (s) await api("/api/suppliers/" + s.supplier_id, { method: "PUT", body: payload });
      else await api("/api/suppliers", { method: "POST", body: payload });
      state.ref = await api("/api/reference");
      closeModal();
      renderSuppliers();
    },
  });
}

/* ======================================================================
   REPORTING
   ====================================================================== */
let repState = { view: "profitability", sortKey: "job_number", sortDir: 1, search: "", page: 1, groupBy: "job", lossOnly: false, customerFilter: "", codeFilter: "", locationFilter: "", notInvoicedOnly: false };
let spendSupplierState = { search: "", year: "", includeZero: false, sortKey: "total_spend", sortDir: -1, page: 1 };
let monthlyIncomeSpendState = { year: "" };
let incomeByCodeState = { search: "", personFilter: "", customerFilter: "", codeFilter: "", sortKey: "revenue", sortDir: -1, page: 1 };

// The Reporting nav item covers four distinct reports, switched between
// with their own top-level toggle group (same look as the existing By
// Job/Customer/Code toggle within the Job Profitability report, one level
// up) rather than four separate sidebar entries - they're all "Reporting"
// to Simon, just different cuts of the same underlying financial data.
const REPORT_VIEWS = [
  { id: "profitability", label: "Job Profitability" },
  { id: "supplier-spend", label: "Spend by Supplier" },
  { id: "monthly", label: "Monthly Income & Spend" },
  { id: "income-by-code", label: "Income by Contract Code" },
  { id: "hours-booked", label: "Hours Booked" },
];
function reportTypeToolbarHtml(active) {
  return `
    <div class="toolbar" style="margin-bottom:10px;">
      <div class="toggle-group" id="report-type-toggle">
        ${REPORT_VIEWS.map(v => `<button class="toggle-btn ${active === v.id ? "active" : ""}" data-report-view="${v.id}" type="button">${v.label}</button>`).join("")}
      </div>
    </div>`;
}
function wireReportTypeToolbar() {
  document.querySelectorAll("#report-type-toggle [data-report-view]").forEach(btn => {
    btn.addEventListener("click", () => {
      const view = btn.dataset.reportView;
      if (repState.view === view) return;
      repState.view = view;
      renderReporting();
    });
  });
}

async function renderReporting() {
  switch (repState.view) {
    case "supplier-spend": return renderSpendBySupplierReport();
    case "monthly": return renderMonthlyIncomeSpendReport();
    case "income-by-code": return renderIncomeByContractCodeReport();
    case "hours-booked": return renderHoursBookedReport();
    default: return renderJobProfitabilityReport();
  }
}

// Decoded from the Order Book's own "Info Sheet" legend. Keyed uppercase so
// lookups are case-insensitive; "display" preserves the legend's own casing.
const CONTRACT_CODE_INFO = {
  "FS": { display: "FS", label: "Fixture Service" },
  "FR/TL": { display: "FR/TL", label: "Fixture Repair (booked on TEAM Live)" },
  "FR": { display: "FR", label: "" },
  "TR": { display: "TR", label: "Tooling/Fixture Repair" },
  "TRP": { display: "TRP", label: "Transport" },
  "FSP": { display: "FSp", label: "Fixture Spares" },
  "TSP": { display: "TSp", label: "Tooling Spare" },
  "LR": { display: "LR", label: "Lifter Repair" },
  "LSP": { display: "LSp", label: "Lifter Spares" },
  "LNEW": { display: "LNew", label: "Lifting New" },
  "INT": { display: "INT", label: "Integration" },
  "MS": { display: "MS", label: "Machine Service" },
  "MR": { display: "MR", label: "Machine Repairs" },
  "MSP": { display: "MSp", label: "Machine Service Spares" },
  "M": { display: "M", label: "Maintenance" },
  "MH": { display: "MH", label: "" },
  "CL": { display: "CL", label: "Cleaning" },
  "NEW": { display: "NEW", label: "New Manufacture" },
  "CO": { display: "CO", label: "Co-Ordinator" },
  "LS": { display: "LS", label: "Lifter Service" },
  "TH": { display: "TH", label: "" },
  "DSN": { display: "DSN", label: "Design" },
  "FM": { display: "FM", label: "Fixture Modification" },
  "FS - L": { display: "FS - L", label: "Fixture Service (Low)" },
  "FS - M": { display: "FS - M", label: "Fixture Service (Medium)" },
  "FS - H": { display: "FS - H", label: "Fixture Service (High)" },
  "FS - HQ": { display: "FS - HQ", label: "Fixture Service (HQ)" },
};

// The standard time/cost recipe behind each fixed-price FS tier - shown
// as a hint on the Works Order form so a service engineer logging hours
// against one of these jobs knows what's expected, via the SAME rate
// card entries used everywhere else (Fitting / Assembly, CMM, Team
// Transport) plus consumables issued from Inventory (rags, oil, general
// consumables) - not a separate mechanism of its own.
const FS_TIER_RECIPES = {
  "FS - L": "4h Fitting / Assembly, 0.5h Team Transport, ~£9.63 of consumables (rags/oil) issued from Inventory.",
  "FS - M": "5h Fitting / Assembly, 2h CMM, 0.5h Team Transport, ~£11.29 of consumables issued from Inventory.",
  "FS - H": "9h Fitting / Assembly, 3h CMM, 1h Team Transport, ~£58.18 of consumables issued from Inventory.",
  "FS - HQ": "6.3h Fitting / Assembly, 3h CMM, ~£58.18 of consumables issued from Inventory (no transport).",
};
function contractCodeInfo(code) {
  if (!code) return { display: "Unspecified", label: "No contract code recorded" };
  const info = CONTRACT_CODE_INFO[code.trim().toUpperCase()];
  return info || { display: code, label: "" };
}

function aggregateByCustomer(rows) {
  const map = new Map();
  for (const r of rows) {
    const key = r.customer_name || "Unknown";
    if (!map.has(key)) {
      map.set(key, {
        customer_name: key, jobs: 0, loss_making_jobs: 0,
        contract_value: 0, actual_cost: 0, committed_cost: 0,
        forecast_cost: 0, revenue: 0, profit: 0, forecast_profit: 0,
      });
    }
    const agg = map.get(key);
    agg.jobs += 1;
    if (r.profit < 0) agg.loss_making_jobs += 1;
    agg.contract_value += r.contract_value;
    agg.actual_cost += r.actual_cost;
    agg.committed_cost += r.committed_cost;
    agg.forecast_cost += r.forecast_cost;
    agg.revenue += r.revenue;
    agg.profit += r.profit;
    agg.forecast_profit += r.forecast_profit;
  }
  // Margin must be computed from summed revenue/profit, not averaged per-job
  // margins - otherwise a handful of small jobs can skew a customer's
  // overall rate away from what their actual pound-for-pound margin is.
  return Array.from(map.values()).map(agg => ({
    ...agg,
    margin_pct: agg.revenue ? (agg.profit / agg.revenue) * 100 : 0,
  }));
}

function aggregateByContractCode(rows) {
  const map = new Map();
  for (const r of rows) {
    const info = contractCodeInfo(r.contract_code);
    const key = info.display;
    if (!map.has(key)) {
      map.set(key, {
        contract_code: key, label: info.label, jobs: 0, loss_making_jobs: 0,
        contract_value: 0, actual_cost: 0, committed_cost: 0,
        forecast_cost: 0, revenue: 0, profit: 0, forecast_profit: 0,
        margin_target: r.margin_target ?? 15, // same for every job sharing this code
      });
    }
    const agg = map.get(key);
    agg.jobs += 1;
    if (r.profit < 0) agg.loss_making_jobs += 1;
    agg.contract_value += r.contract_value;
    agg.actual_cost += r.actual_cost;
    agg.committed_cost += r.committed_cost;
    agg.forecast_cost += r.forecast_cost;
    agg.revenue += r.revenue;
    agg.profit += r.profit;
    agg.forecast_profit += r.forecast_profit;
  }
  return Array.from(map.values()).map(agg => ({
    ...agg,
    margin_pct: agg.revenue ? (agg.profit / agg.revenue) * 100 : 0,
  }));
}

function computeTotals(rows) {
  const totals = rows.reduce((acc, r) => {
    acc.contract_value += r.contract_value;
    acc.actual_cost += r.actual_cost;
    acc.revenue += r.revenue;
    acc.profit += r.profit;
    if (r.profit < 0) acc.loss_making_jobs += 1;
    return acc;
  }, { contract_value: 0, actual_cost: 0, revenue: 0, profit: 0, loss_making_jobs: 0 });
  totals.margin_pct = totals.revenue ? (totals.profit / totals.revenue) * 100 : 0;
  totals.jobs = rows.length;
  return totals;
}

function downloadCSV(filename, headers, rows) {
  const escapeCsv = (v) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [headers.map(escapeCsv).join(","), ...rows.map(row => row.map(escapeCsv).join(","))];
  const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function renderJobProfitabilityReport() {
  const root = document.getElementById("view-root");
  root.innerHTML = reportTypeToolbarHtml("profitability") + `<div class="empty-state">Loading report…</div>`;
  wireReportTypeToolbar();
  const [jobRowsRaw, marginSettings] = await Promise.all([
    api("/api/reporting"),
    api("/api/settings/margin-targets"),
  ]);
  const defaultTarget = marginSettings.default_target ?? 15;

  // Dropdown option lists come from the full unfiltered dataset (and the
  // customer reference list), so picking one filter never shrinks the
  // other dropdown's own options.
  const customerOptions = state.ref.customers.map(c => c.customer_name).sort();
  const codeOptionsSet = new Set(jobRowsRaw.map(r => r.contract_code).filter(Boolean));
  const codeOptions = [...codeOptionsSet].sort();
  const locationOptionsSet = new Set(jobRowsRaw.map(r => r.location).filter(Boolean));
  const locationOptions = [...locationOptionsSet].sort();

  let jobRows = jobRowsRaw;
  if (repState.search) {
    const q = repState.search.toLowerCase();
    jobRows = jobRows.filter(r =>
      r.job_number.toLowerCase().includes(q) ||
      r.customer_name.toLowerCase().includes(q) ||
      (r.contract_code || "").toLowerCase().includes(q)
    );
  }
  if (repState.customerFilter) jobRows = jobRows.filter(r => r.customer_name === repState.customerFilter);
  if (repState.codeFilter) jobRows = jobRows.filter(r => (r.contract_code || "") === repState.codeFilter);
  if (repState.locationFilter) jobRows = jobRows.filter(r => (r.location || "") === repState.locationFilter);
  // contract_value > 0 excludes "No Charge"/warranty jobs with nothing to
  // invoice in the first place - those show £0 revenue too, but flagging
  // them here would just be noise, not a real outstanding-invoice case.
  if (repState.notInvoicedOnly) jobRows = jobRows.filter(r => r.status === "Completed" && !r.invoiced && r.contract_value > 0);

  const totals = computeTotals(jobRows);
  const mode = repState.groupBy; // "job" | "customer" | "code"
  const baseRows = repState.lossOnly ? jobRows.filter(r => r.profit < 0) : jobRows;
  let allRows = baseRows;
  if (mode === "customer") allRows = aggregateByCustomer(baseRows);
  else if (mode === "code") allRows = aggregateByContractCode(baseRows);

  allRows.sort((a, b) => {
    const av = a[repState.sortKey], bv = b[repState.sortKey];
    const cmp = typeof av === "number" ? av - bv : String(av ?? "").localeCompare(String(bv ?? ""));
    return cmp * repState.sortDir;
  });

  const totalPages = Math.max(1, Math.ceil(allRows.length / PAGE_SIZE));
  repState.page = Math.min(Math.max(1, repState.page), totalPages);
  const rows = allRows.slice((repState.page - 1) * PAGE_SIZE, repState.page * PAGE_SIZE);

  const kpi = (label, value, cls = "", id = null) => {
    const tag = id ? "button" : "div";
    return `<${tag} class="kpi-card ${cls}${id ? " clickable" : ""}"${id ? ` id="${id}" type="button"` : ""}><div class="label">${label}</div><div class="value">${value}</div></${tag}>`;
  };

  const jobHeaderRow = `
        <th data-key="job_number">Job Number</th>
        <th data-key="customer_name">Customer</th>
        <th data-key="contract_code">Code</th>
        <th data-key="location">Location</th>
        <th data-key="contract_value" class="num">Purchase Order Value</th>
        <th data-key="actual_cost" class="num">Actual Cost</th>
        <th data-key="committed_cost" class="num">Total Purchases</th>
        <th data-key="forecast_cost" class="num">Quoted</th>
        <th data-key="revenue" class="num">Revenue</th>
        <th data-key="profit" class="num">Profit</th>
        <th data-key="forecast_profit" class="num">Forecast Profit</th>
        <th data-key="margin_pct" class="num">Margin %</th>
        <th data-key="status">Status</th>
        <th data-key="invoiced">Invoiced</th>`;
  const customerHeaderRow = `
        <th data-key="customer_name">Customer</th>
        <th data-key="jobs" class="num">Jobs</th>
        <th data-key="loss_making_jobs" class="num">Loss-Making</th>
        <th data-key="contract_value" class="num">Purchase Order Value</th>
        <th data-key="actual_cost" class="num">Actual Cost</th>
        <th data-key="committed_cost" class="num">Total Purchases</th>
        <th data-key="forecast_cost" class="num">Quoted</th>
        <th data-key="revenue" class="num">Revenue</th>
        <th data-key="profit" class="num">Profit</th>
        <th data-key="forecast_profit" class="num">Forecast Profit</th>
        <th data-key="margin_pct" class="num">Margin %</th>`;
  const codeHeaderRow = `
        <th data-key="contract_code">Code</th>
        <th data-key="label">Description</th>
        <th data-key="jobs" class="num">Jobs</th>
        <th data-key="loss_making_jobs" class="num">Loss-Making</th>
        <th data-key="contract_value" class="num">Purchase Order Value</th>
        <th data-key="actual_cost" class="num">Actual Cost</th>
        <th data-key="committed_cost" class="num">Total Purchases</th>
        <th data-key="forecast_cost" class="num">Quoted</th>
        <th data-key="revenue" class="num">Revenue</th>
        <th data-key="profit" class="num">Profit</th>
        <th data-key="forecast_profit" class="num">Forecast Profit</th>
        <th data-key="margin_pct" class="num">Margin %</th>
        <th data-key="margin_target" class="num">Target %</th>`;

  const jobRow = (r) => `
          <tr>
            <td><b>${esc(r.job_number)}</b></td>
            <td>${esc(r.customer_name)}</td>
            <td>${r.contract_code ? esc(contractCodeInfo(r.contract_code).display) : "—"}</td>
            <td>${esc(r.location || "—")}</td>
            <td class="num">${fmtMoney(r.contract_value)}</td>
            <td class="num">${fmtMoney(r.actual_cost)}</td>
            <td class="num">${fmtMoney(r.committed_cost)}</td>
            <td class="num">${fmtMoney(r.forecast_cost)}</td>
            <td class="num">${fmtMoney(r.revenue)}</td>
            <td class="num">${fmtMoney(r.profit)}</td>
            <td class="num">${fmtMoney(r.forecast_profit)}</td>
            <td class="num">${marginBadge(r.margin_pct, r.margin_target ?? 15)}</td>
            <td>${statusBadge(r.status)}</td>
            <td>${r.invoiced ? lightBadge("green", "Yes") : lightBadge("amber", "No")}</td>
          </tr>`;
  const customerRow = (r) => `
          <tr>
            <td><b>${esc(r.customer_name)}</b></td>
            <td class="num">${r.jobs.toLocaleString()}</td>
            <td class="num">${r.loss_making_jobs > 0 ? lightBadge("red", r.loss_making_jobs) : r.loss_making_jobs}</td>
            <td class="num">${fmtMoney(r.contract_value)}</td>
            <td class="num">${fmtMoney(r.actual_cost)}</td>
            <td class="num">${fmtMoney(r.committed_cost)}</td>
            <td class="num">${fmtMoney(r.forecast_cost)}</td>
            <td class="num">${fmtMoney(r.revenue)}</td>
            <td class="num">${fmtMoney(r.profit)}</td>
            <td class="num">${fmtMoney(r.forecast_profit)}</td>
            <td class="num">${marginBadge(r.margin_pct)}</td>
          </tr>`;
  const codeRow = (r) => `
          <tr>
            <td><b>${esc(r.contract_code)}</b></td>
            <td>${esc(r.label || "")}</td>
            <td class="num">${r.jobs.toLocaleString()}</td>
            <td class="num">${r.loss_making_jobs > 0 ? lightBadge("red", r.loss_making_jobs) : r.loss_making_jobs}</td>
            <td class="num">${fmtMoney(r.contract_value)}</td>
            <td class="num">${fmtMoney(r.actual_cost)}</td>
            <td class="num">${fmtMoney(r.committed_cost)}</td>
            <td class="num">${fmtMoney(r.forecast_cost)}</td>
            <td class="num">${fmtMoney(r.revenue)}</td>
            <td class="num">${fmtMoney(r.profit)}</td>
            <td class="num">${fmtMoney(r.forecast_profit)}</td>
            <td class="num">${marginBadge(r.margin_pct, r.margin_target ?? 15)}</td>
            <td class="num">${fmtPct(r.margin_target ?? 15)}</td>
          </tr>`;

  const headerRow = mode === "customer" ? customerHeaderRow : mode === "code" ? codeHeaderRow : jobHeaderRow;
  const rowFn = mode === "customer" ? customerRow : mode === "code" ? codeRow : jobRow;
  const colCount = mode === "code" ? 13 : mode === "customer" ? 11 : 14;
  const unitLabel = mode === "customer" ? "customer" : mode === "code" ? "contract code" : "job";

  root.innerHTML = reportTypeToolbarHtml("profitability") + `
    <div class="kpi-grid">
      ${kpi("Purchase Order Value", fmtMoney(totals.contract_value))}
      ${kpi("Revenue", fmtMoney(totals.revenue))}
      ${kpi("Actual Cost", fmtMoney(totals.actual_cost))}
      ${kpi("Profit", fmtMoney(totals.profit), totals.profit >= 0 ? "green" : "red")}
      ${kpi("Overall Margin", fmtPct(totals.margin_pct), totals.margin_pct >= defaultTarget ? "green" : "amber")}
      ${kpi("Loss-Making Jobs", totals.loss_making_jobs, totals.loss_making_jobs > 0 ? "red" : "green", totals.loss_making_jobs > 0 ? "rep-kpi-loss" : null)}
    </div>
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="rep-search" placeholder="Search job, customer or contract code…" value="${esc(repState.search)}">
        <select id="rep-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${repState.customerFilter === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="rep-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${repState.codeFilter === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
        <select id="rep-location-filter">
          <option value="">All locations</option>
          ${locationOptions.map(loc => `<option value="${esc(loc)}" ${repState.locationFilter === loc ? "selected" : ""}>${esc(loc)}</option>`).join("")}
        </select>
      </div>
      <div class="filters">
        <button class="btn ${repState.notInvoicedOnly ? "" : "secondary"} small" id="rep-toggle-not-invoiced">${repState.notInvoicedOnly ? "✓ " : ""}Completed, Not Yet Invoiced</button>
        ${repState.lossOnly ? `<button class="btn secondary small" id="rep-clear-loss">✕ Loss-making jobs only</button>` : ""}
        <div class="toggle-group">
          <button class="toggle-btn ${mode === "job" ? "active" : ""}" id="rep-mode-job">By Job</button>
          <button class="toggle-btn ${mode === "customer" ? "active" : ""}" id="rep-mode-customer">By Customer</button>
          <button class="toggle-btn ${mode === "code" ? "active" : ""}" id="rep-mode-code">By Contract Code</button>
        </div>
        <button class="btn secondary" id="rep-export">Export to CSV</button>
      </div>
    </div>
    <div class="hint">${allRows.length.toLocaleString()} ${unitLabel}${allRows.length === 1 ? "" : "s"}${mode !== "job" ? ` from ${baseRows.length.toLocaleString()} job${baseRows.length === 1 ? "" : "s"}` : ""}${repState.lossOnly ? " (loss-making only)" : ""}${repState.notInvoicedOnly ? " (completed, not yet invoiced)" : ""}${repState.customerFilter ? ` · ${esc(repState.customerFilter)}` : ""}${repState.codeFilter ? ` · ${esc(contractCodeInfo(repState.codeFilter).display)}` : ""}${repState.locationFilter ? ` · ${esc(repState.locationFilter)}` : ""}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>${headerRow}</tr></thead>
      <tbody>
        ${rows.length ? rows.map(rowFn).join("") : `<tr><td colspan="${colCount}"><div class="empty-state">No ${repState.notInvoicedOnly ? "completed jobs awaiting invoicing" : repState.lossOnly ? "loss-making jobs" : "data"}.</div></td></tr>`}
      </tbody>
    </table></div>
    ${renderPager(repState.page, totalPages, "rep-page")}
  `;
  wireReportTypeToolbar();
  document.getElementById("rep-search").addEventListener("input", debounce(e => { repState.search = e.target.value; repState.page = 1; debouncedSearchRerender(e.target, renderJobProfitabilityReport); }, 500));
  document.getElementById("rep-customer-filter").addEventListener("change", e => { repState.customerFilter = e.target.value; repState.page = 1; renderJobProfitabilityReport(); });
  document.getElementById("rep-code-filter").addEventListener("change", e => { repState.codeFilter = e.target.value; repState.page = 1; renderJobProfitabilityReport(); });
  document.getElementById("rep-location-filter").addEventListener("change", e => { repState.locationFilter = e.target.value; repState.page = 1; renderJobProfitabilityReport(); });
  document.getElementById("rep-toggle-not-invoiced").addEventListener("click", () => {
    repState.notInvoicedOnly = !repState.notInvoicedOnly;
    repState.page = 1;
    renderJobProfitabilityReport();
  });
  document.getElementById("rep-mode-job").addEventListener("click", () => {
    if (repState.groupBy === "job") return;
    repState.groupBy = "job"; repState.sortKey = "job_number"; repState.sortDir = 1; repState.page = 1;
    renderJobProfitabilityReport();
  });
  document.getElementById("rep-mode-customer").addEventListener("click", () => {
    if (repState.groupBy === "customer") return;
    repState.groupBy = "customer"; repState.sortKey = "profit"; repState.sortDir = -1; repState.page = 1;
    renderJobProfitabilityReport();
  });
  document.getElementById("rep-mode-code").addEventListener("click", () => {
    if (repState.groupBy === "code") return;
    repState.groupBy = "code"; repState.sortKey = "profit"; repState.sortDir = -1; repState.page = 1;
    renderJobProfitabilityReport();
  });
  document.getElementById("rep-kpi-loss")?.addEventListener("click", () => {
    repState.lossOnly = true;
    repState.groupBy = "job";
    repState.sortKey = "profit";
    repState.sortDir = 1; // most negative (worst losses) first
    repState.page = 1;
    renderJobProfitabilityReport();
  });
  document.getElementById("rep-clear-loss")?.addEventListener("click", () => {
    repState.lossOnly = false;
    repState.page = 1;
    renderJobProfitabilityReport();
  });
  document.getElementById("rep-export").addEventListener("click", () => {
    if (mode === "customer") {
      const headers = ["Customer", "Jobs", "Loss-Making Jobs", "Purchase Order Value", "Actual Cost", "Total Purchases", "Quoted", "Revenue", "Profit", "Forecast Profit", "Margin %"];
      const csvRows = allRows.map(r => [r.customer_name, r.jobs, r.loss_making_jobs, r.contract_value.toFixed(2), r.actual_cost.toFixed(2), r.committed_cost.toFixed(2), r.forecast_cost.toFixed(2), r.revenue.toFixed(2), r.profit.toFixed(2), r.forecast_profit.toFixed(2), r.margin_pct.toFixed(2)]);
      downloadCSV("profitability_by_customer.csv", headers, csvRows);
    } else if (mode === "code") {
      const headers = ["Contract Code", "Description", "Jobs", "Loss-Making Jobs", "Purchase Order Value", "Actual Cost", "Total Purchases", "Quoted", "Revenue", "Profit", "Forecast Profit", "Margin %"];
      const csvRows = allRows.map(r => [r.contract_code, r.label, r.jobs, r.loss_making_jobs, r.contract_value.toFixed(2), r.actual_cost.toFixed(2), r.committed_cost.toFixed(2), r.forecast_cost.toFixed(2), r.revenue.toFixed(2), r.profit.toFixed(2), r.forecast_profit.toFixed(2), r.margin_pct.toFixed(2)]);
      downloadCSV("profitability_by_contract_code.csv", headers, csvRows);
    } else {
      const params = new URLSearchParams();
      if (repState.search) params.set("q", repState.search);
      if (repState.customerFilter) params.set("customer", repState.customerFilter);
      if (repState.codeFilter) params.set("code", repState.codeFilter);
      if (repState.locationFilter) params.set("location", repState.locationFilter);
      if (repState.lossOnly) params.set("loss_only", "1");
      if (repState.notInvoicedOnly) params.set("not_invoiced", "1");
      window.location.href = "/api/reporting/export?" + params.toString();
    }
  });
  wirePager("rep-page", repState.page, totalPages, p => { repState.page = p; renderJobProfitabilityReport(); });
  root.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.key;
    repState.sortDir = repState.sortKey === key ? -repState.sortDir : 1;
    repState.sortKey = key;
    repState.page = 1;
    renderJobProfitabilityReport();
  }));
}

/* ----------------------------------------------------------------------
   Spend by Supplier
   ---------------------------------------------------------------------- */
async function renderSpendBySupplierReport() {
  const root = document.getElementById("view-root");
  root.innerHTML = reportTypeToolbarHtml("supplier-spend") + `<div class="empty-state">Loading report…</div>`;
  wireReportTypeToolbar();
  const params = new URLSearchParams();
  if (spendSupplierState.year) params.set("year", spendSupplierState.year);
  const data = await api("/api/reporting/spend-by-supplier?" + params.toString());

  let rows = data.rows;
  if (!spendSupplierState.includeZero) rows = rows.filter(r => r.invoice_count > 0);
  if (spendSupplierState.search) {
    const q = spendSupplierState.search.toLowerCase();
    rows = rows.filter(r => r.supplier_name.toLowerCase().includes(q));
  }
  rows = [...rows].sort((a, b) => {
    const av = a[spendSupplierState.sortKey], bv = b[spendSupplierState.sortKey];
    const cmp = typeof av === "number" ? av - bv : String(av ?? "").localeCompare(String(bv ?? ""));
    return cmp * spendSupplierState.sortDir;
  });

  const totals = rows.reduce((acc, r) => {
    acc.total_spend += r.total_spend;
    acc.invoice_count += r.invoice_count;
    return acc;
  }, { total_spend: 0, invoice_count: 0 });

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  spendSupplierState.page = Math.min(Math.max(1, spendSupplierState.page), totalPages);
  const pageRows = rows.slice((spendSupplierState.page - 1) * PAGE_SIZE, spendSupplierState.page * PAGE_SIZE);

  const kpi = (label, value, cls = "") => `<div class="kpi-card ${cls}"><div class="label">${label}</div><div class="value">${value}</div></div>`;

  root.innerHTML = reportTypeToolbarHtml("supplier-spend") + `
    <div class="kpi-grid">
      ${kpi("Total Spend" + (spendSupplierState.year ? ` (${esc(spendSupplierState.year)})` : ""), fmtMoney(totals.total_spend))}
      ${kpi("Supplier Invoices", totals.invoice_count.toLocaleString())}
      ${kpi(spendSupplierState.includeZero ? "Suppliers" : "Suppliers with Spend", rows.length.toLocaleString())}
    </div>
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="ss-search" placeholder="Search supplier…" value="${esc(spendSupplierState.search)}">
        <select id="ss-year-filter">
          <option value="">All years</option>
          ${data.years.map(y => `<option value="${esc(y)}" ${spendSupplierState.year === y ? "selected" : ""}>${esc(y)}</option>`).join("")}
        </select>
        <button class="btn ${spendSupplierState.includeZero ? "" : "secondary"} small" id="ss-toggle-zero">${spendSupplierState.includeZero ? "✓ " : ""}Include suppliers with no invoices</button>
      </div>
      <div class="filters">
        <button class="btn secondary" id="ss-export">Export to CSV</button>
      </div>
    </div>
    <div class="hint">${rows.length.toLocaleString()} supplier${rows.length === 1 ? "" : "s"}${spendSupplierState.year ? ` · ${esc(spendSupplierState.year)}` : ""}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>
        <th data-key="supplier_name">Supplier</th>
        <th data-key="invoice_count" class="num">Invoices</th>
        <th data-key="total_spend" class="num">Total Spend</th>
        <th data-key="last_invoice_date">Last Invoice</th>
      </tr></thead>
      <tbody>
        ${pageRows.length ? pageRows.map(r => `
          <tr>
            <td><b>${esc(r.supplier_name)}</b></td>
            <td class="num">${r.invoice_count.toLocaleString()}</td>
            <td class="num">${fmtMoney(r.total_spend)}</td>
            <td>${r.last_invoice_date ? fmtDate(r.last_invoice_date) : "—"}</td>
          </tr>`).join("") : `<tr><td colspan="4"><div class="empty-state">No suppliers match.</div></td></tr>`}
      </tbody>
    </table></div>
    ${renderPager(spendSupplierState.page, totalPages, "ss-page")}
  `;
  wireReportTypeToolbar();
  document.getElementById("ss-search").addEventListener("input", debounce(e => { spendSupplierState.search = e.target.value; spendSupplierState.page = 1; debouncedSearchRerender(e.target, renderSpendBySupplierReport); }, 400));
  document.getElementById("ss-year-filter").addEventListener("change", e => { spendSupplierState.year = e.target.value; spendSupplierState.page = 1; renderSpendBySupplierReport(); });
  document.getElementById("ss-toggle-zero").addEventListener("click", () => {
    spendSupplierState.includeZero = !spendSupplierState.includeZero;
    spendSupplierState.page = 1;
    renderSpendBySupplierReport();
  });
  document.getElementById("ss-export").addEventListener("click", () => {
    const exportParams = new URLSearchParams();
    if (spendSupplierState.year) exportParams.set("year", spendSupplierState.year);
    if (spendSupplierState.includeZero) exportParams.set("include_zero", "1");
    window.location.href = "/api/reporting/spend-by-supplier/export?" + exportParams.toString();
  });
  wirePager("ss-page", spendSupplierState.page, totalPages, p => { spendSupplierState.page = p; renderSpendBySupplierReport(); });
  root.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.key;
    spendSupplierState.sortDir = spendSupplierState.sortKey === key ? -spendSupplierState.sortDir : 1;
    spendSupplierState.sortKey = key;
    spendSupplierState.page = 1;
    renderSpendBySupplierReport();
  }));
}

/* ----------------------------------------------------------------------
   Hours Booked: hours from Labour Entries by month, grouped by employee,
   job, customer or contract code.
   ---------------------------------------------------------------------- */
let hoursBookedState = { year: "", group: "employee", employee: "", customer: "", contract_code: "" };
const HOURS_GROUP_LABELS = { employee: "Employee", job: "Job", customer: "Customer", contract_code: "Contract code" };

function hoursBookedParams() {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(hoursBookedState)) if (v) params.set(k, v);
  return params;
}

async function renderHoursBookedReport() {
  const root = document.getElementById("view-root");
  root.innerHTML = reportTypeToolbarHtml("hours-booked") + `<div class="empty-state">Loading report…</div>`;
  wireReportTypeToolbar();
  const data = await api("/api/reporting/hours-booked?" + hoursBookedParams().toString());
  hoursBookedState.year = data.year;
  const customerOptions = (state.ref.customers || []).map(c => c.customer_name).sort();
  const codeOptions = [...new Set((state.ref.works_orders || []).map(w => w.contract_code).filter(Boolean))].sort();
  const kpi = (label, value) => `<div class="kpi-card"><div class="label">${label}</div><div class="value">${value}</div></div>`;
  const h = v => (v ? Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 }) : "");
  const sel = (id, label, options, current) => `<select id="${id}"><option value="">${label}</option>${options.map(o => `<option value="${esc(o)}" ${current === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;

  root.innerHTML = reportTypeToolbarHtml("hours-booked") + `
    <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr);">
      ${kpi("Total hours booked", h(data.totals.hours) || "0")}
      ${kpi("Jobs worked on", data.totals.jobs.toLocaleString())}
      ${kpi("Average hours per job", h(data.totals.avg_per_job) || "0")}
    </div>
    <div class="toolbar">
      <div class="filters">
        <select id="hb-year">${data.years.map(y => `<option value="${esc(y)}" ${data.year === y ? "selected" : ""}>${esc(y)}</option>`).join("")}</select>
        <select id="hb-group">${Object.entries(HOURS_GROUP_LABELS).map(([k, l]) => `<option value="${k}" ${data.group === k ? "selected" : ""}>Group by ${l.toLowerCase()}</option>`).join("")}</select>
        ${sel("hb-employee", "All employees", data.employees, hoursBookedState.employee)}
        ${sel("hb-customer", "All customers", customerOptions, hoursBookedState.customer)}
        ${sel("hb-code", "All contract codes", codeOptions, hoursBookedState.contract_code)}
      </div>
      <div class="filters"><button class="btn secondary" id="hb-export">Export to CSV</button></div>
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>${HOURS_GROUP_LABELS[data.group]}</th>${[1,2,3,4,5,6,7,8,9,10,11,12].map(m => `<th class="num">${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][m-1]}</th>`).join("")}<th class="num">Total hours</th></tr></thead>
      <tbody>
        ${data.rows.length ? data.rows.map(r => `<tr><td>${esc(r.name)}</td>${r.months.map(v => `<td class="num">${h(v)}</td>`).join("")}<td class="num"><b>${h(r.total)}</b></td></tr>`).join("")
          + `<tr class="subtotal-row"><td><b>Total</b></td>${[0,1,2,3,4,5,6,7,8,9,10,11].map(i => `<td class="num"><b>${h(data.rows.reduce((a, r) => a + r.months[i], 0))}</b></td>`).join("")}<td class="num"><b>${h(data.totals.hours)}</b></td></tr>`
          : `<tr><td colspan="14"><div class="empty-state">No hours booked for ${esc(data.year)} with these filters.</div></td></tr>`}
      </tbody>
    </table></div>`;
  wireReportTypeToolbar();
  const bind = (id, key) => document.getElementById(id).addEventListener("change", e => { hoursBookedState[key] = e.target.value; renderHoursBookedReport(); });
  bind("hb-year", "year"); bind("hb-group", "group"); bind("hb-employee", "employee"); bind("hb-customer", "customer"); bind("hb-code", "contract_code");
  document.getElementById("hb-export").addEventListener("click", () => {
    window.location.href = "/api/reporting/hours-booked/export?" + hoursBookedParams().toString();
  });
}

/* ----------------------------------------------------------------------
   Monthly Income & Spend
   ---------------------------------------------------------------------- */
async function renderMonthlyIncomeSpendReport() {
  const root = document.getElementById("view-root");
  root.innerHTML = reportTypeToolbarHtml("monthly") + `<div class="empty-state">Loading report…</div>`;
  wireReportTypeToolbar();
  const params = new URLSearchParams();
  if (monthlyIncomeSpendState.year) params.set("year", monthlyIncomeSpendState.year);
  const data = await api("/api/reporting/monthly-income-spend?" + params.toString());
  // The server picks a fallback year (e.g. the current one) when none was
  // requested or the requested one has no data - keep local state in sync
  // with whatever it actually resolved to, so the dropdown and future
  // requests agree with what's on screen.
  monthlyIncomeSpendState.year = data.year;

  const kpi = (label, value, cls = "") => `<div class="kpi-card ${cls}"><div class="label">${label}</div><div class="value">${value}</div></div>`;

  root.innerHTML = reportTypeToolbarHtml("monthly") + `
    <div class="kpi-grid">
      ${kpi("Income", fmtMoney(data.totals.income))}
      ${kpi("Spend", fmtMoney(data.totals.spend))}
      ${kpi("Net", fmtMoney(data.totals.net), data.totals.net >= 0 ? "green" : "red")}
    </div>
    <div class="toolbar">
      <div class="filters">
        <select id="mis-year-filter">
          ${data.years.map(y => `<option value="${esc(y)}" ${data.year === y ? "selected" : ""}>${esc(y)}</option>`).join("")}
        </select>
      </div>
      <div class="filters">
        <button class="btn secondary" id="mis-export">Export to CSV</button>
      </div>
    </div>
    <div class="chart-grid">
      <div class="panel wide">
        <h3>Income, Spend &amp; Net — ${esc(data.year)}</h3>
        <canvas id="chart-monthly-income-spend"></canvas>
      </div>
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Month</th><th class="num">Income</th><th class="num">Spend</th><th class="num">Net</th></tr></thead>
      <tbody>
        ${data.months.length ? data.months.map(m => `
          <tr>
            <td>${fmtMonthLabel(m.month)}</td>
            <td class="num">${fmtMoney(m.income)}</td>
            <td class="num">${fmtMoney(m.spend)}</td>
            <td class="num" ${m.net < 0 ? `style="color:var(--red);"` : ""}>${fmtMoney(m.net)}</td>
          </tr>`).join("") : `<tr><td colspan="4"><div class="empty-state">No data for ${esc(data.year)}.</div></td></tr>`}
        ${data.months.length ? `
          <tr class="subtotal-row">
            <td><b>Total</b></td>
            <td class="num"><b>${fmtMoney(data.totals.income)}</b></td>
            <td class="num"><b>${fmtMoney(data.totals.spend)}</b></td>
            <td class="num"><b>${fmtMoney(data.totals.net)}</b></td>
          </tr>` : ""}
      </tbody>
    </table></div>
  `;
  wireReportTypeToolbar();
  document.getElementById("mis-year-filter").addEventListener("change", e => { monthlyIncomeSpendState.year = e.target.value; renderMonthlyIncomeSpendReport(); });
  document.getElementById("mis-export").addEventListener("click", () => {
    window.location.href = "/api/reporting/monthly-income-spend/export?year=" + encodeURIComponent(data.year);
  });
  drawChart("chart-monthly-income-spend", "line", data.months.map(m => fmtMonthLabel(m.month)), [
    { label: "Income (£)", data: data.months.map(m => m.income), borderColor: "#2563eb", backgroundColor: "rgba(37,99,235,.12)", tension: .3 },
    { label: "Spend (£)", data: data.months.map(m => m.spend), borderColor: "#b45309", backgroundColor: "rgba(180,83,9,.12)", tension: .3 },
    { label: "Net (£)", data: data.months.map(m => m.net), borderColor: "#15803d", backgroundColor: "rgba(21,128,61,.12)", tension: .3 },
  ]);
}

/* ----------------------------------------------------------------------
   Income by Contract Code (filterable by Project Manager)
   ---------------------------------------------------------------------- */
function aggregateIncomeByContractCode(rows) {
  const map = new Map();
  for (const r of rows) {
    const info = contractCodeInfo(r.contract_code);
    const key = info.display;
    if (!map.has(key)) {
      map.set(key, { contract_code: key, label: info.label, jobs: 0, invoice_count: 0, revenue: 0 });
    }
    const agg = map.get(key);
    agg.jobs += 1;
    agg.invoice_count += r.invoice_count;
    agg.revenue += r.revenue;
  }
  return Array.from(map.values());
}

async function renderIncomeByContractCodeReport() {
  const root = document.getElementById("view-root");
  root.innerHTML = reportTypeToolbarHtml("income-by-code") + `<div class="empty-state">Loading report…</div>`;
  wireReportTypeToolbar();
  const data = await api("/api/reporting/income-by-contract-code");

  const customerOptions = state.ref.customers.map(c => c.customer_name).sort();
  const codeOptionsSet = new Set(data.rows.map(r => r.contract_code).filter(Boolean));
  const codeOptions = [...codeOptionsSet].sort();

  let rows = data.rows;
  if (incomeByCodeState.search) {
    const q = incomeByCodeState.search.toLowerCase();
    rows = rows.filter(r =>
      r.job_number.toLowerCase().includes(q) ||
      r.customer_name.toLowerCase().includes(q) ||
      (r.contract_code || "").toLowerCase().includes(q)
    );
  }
  if (incomeByCodeState.personFilter) rows = rows.filter(r => r.project_manager === incomeByCodeState.personFilter);
  if (incomeByCodeState.customerFilter) rows = rows.filter(r => r.customer_name === incomeByCodeState.customerFilter);
  if (incomeByCodeState.codeFilter) rows = rows.filter(r => (r.contract_code || "") === incomeByCodeState.codeFilter);

  const agg = aggregateIncomeByContractCode(rows);
  agg.sort((a, b) => {
    const av = a[incomeByCodeState.sortKey], bv = b[incomeByCodeState.sortKey];
    const cmp = typeof av === "number" ? av - bv : String(av ?? "").localeCompare(String(bv ?? ""));
    return cmp * incomeByCodeState.sortDir;
  });

  const totals = agg.reduce((acc, r) => {
    acc.revenue += r.revenue;
    acc.invoice_count += r.invoice_count;
    acc.jobs += r.jobs;
    return acc;
  }, { revenue: 0, invoice_count: 0, jobs: 0 });

  const totalPages = Math.max(1, Math.ceil(agg.length / PAGE_SIZE));
  incomeByCodeState.page = Math.min(Math.max(1, incomeByCodeState.page), totalPages);
  const pageRows = agg.slice((incomeByCodeState.page - 1) * PAGE_SIZE, incomeByCodeState.page * PAGE_SIZE);

  const kpi = (label, value, cls = "") => `<div class="kpi-card ${cls}"><div class="label">${label}</div><div class="value">${value}</div></div>`;

  root.innerHTML = reportTypeToolbarHtml("income-by-code") + `
    <div class="kpi-grid">
      ${kpi("Total Income", fmtMoney(totals.revenue))}
      ${kpi("Jobs", totals.jobs.toLocaleString())}
      ${kpi("Invoices", totals.invoice_count.toLocaleString())}
    </div>
    <div class="toolbar">
      <div class="filters">
        <input type="text" id="ibc-search" placeholder="Search job, customer or contract code…" value="${esc(incomeByCodeState.search)}">
        <select id="ibc-person-filter">
          <option value="">All project managers</option>
          ${data.project_managers.map(p => `<option value="${esc(p)}" ${incomeByCodeState.personFilter === p ? "selected" : ""}>${esc(p)}</option>`).join("")}
        </select>
        <select id="ibc-customer-filter">
          <option value="">All customers</option>
          ${customerOptions.map(name => `<option value="${esc(name)}" ${incomeByCodeState.customerFilter === name ? "selected" : ""}>${esc(name)}</option>`).join("")}
        </select>
        <select id="ibc-code-filter">
          <option value="">All contract codes</option>
          ${codeOptions.map(code => `<option value="${esc(code)}" ${incomeByCodeState.codeFilter === code ? "selected" : ""}>${esc(contractCodeInfo(code).display)}</option>`).join("")}
        </select>
      </div>
      <div class="filters">
        <button class="btn secondary" id="ibc-export">Export to CSV</button>
      </div>
    </div>
    <div class="hint">${agg.length.toLocaleString()} contract code${agg.length === 1 ? "" : "s"} from ${rows.length.toLocaleString()} job${rows.length === 1 ? "" : "s"}${incomeByCodeState.personFilter ? ` · ${esc(incomeByCodeState.personFilter)}` : ""}${incomeByCodeState.customerFilter ? ` · ${esc(incomeByCodeState.customerFilter)}` : ""}${incomeByCodeState.codeFilter ? ` · ${esc(contractCodeInfo(incomeByCodeState.codeFilter).display)}` : ""}</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>
        <th data-key="contract_code">Code</th>
        <th data-key="label">Description</th>
        <th data-key="jobs" class="num">Jobs</th>
        <th data-key="invoice_count" class="num">Invoices</th>
        <th data-key="revenue" class="num">Income</th>
      </tr></thead>
      <tbody>
        ${pageRows.length ? pageRows.map(r => `
          <tr>
            <td><b>${esc(r.contract_code)}</b></td>
            <td>${esc(r.label || "")}</td>
            <td class="num">${r.jobs.toLocaleString()}</td>
            <td class="num">${r.invoice_count.toLocaleString()}</td>
            <td class="num">${fmtMoney(r.revenue)}</td>
          </tr>`).join("") : `<tr><td colspan="5"><div class="empty-state">No data matches.</div></td></tr>`}
      </tbody>
    </table></div>
    ${renderPager(incomeByCodeState.page, totalPages, "ibc-page")}
  `;
  wireReportTypeToolbar();
  document.getElementById("ibc-search").addEventListener("input", debounce(e => { incomeByCodeState.search = e.target.value; incomeByCodeState.page = 1; debouncedSearchRerender(e.target, renderIncomeByContractCodeReport); }, 400));
  document.getElementById("ibc-person-filter").addEventListener("change", e => { incomeByCodeState.personFilter = e.target.value; incomeByCodeState.page = 1; renderIncomeByContractCodeReport(); });
  document.getElementById("ibc-customer-filter").addEventListener("change", e => { incomeByCodeState.customerFilter = e.target.value; incomeByCodeState.page = 1; renderIncomeByContractCodeReport(); });
  document.getElementById("ibc-code-filter").addEventListener("change", e => { incomeByCodeState.codeFilter = e.target.value; incomeByCodeState.page = 1; renderIncomeByContractCodeReport(); });
  document.getElementById("ibc-export").addEventListener("click", () => {
    const headers = ["Contract Code", "Description", "Jobs", "Invoices", "Income"];
    const csvRows = agg.map(r => [r.contract_code, r.label, r.jobs, r.invoice_count, r.revenue.toFixed(2)]);
    downloadCSV("income_by_contract_code.csv", headers, csvRows);
  });
  wirePager("ibc-page", incomeByCodeState.page, totalPages, p => { incomeByCodeState.page = p; renderIncomeByContractCodeReport(); });
  root.querySelectorAll("th[data-key]").forEach(th => th.addEventListener("click", () => {
    const key = th.dataset.key;
    incomeByCodeState.sortDir = incomeByCodeState.sortKey === key ? -incomeByCodeState.sortDir : 1;
    incomeByCodeState.sortKey = key;
    incomeByCodeState.page = 1;
    renderIncomeByContractCodeReport();
  }));
}

/* ======================================================================
   USERS (Administrator only)
   ====================================================================== */
async function renderUsers() {
  const root = document.getElementById("view-root");
  if (!isAdmin()) { root.innerHTML = `<div class="empty-state">You don't have permission to view this page.</div>`; return; }
  root.innerHTML = `<div class="empty-state">Loading users…</div>`;
  const rows = await api("/api/users");
  root.innerHTML = `
    <div class="toolbar">
      <div class="hint">Manage user accounts and roles.</div>
      <button class="btn small secondary" id="login-activity-btn">Login Activity</button>
      <button class="btn small secondary" id="document-access-btn">Document Access</button>
      <button class="btn" id="user-new">+ New User</button>
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Full Name</th><th>Username</th><th>Role</th><th>Email</th><th>Created</th><th>Password</th><th>2FA</th><th></th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td><b>${esc(r.full_name)}</b></td>
            <td>${esc(r.username)}</td>
            <td><span class="badge blue">${esc(r.role)}</span></td>
            <td>${esc(r.email || "")}</td>
            <td>${fmtDate(r.created_date)}</td>
            <td>${r.must_change_password ? lightBadge("amber", "Awaiting change") : lightBadge("green", "Set")}</td>
            <td>${r.totp_enabled ? lightBadge("green", "Enabled") : lightBadge("amber", "Not set up")}</td>
            <td class="actions-cell">
              <button class="btn small secondary" data-edit="${r.user_id}">Edit</button>
              ${r.totp_enabled ? `<button class="btn small secondary" data-reset-2fa="${r.user_id}">Reset 2FA</button>` : ""}
              ${r.user_id !== state.user.user_id ? `<button class="btn small danger" data-del="${r.user_id}">Delete</button>` : ""}
            </td>
          </tr>`).join("")}
      </tbody>
    </table></div>
  `;
  document.getElementById("user-new").addEventListener("click", () => openUserForm());
  document.getElementById("login-activity-btn").addEventListener("click", () => showLoginActivity());
  document.getElementById("document-access-btn").addEventListener("click", () => showDocumentAccessLog());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () => {
    openUserEditForm(rows.find(r => String(r.user_id) === b.dataset.edit));
  }));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Delete this user?", "/api/users/" + b.dataset.del, renderUsers)
  ));
  root.querySelectorAll("[data-reset-2fa]").forEach(b => b.addEventListener("click", () => {
    confirmResetTfa(rows.find(row => String(row.user_id) === b.getAttribute("data-reset-2fa")));
  }));
}

async function confirmResetTfa(user) {
  openModal("Reset Two-Factor Authentication", `
    <p>Reset two-factor authentication for <b>${esc(user.full_name)}</b>? They'll be asked to set it up again -
      with a new setup key and new backup codes - the next time they sign in. Use this if they've lost their
      phone and used up (or never saved) their backup codes.</p>
  `, {
    submitLabel: "Reset 2FA",
    onSubmit: async () => {
      await api(`/api/users/${user.user_id}/reset-2fa`, { method: "POST" });
      closeModal();
      renderUsers();
    },
  });
}

// Durable record of every PDF download, CSV export, and Goods In document
// download (who, what document, when), backed by /api/document-access-log
// - see log_document_access() in app.py. Distinct from Login Activity (who
// got in) and each record's own Change History (what field values changed).
async function showDocumentAccessLog() {
  let rows = [];
  try { rows = await api("/api/document-access-log"); } catch (e) { /* show as empty below */ }
  const body = rows.length ? `
    <div class="hint" style="margin-bottom:10px;">Most recent ${rows.length} document access(es), newest first.</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Time</th><th>User</th><th>Document</th><th>Reference</th><th>IP Address</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${fmtDateTime(r.access_date)}</td>
          <td>${esc(r.username)}</td>
          <td>${esc(r.document_type)}</td>
          <td>${esc(r.reference || "—")}</td>
          <td>${esc(r.ip_address || "—")}</td>
        </tr>`).join("")}</tbody>
    </table></div>
  ` : `<div class="hint">No document downloads or exports recorded yet.</div>`;
  openModal("Document Access", body, { showFooter: false });
}

// Durable login audit trail (success / failure / lockout), backed by
// /api/login-events - see log_login_event() in app.py. Distinct from the
// in-memory throttling that actually locks accounts out; this is purely
// the after-the-fact record of it, for investigating e.g. a run of failed
// attempts against a particular account.
async function showLoginActivity() {
  let rows = [];
  try { rows = await api("/api/login-events"); } catch (e) { /* show as empty below */ }
  const RESULT_BADGE = {
    success: lightBadge("green", "Success"),
    failure: lightBadge("red", "Failed"),
    lockout: lightBadge("amber", "Locked out"),
  };
  const body = rows.length ? `
    <div class="hint" style="margin-bottom:10px;">Most recent ${rows.length} login attempt(s), newest first.</div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr><th>Time</th><th>Username</th><th>IP Address</th><th>Result</th></tr></thead>
      <tbody>${rows.map(r => `
        <tr>
          <td>${fmtDateTime(r.event_date)}</td>
          <td>${esc(r.username)}</td>
          <td>${esc(r.ip_address || "—")}</td>
          <td>${RESULT_BADGE[r.event_type] || esc(r.event_type)}</td>
        </tr>`).join("")}</tbody>
    </table></div>
  ` : `<div class="hint">No login activity recorded yet.</div>`;
  openModal("Login Activity", body, { showFooter: false });
}

function openUserForm() {
  const roles = ["Administrator", "Project Manager", "Procurement", "Finance", "Executive", "Service Engineer"];
  const body = `
    <div class="form-grid-2">
      <div class="form-row"><label>Full Name</label><input name="full_name" required></div>
      <div class="form-row"><label>Username</label><input name="username" required></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Role</label><select name="role">${roles.map(r => `<option>${r}</option>`).join("")}</select></div>
      <div class="form-row"><label>Email</label><input type="email" name="email"></div>
    </div>
    <div class="form-row"><label>Temporary Password (optional)</label><input type="text" name="password" placeholder="Leave blank to generate a secure one-time password">
      <div class="hint" style="margin-top:6px;">Whatever's set here (or generated), the new user will be asked to choose their own password the first time they sign in.</div>
    </div>
  `;
  openModal("New User", body, {
    submitLabel: "Create User",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      if (!payload.password) delete payload.password;
      const result = await api("/api/users", { method: "POST", body: payload });
      closeModal();
      renderUsers();
      if (result.temporary_password) showTemporaryPassword(payload.full_name, result.temporary_password);
    },
  });
}

function showTemporaryPassword(fullName, password) {
  openModal("One-Time Password Generated", `
    <p>Share this with <b>${esc(fullName)}</b> so they can sign in for the first time. It can't be shown again -
      if it's lost, reset it from the Edit button on their account.</p>
    <div class="form-row"><input type="text" readonly value="${esc(password)}" style="font-family:monospace; font-size:15px; text-align:center;"></div>
    <p class="hint">They'll be asked to set their own password immediately after signing in with this one.</p>
  `, { showFooter: false });
}

function openUserEditForm(u) {
  const roles = ["Administrator", "Project Manager", "Procurement", "Finance", "Executive", "Service Engineer"];
  // Full name and username are locked - not editable once the account exists.
  const body = `
    <div class="form-grid-2">
      <div class="form-row"><label>Full Name</label><input value="${esc(u.full_name)}" disabled></div>
      <div class="form-row"><label>Username</label><input value="${esc(u.username)}" disabled></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Role</label>
        <select name="role">${roles.map(r => `<option ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}</select>
      </div>
      <div class="form-row"><label>Email</label><input type="email" name="email" value="${esc(u.email || "")}"></div>
    </div>
    <div class="form-row"><label>New Password</label><input type="text" name="password" placeholder="Leave blank to keep current password"></div>
  `;
  openModal(`Edit User — ${u.full_name}`, body, {
    submitLabel: "Save Changes",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      if (!payload.password) delete payload.password;
      await api("/api/users/" + u.user_id, { method: "PUT", body: payload });
      closeModal();
      renderUsers();
    },
  });
}

/* ======================================================================
   SETTINGS (Administrator only)
   ====================================================================== */
async function renderSettings() {
  const root = document.getElementById("view-root");
  if (!isAdmin()) { root.innerHTML = `<div class="empty-state">You don't have permission to view this page.</div>`; return; }
  root.innerHTML = `<div class="empty-state">Loading settings…</div>`;
  const [data, shipToAddresses] = await Promise.all([
    api("/api/settings/margin-targets"),
    api("/api/settings/ship-to-addresses"),
  ]);

  root.innerHTML = `
    <div class="panel" style="margin-bottom:20px;max-width:420px;">
      <h3>Default Margin Target</h3>
      <div class="hint">Used for every job whose contract code doesn't have its own target below — drives the Margin Risk alert and the red/amber/green colouring throughout the app.</div>
      <div class="form-row" style="margin-top:10px;">
        <label>Target %</label>
        <input type="number" step="0.1" min="0" max="100" id="default-target-input" value="${data.default_target}">
      </div>
      <button class="btn" id="default-target-save">Save</button>
      <span class="form-error" id="default-target-error"></span>
    </div>

    <div class="toolbar">
      <div class="hint" style="margin:0;">Per-contract-code overrides — a code listed here uses its own target instead of the default above.</div>
      <button class="btn" id="target-new">+ Add Target</button>
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>
        <th>Code</th>
        <th>Description</th>
        <th class="num">Jobs</th>
        <th class="num">Target %</th>
        <th class="num">Standard Value</th>
        <th class="num">Standard Cost</th>
        <th></th>
      </tr></thead>
      <tbody>
        ${data.contract_code_targets.length ? data.contract_code_targets.map(t => `
          <tr>
            <td><b>${esc(t.contract_code)}</b></td>
            <td>${esc(contractCodeInfo(t.contract_code).label || "")}</td>
            <td class="num">${t.job_count.toLocaleString()}</td>
            <td class="num">${fmtPct(t.target_pct)}</td>
            <td class="num">${t.standard_value != null ? fmtMoney(t.standard_value) : "—"}</td>
            <td class="num">${t.standard_cost != null ? fmtMoney(t.standard_cost) : "—"}</td>
            <td class="actions-cell">
              <button class="btn small secondary" data-edit="${esc(t.contract_code)}" data-pct="${t.target_pct}" data-value="${t.standard_value ?? ""}" data-cost="${t.standard_cost ?? ""}">Edit</button>
              <button class="btn small danger" data-del="${esc(t.contract_code)}">Remove</button>
            </td>
          </tr>`).join("") : `<tr><td colspan="7"><div class="empty-state">No overrides set — every job uses the default target above.</div></td></tr>`}
      </tbody>
    </table></div>

    <div class="toolbar" style="margin-top:28px;">
      <div class="hint" style="margin:0;">Ship To (delivery) addresses — available on the Purchase Order form. Add every site's address here as the business provides them.</div>
      <button class="btn" id="shipto-new">+ Add Address</button>
    </div>
    <div class="table-wrap"><table class="data-table">
      <thead><tr>
        <th>Label</th>
        <th>Company</th>
        <th>Address</th>
        <th>Postcode</th>
        <th></th>
      </tr></thead>
      <tbody>
        ${shipToAddresses.length ? shipToAddresses.map(a => `
          <tr>
            <td><b>${esc(a.label)}</b></td>
            <td>${esc(a.company_name || "")}</td>
            <td>${esc([a.address_line1, a.address_line2, a.city].filter(Boolean).join(", "))}</td>
            <td>${esc(a.postcode || "")}</td>
            <td class="actions-cell">
              <button class="btn small secondary" data-edit-addr="${a.ship_to_id}">Edit</button>
              <button class="btn small danger" data-del-addr="${a.ship_to_id}">Remove</button>
            </td>
          </tr>`).join("") : `<tr><td colspan="5"><div class="empty-state">No delivery addresses yet.</div></td></tr>`}
      </tbody>
    </table></div>

    <div class="panel" style="margin-top:28px;max-width:640px;border-color:var(--red-300,#f5c2c2);">
      <h3>Go-Live: Reset Job Data</h3>
      <div class="hint">
        Permanently deletes every Quote, Works Order, Purchase Order, Goods In record (and its
        uploaded documents), Despatch Note, Supplier Invoice, Customer Invoice, and Labour Entry —
        plus any stock currently allocated to one of those jobs. Customers, Suppliers, Contract
        Codes, the Rate Card, these Settings, and every user account are left exactly as they are,
        and nothing is removed from Login Activity or Document Access. A backup of the current
        database is taken automatically first. <b>This cannot be undone from within the app</b> —
        use it only when you're ready to start inputting real jobs from a completely clean slate.
      </div>
      <button class="btn danger" id="reset-job-data-btn" style="margin-top:10px;">Reset Job Data…</button>
    </div>
  `;

  document.getElementById("default-target-save").addEventListener("click", async () => {
    const errEl = document.getElementById("default-target-error");
    errEl.textContent = "";
    const val = document.getElementById("default-target-input").value;
    try {
      await api("/api/settings/margin-targets/default", { method: "PUT", body: { target_pct: Number(val) } });
      renderSettings();
    } catch (err) {
      errEl.textContent = err.message;
    }
  });
  document.getElementById("target-new").addEventListener("click", () => openTargetForm());
  root.querySelectorAll("[data-edit]").forEach(b => b.addEventListener("click", () =>
    openTargetForm({ contract_code: b.dataset.edit, target_pct: Number(b.dataset.pct), standard_value: b.dataset.value, standard_cost: b.dataset.cost })
  ));
  root.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", () =>
    handleDelete(`Remove the target for ${b.dataset.del}? Those jobs will fall back to the default.`, "/api/settings/margin-targets/" + b.dataset.del, renderSettings)
  ));

  document.getElementById("shipto-new").addEventListener("click", () => openShipToForm());
  root.querySelectorAll("[data-edit-addr]").forEach(b => b.addEventListener("click", () =>
    openShipToForm(shipToAddresses.find(a => String(a.ship_to_id) === b.dataset.editAddr))
  ));
  root.querySelectorAll("[data-del-addr]").forEach(b => b.addEventListener("click", () =>
    handleDelete("Remove this delivery address?", "/api/settings/ship-to-addresses/" + b.dataset.delAddr, renderSettings)
  ));

  document.getElementById("reset-job-data-btn").addEventListener("click", openResetJobDataForm);
}

const RESET_JOB_DATA_CONFIRMATION = "RESET JOB DATA";

function openResetJobDataForm() {
  const body = `
    <div class="form-warning">
      This permanently deletes every Quote, Works Order, Purchase Order, Goods In record and its
      documents, Despatch Note, Supplier Invoice, Customer Invoice, and Labour Entry — everything
      currently in the app. A backup is taken first, but there's no "undo" button in the app
      itself. Only do this once you're ready to start inputting real jobs from scratch.
    </div>
    <div class="form-row">
      <label>Type <code>${esc(RESET_JOB_DATA_CONFIRMATION)}</code> to confirm</label>
      <input name="confirm" autocomplete="off" required>
    </div>
  `;
  openModal("Reset Job Data", body, {
    submitLabel: "Permanently Reset",
    onSubmit: async (fd) => {
      const typed = (fd.get("confirm") || "").trim();
      if (typed !== RESET_JOB_DATA_CONFIRMATION) {
        throw new Error(`Type "${RESET_JOB_DATA_CONFIRMATION}" exactly (all capitals) to confirm.`);
      }
      const result = await api("/api/admin/reset-job-data", { method: "POST", body: { confirm: typed } });
      closeModal();
      const lines = Object.entries(result.deleted)
        .map(([table, n]) => `${n.toLocaleString()} ${table.replace(/_/g, " ")}`)
        .join(", ");
      alert(`Done. A backup was taken first. Deleted: ${lines}.`);
      state.ref = await api("/api/reference");
      try {
        const alertsResp = await api("/api/alerts");
        state.alerts = alertsResp.alerts;
      } catch (e) { state.alerts = []; }
      renderTopbarAlerts();
      renderSettings();
    },
  });
}

function openShipToForm(existing = null) {
  const body = `
    <div class="form-row"><label>Label</label><input name="label" value="${esc(existing?.label || "")}" placeholder="e.g. Derby" required></div>
    <div class="form-row"><label>Company Name</label><input name="company_name" value="${esc(existing?.company_name ?? "Total Engineering Asset Management Limited")}"></div>
    <div class="form-grid-2">
      <div class="form-row"><label>Address Line 1</label><input name="address_line1" value="${esc(existing?.address_line1 || "")}"></div>
      <div class="form-row"><label>Address Line 2</label><input name="address_line2" value="${esc(existing?.address_line2 || "")}"></div>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>City</label><input name="city" value="${esc(existing?.city || "")}"></div>
      <div class="form-row"><label>Postcode</label><input name="postcode" value="${esc(existing?.postcode || "")}"></div>
    </div>
  `;
  openModal(existing ? `Edit Address — ${existing.label}` : "New Delivery Address", body, {
    submitLabel: "Save",
    onSubmit: async (fd) => {
      const payload = Object.fromEntries(fd.entries());
      if (existing) await api("/api/settings/ship-to-addresses/" + existing.ship_to_id, { method: "PUT", body: payload });
      else await api("/api/settings/ship-to-addresses", { method: "POST", body: payload });
      closeModal();
      renderSettings();
    },
  });
}

function openTargetForm(existing = null) {
  const body = `
    <div class="form-row"><label>Contract Code</label>
      <input name="contract_code" value="${esc(existing?.contract_code || "")}" ${existing ? "disabled" : ""} placeholder="e.g. INT" required>
    </div>
    <div class="form-row"><label>Target %</label>
      <input type="number" step="0.1" min="0" max="100" name="target_pct" value="${existing?.target_pct ?? ""}" required>
    </div>
    <div class="form-grid-2">
      <div class="form-row"><label>Standard Value (£)</label><input type="number" step="0.01" name="standard_value" value="${existing?.standard_value ?? ""}"></div>
      <div class="form-row"><label>Standard Cost (£)</label><input type="number" step="0.01" name="standard_cost" value="${existing?.standard_cost ?? ""}"></div>
    </div>
    <div class="hint">Optional. If both are set, Purchase Order Value and Budget Cost auto-fill with these figures the moment this code is picked on the New/Edit Works Order form — for a fixed-price type of job where both are always the same. Leave blank for a code that isn't fixed-price.</div>
  `;
  const form = openModal(existing ? `Edit Target — ${existing.contract_code}` : "New Contract Code Target", body, {
    submitLabel: "Save",
    onSubmit: async (fd) => {
      const code = existing ? existing.contract_code : fd.get("contract_code").trim().toUpperCase();
      if (!code) throw new Error("Contract code is required");
      await api("/api/settings/margin-targets/" + encodeURIComponent(code), {
        method: "PUT", body: {
          target_pct: Number(fd.get("target_pct")),
          standard_value: fd.get("standard_value") || null,
          standard_cost: fd.get("standard_cost") || null,
        },
      });
      state.ref = await api("/api/reference");
      closeModal();
      renderSettings();
    },
  });
}

/* ---------------------------------------------------------------------- */
bootstrap().finally(hideBootSplash);
