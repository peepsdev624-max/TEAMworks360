"""
The one place the app's version lives. Change APP_VERSION (and the date) here
when a new release is built - nothing else needs editing. Format MAJOR.MINOR.PATCH:
  PATCH  small fixes
  MINOR  new features, database upgrades that happen automatically
  MAJOR  a change that needs care when updating
"""
import re

APP_VERSION = "1.2.0"
APP_BUILD_DATE = "2026-10-22"

# Shown on the opening splash screen and the sign-in screen.
APP_OWNER = "Simon Lee"
APP_LICENCE = "Trial"


def parse_version(text):
    """'1.2.10' -> (1, 2, 10). Anything unreadable -> None."""
    m = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def is_newer(candidate, current):
    """True only if both are valid versions and candidate is strictly newer."""
    a, b = parse_version(candidate), parse_version(current)
    return bool(a and b and a > b)
