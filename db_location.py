"""
Single source of truth for where the database and session secret key live.
Imported by app.py, seed.py, and import_team_order_book.py so all three
always agree - never redefine these paths locally in another file.

The database lives OUTSIDE the app's own folder, in a stable location in
the user's home directory. This means installing an update - replacing
the whole app folder with a new one - never touches it, since the new
code looks in this same external place regardless of where the app
itself was unzipped. See README.md, "Deploying updates safely".
"""
import os
import shutil
import sys
import datetime

# This app was originally named "Job Profitability Manager" - OLD_DATA_DIR
# is where every existing install's data already lives, and
# migrate_from_old_product_name() below moves it into the new folder the
# first time a renamed copy runs, so the TEAMworks360 rename never loses
# anyone's existing database.
OLD_DATA_DIR = os.path.join(os.path.expanduser("~"), "JobProfitabilityManager")
# TEAMWORKS360_DATA_DIR overrides where the data folder lives, for a second,
# fully separate instance running alongside the real one - e.g. the shared
# test copy (see start_test_shared_host.bat/.command and the README's "Test
# copy (sandbox)" section). Unset, which is every normal single-user or
# shared-host install, this is exactly the one ~/TEAMworks360 folder it
# always was - nothing about the default install changes because this
# override exists.
DATA_DIR = os.environ.get("TEAMWORKS360_DATA_DIR") or os.path.join(os.path.expanduser("~"), "TEAMworks360")
DB_PATH = os.path.join(DATA_DIR, "jpm.db")
SECRET_KEY_PATH = os.path.join(DATA_DIR, ".secret_key")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
# Uploaded Goods In documents (delivery notes, packing slips, photos of
# goods received) - kept outside the app folder for the same reason the
# database is: replacing the app folder on an update must never lose
# them. Deliberately its own subfolder, not inside BACKUP_DIR, since
# these aren't backups and shouldn't be pruned the way backup_db() prunes
# old database snapshots.
GOODS_IN_DOCS_DIR = os.path.join(DATA_DIR, "goods_in_documents")
# Captured customer signatures (Despatch Notes) - same reasoning as
# GOODS_IN_DOCS_DIR above: kept outside the app folder so an app-folder
# update never loses one, and in its own subfolder since these are proof-
# of-delivery records, not backups.
SIGNATURES_DIR = os.path.join(DATA_DIR, "signatures")


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    os.makedirs(GOODS_IN_DOCS_DIR, exist_ok=True)
    os.makedirs(SIGNATURES_DIR, exist_ok=True)
    harden_file_permissions()


def harden_file_permissions():
    """Best-effort lockdown of the data directory and the files that
    matter most if this machine is ever shared with anyone else: the
    database (every job, cost and margin figure the business has) and
    the session secret key (anyone who reads it could forge a login as
    any user, including an Administrator, with no password at all).
    Restricts them to the OS account the app runs as.

    This is a real, meaningful restriction on Mac/Linux, where os.chmod
    maps directly onto standard Unix file permissions. On Windows -
    where most installs of this app actually run, via start_windows.bat
    - it does much less: Python's os.chmod there only toggles the
    read-only attribute and has no way to touch NTFS's own per-user
    access control list, so this call does not meaningfully stop another
    account on the same Windows machine from reading these files. On a
    shared Windows PC, the real protection is either a Windows user
    account only the right people can log into, or setting NTFS
    permissions on the TEAMworks360 folder by hand (right-click -
    Properties - Security) - see README.md, "File permissions and who
    else can read the database".

    Called on every startup (ensure_data_dir(), and again after every
    backup so a freshly-written backup file is covered too), and never
    allowed to stop the app running - a permissions error here (e.g. an
    unusual hosting setup where the app doesn't own these files) is
    silently skipped rather than crashing startup."""
    def _chmod(path, mode):
        try:
            if os.path.exists(path):
                os.chmod(path, mode)
        except OSError:
            pass

    _chmod(DATA_DIR, 0o700)
    _chmod(BACKUP_DIR, 0o700)
    _chmod(DB_PATH, 0o600)
    _chmod(SECRET_KEY_PATH, 0o600)
    _chmod(GOODS_IN_DOCS_DIR, 0o700)
    _chmod(SIGNATURES_DIR, 0o700)
    try:
        for name in os.listdir(BACKUP_DIR):
            _chmod(os.path.join(BACKUP_DIR, name), 0o600)
    except OSError:
        pass
    try:
        for name in os.listdir(GOODS_IN_DOCS_DIR):
            _chmod(os.path.join(GOODS_IN_DOCS_DIR, name), 0o600)
    except OSError:
        pass
    try:
        for name in os.listdir(SIGNATURES_DIR):
            _chmod(os.path.join(SIGNATURES_DIR, name), 0o600)
    except OSError:
        pass


def migrate_from_old_product_name():
    """One-time move of everything - database, secret key, backups - out of
    the old product name's data folder (~/JobProfitabilityManager, from
    before this app was renamed to TEAMworks360) and into the new one.
    Safe to call on every startup: once the new folder has its own
    database (either moved here or freshly created), this is a no-op
    forever after, and an install that never had the old folder at all
    (a brand new one) just skips straight past it."""
    if not os.path.isdir(OLD_DATA_DIR) or os.path.exists(DB_PATH):
        return
    ensure_data_dir()
    old_db = os.path.join(OLD_DATA_DIR, "jpm.db")
    if os.path.exists(old_db):
        shutil.move(old_db, DB_PATH)
        print(f"One-time move: found your existing database under this app's old name "
              f"(Job Profitability Manager) - moved it to {DB_PATH}")
    old_key = os.path.join(OLD_DATA_DIR, ".secret_key")
    if os.path.exists(old_key) and not os.path.exists(SECRET_KEY_PATH):
        # Carried across deliberately, not regenerated - keeping the same
        # secret key means nobody's already-open browser session is
        # invalidated by the rename itself.
        shutil.move(old_key, SECRET_KEY_PATH)
    old_backups = os.path.join(OLD_DATA_DIR, "backups")
    if os.path.isdir(old_backups):
        for name in os.listdir(old_backups):
            src, dest = os.path.join(old_backups, name), os.path.join(BACKUP_DIR, name)
            if os.path.isfile(src) and not os.path.exists(dest):
                shutil.move(src, dest)


def migrate_legacy_files(app_base_dir):
    """One-time move of a database (and secret key) still sitting next to
    the app code, from before this external-location change existed - so
    upgrading from an older version never loses anyone's existing data or
    forces everyone to log in again. Safe to call on every startup: once
    the legacy file is gone, this is a no-op forever after."""
    ensure_data_dir()
    legacy_db = os.path.join(app_base_dir, "jpm.db")
    if os.path.exists(legacy_db) and not os.path.exists(DB_PATH):
        shutil.move(legacy_db, DB_PATH)
        print(f"One-time move: found an existing database next to the app - moved it to {DB_PATH}")
    legacy_key = os.path.join(app_base_dir, ".secret_key")
    if os.path.exists(legacy_key) and not os.path.exists(SECRET_KEY_PATH):
        shutil.move(legacy_key, SECRET_KEY_PATH)


def backup_db(keep=20):
    """Copies the current database to a timestamped backup, then prunes
    backups beyond `keep`. Best-effort and silent-safe: a backup failure
    (e.g. a full disk) never blocks the app from starting."""
    ensure_data_dir()
    if not os.path.exists(DB_PATH):
        return
    try:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"jpm-{stamp}.db")
        shutil.copy2(DB_PATH, dest)
        backups = sorted(
            f for f in os.listdir(BACKUP_DIR) if f.startswith("jpm-") and f.endswith(".db")
        )
        for old in backups[:-keep]:
            os.remove(os.path.join(BACKUP_DIR, old))
        harden_file_permissions()
    except OSError as e:
        print(f"Warning: could not create a backup ({e}) - continuing anyway", file=sys.stderr)
