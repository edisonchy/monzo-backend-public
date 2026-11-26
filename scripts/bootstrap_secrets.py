"""Generate new deployment secrets without printing them. Never overwrites existing files."""

import argparse
import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
args = parser.parse_args()
args.directory.mkdir(parents=True, exist_ok=True)
names = [".env", ".env.database", ".env.migrate"]
if any((args.directory / n).exists() for n in names):
    raise SystemExit("Refusing to overwrite existing deployment secrets")
admin, app = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
database = f"POSTGRES_USER=postgres\nPOSTGRES_DB=monzo\nPOSTGRES_PASSWORD={admin}\nMONZO_APP_DB_PASSWORD={app}\n"
application = (
    f"MONZO_DATABASE_URL=postgresql+psycopg://monzo_app:{app}@postgres:5432/monzo\n"
    f"MONZO_OWNER_TOKEN={secrets.token_urlsafe(40)}\n"
    f"MONZO_AUTOMATION_TOKEN={secrets.token_urlsafe(40)}\n"
    f"MONZO_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n"
    f"MONZO_WEBHOOK_SECRET={secrets.token_urlsafe(40)}\n"
    "MONZO_CLIENT_ID=\nMONZO_CLIENT_SECRET=\n"
    "MONZO_REDIRECT_URI=http://localhost:8087/oauth/callback\n"
    "MONZO_PROXY_URL=http://cliproxyapi:8317/v1\nMONZO_PROXY_KEY=\nMONZO_MODEL=\n"
    "MONZO_AI_ENABLED=false\nMONZO_AI_AUTO_ACCEPT=false\nMONZO_SECURE_COOKIES=true\n"
)
migration = (
    f"MONZO_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:{admin}@postgres:5432/monzo\n"
)
for name, content in zip(names, [application, database, migration], strict=True):
    fd = os.open(args.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
print("Generated deployment configuration with mode 0600; values were not printed.")
