#!/usr/bin/env python3
"""Generate private dashboard credentials; never print the password or hash.

Run with the application environment (PYTHONPATH=src .venv/bin/python).
The generated env fragment must be merged into deployment .env before restarting API.
"""

import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from monzo_backend.browser_auth import hash_password

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--origin", required=True)
parser.add_argument("--directory", type=Path, required=True)
args = parser.parse_args()
url = urlsplit(args.origin)
if (
    url.scheme != "https"
    or not url.hostname
    or url.path
    or url.query
    or url.fragment
    or url.username
):
    parser.error("Origin must be HTTPS without a path")
args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
login = args.directory / "dashboard-login.txt"
env = args.directory / "dashboard.env"
if login.exists() or env.exists():
    parser.error("Credential files already exist; move them aside explicitly before rotating")
password = secrets.token_urlsafe(32)
for path, content in [
    (
        login,
        f"Dashboard: {args.origin}\nPassword: {password}\n\nKeep this file private; do not commit or paste it into chat.\n",
    ),
    (
        env,
        f"MONZO_DASHBOARD_ORIGIN={args.origin}\nMONZO_DASHBOARD_PASSWORD_HASH={hash_password(password)}\n",
    ),
]:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
print(f"Private credentials created at {login}. No credentials were printed.")
