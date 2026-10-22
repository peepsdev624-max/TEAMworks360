#!/bin/bash
# Double-click this file to open TEAMworks360.
#
# This is the file everyone EXCEPT the shared host computer should use.
# It does NOT start a server, does NOT install anything, and does NOT
# create any database on this computer - it only opens your normal web
# browser to the shared copy that TEAM's host computer is already running,
# exactly the way everyone else sees it. Nothing entered in the app is
# ever stored on this computer.
#
# (First time on macOS: right-click -> Open, to get past the "unidentified
# developer" warning - you only need to do that once.)
set -e
cd "$(dirname "$0")"

CONFIG_FILE="shared_host_address.txt"

if [ -f "$CONFIG_FILE" ]; then
    HOST_URL=$(cat "$CONFIG_FILE" | tr -d '[:space:]')
else
    echo "======================================================"
    echo "  TEAMworks360 - one-time setup"
    echo "======================================================"
    echo
    echo "This computer hasn't been told where the shared app lives yet."
    echo "Ask whoever manages it for the address - it looks something like"
    echo "http://192.168.1.50:5000 - and enter it below. You'll only need"
    echo "to do this once on this computer."
    echo
    read -p "Shared app address: " HOST_URL
    case "$HOST_URL" in
        http://*|https://*) ;;
        *) HOST_URL="http://$HOST_URL" ;;
    esac
    echo "$HOST_URL" > "$CONFIG_FILE"
    echo
fi

echo "Opening $HOST_URL ..."
# `|| true` on each: a browser that fails to launch (nothing registered to
# handle it, misconfigured xdg-open, etc.) must never be allowed to abort
# this script under `set -e` before the troubleshooting note below prints.
if command -v open >/dev/null 2>&1; then
    open "$HOST_URL" || echo "Could not open a browser automatically - go to $HOST_URL manually."
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$HOST_URL" || echo "Could not open a browser automatically - go to $HOST_URL manually."
else
    echo "Could not open a browser automatically - go to $HOST_URL manually."
fi

echo
echo "If the page doesn't load: check the host computer is switched on,"
echo "and that you're on the same office network as it. If the address"
echo "has changed, delete the file called shared_host_address.txt in this"
echo "folder and run this again to enter the new one."
sleep 3
