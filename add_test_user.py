"""
Adds one dummy "tester" account (Administrator, so every page can be tried)
to a TEST COPY's database - used by start_colleague_test.bat/.command right
after seeding the demo data.

Refuses to run unless TEAMWORKS360_DATA_DIR is set, i.e. unless this really is
a separate test data folder (see db_location.py). That's deliberate: this
account has a published password, and must never be added to a real install
by accident. Safe to run more than once - it just leaves an existing account
alone.
"""
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db_location

USERNAME = "tester"
PASSWORD = "TestDrive2026"


def main():
    if not os.environ.get("TEAMWORKS360_DATA_DIR"):
        sys.exit("Refusing to add the dummy tester account to a normal install "
                 "(TEAMWORKS360_DATA_DIR is not set).")
    if not os.path.exists(db_location.DB_PATH):
        sys.exit("No database found - run seed.py first.")
    conn = sqlite3.connect(db_location.DB_PATH)
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (USERNAME,)).fetchone():
            print(f"Account '{USERNAME}' already exists - left as it was.")
            return
        conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role, email) VALUES (?,?,?,?,?)",
            (USERNAME, generate_password_hash(PASSWORD), "Test User", "Administrator", "tester@example.com"),
        )
        conn.commit()
        print(f"Created dummy account '{USERNAME}'.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
