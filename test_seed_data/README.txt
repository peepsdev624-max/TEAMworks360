This jpm.db is a one-time snapshot of the real TEAMworks360 database,
uploaded by Simon on 2026-10-04 specifically to seed the shared test copy
(see start_test_shared_host.bat / start_test_shared_host.command, and the
main README's "Test copy (sandbox)" section).

It is used ONLY the first time the test launcher runs (when
~/TEAMworks360-Test does not yet have a database) - after that, the test
copy lives its own separate life and this file is never touched again.

To get a fresh snapshot of live data into the test copy later: replace
this file with a newer copy of the real jpm.db, delete the existing
~/TEAMworks360-Test folder, and run the test launcher again.

REFRESHED 2026-10-07: the 4 October snapshot was then brought up to date with
'1. Team Order Book 1st Jan - 31st Dec 2026' using update_order_book.py
(154 jobs added, 984 updated; 1,909 order rows in total).
