"""
Runs the app as a real shared server other people on the same network
can connect to, using waitress (a production-grade WSGI server) instead
of Flask's own development server - the one app.py uses for a single
person running it locally, and which explicitly isn't meant to serve
anyone beyond that (see the warning it prints on every startup).

Only the machine that runs this becomes the server. Everyone else just
opens a browser and goes to the address this prints - they don't need
Python, this folder, or a copy of the app on their own machine at all.

This is for a trusted local network only. There is no HTTPS here, so
traffic - including login passwords - travels unencrypted on the local
network. That's a normal, low-risk tradeoff on an office LAN behind its
own router/firewall; it is not safe to forward this machine's port to
the wider internet.

Run with:  python serve_shared.py
"""
import os
import socket
import sys

from app import app, run_startup_tasks


def get_lan_ip():
    """Best-effort discovery of this machine's own LAN IP address - the
    one other devices on the same network would actually use to reach
    it. Connecting a UDP socket doesn't send any data (UDP is
    connectionless); it's just a reliable, standard way to ask the OS
    which local interface/IP it would use to reach an outside address,
    without depending on hostname resolution, which can return the
    wrong thing (e.g. 127.0.1.1) on some machines. Falls back to
    127.0.0.1 - still correct for the host's own use, just not useful
    for anyone else - if the machine has no network route at all."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    if not run_startup_tasks():
        sys.exit(1)

    try:
        from waitress import serve
    except ImportError:
        sys.exit("waitress isn't installed - run: pip install -r requirements.txt")

    port = int(os.environ.get("FLASK_PORT", "5000"))
    threads = int(os.environ.get("WAITRESS_THREADS", "8"))
    lan_ip = get_lan_ip()

    is_test_copy = bool(os.environ.get("TEAMWORKS360_DATA_DIR"))
    print("=" * 70)
    if is_test_copy:
        print("TEAMworks360 - SHARED HOSTING MODE (TEST COPY - not the live system)")
        print(f"Data folder for this test copy: {os.environ['TEAMWORKS360_DATA_DIR']}")
    else:
        print("TEAMworks360 - SHARED HOSTING MODE")
    print("=" * 70)
    print()
    print("Everyone on this network can now use the app at:")
    print(f"    http://{lan_ip}:{port}")
    print()
    print("They just need that address in their own browser - no install,")
    print("no copy of this folder, nothing else needed on their end.")
    print()
    print("Keep this window open - closing it stops the app for everyone")
    print("currently using it.")
    print()
    print("This is for a trusted local network only: there is no HTTPS, so")
    print("do not forward this machine's port to the internet.")
    print()
    if os.environ.get("FLASK_HTTPS") != "1":
        print("Traffic on this network - including login passwords - travels")
        print("UNENCRYPTED right now. See README.md, \"Securing shared hosting")
        print("with HTTPS\", for a reverse-proxy setup that fixes this (and then")
        print("set FLASK_HTTPS=1 so session cookies are marked Secure too).")
        print()
    print("If a colleague can't connect but this machine can reach the app")
    print("fine itself, it's almost always this machine's firewall silently")
    print("blocking the connection - not a real network problem. On Windows,")
    print("start_shared_host.bat already tries to open this port through")
    print("Windows Firewall automatically; if that failed (it needs Admin")
    print("rights), run it once as Administrator, or add an inbound rule for")
    print(f"TCP port {port} yourself. On Mac, check System Settings -> Network ->")
    print("Firewall. Also worth checking: a VPN active on this machine can")
    print("make the address above wrong for anyone else on the office network.")
    print("=" * 70)

    if os.environ.get("NO_BROWSER") != "1":
        import threading
        import time
        import webbrowser

        def _open_browser():
            time.sleep(1.2)
            try:
                webbrowser.open(f"http://127.0.0.1:{port}")
            except Exception:
                pass  # headless environment or no browser configured - not fatal

        threading.Thread(target=_open_browser, daemon=True).start()

    serve(app, host="0.0.0.0", port=port, threads=threads)
