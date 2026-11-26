"""Run checks against an explicitly configured, isolated PostgreSQL test database."""

import os
import subprocess
import sys

from dotenv import dotenv_values
from sqlalchemy import create_engine

from monzo_backend.db import require_test_database, require_test_database_url

env = {**os.environ, **dotenv_values(".env.test")}
url = env.get("MONZO_DATABASE_URL", "")
require_test_database_url(url)
check_engine = create_engine(url, hide_parameters=True)
try:
    with check_engine.connect() as connection:
        require_test_database(connection)
finally:
    check_engine.dispose()
# Alembic must use the same checked database, never an inherited production override.
env["MONZO_MIGRATION_DATABASE_URL"] = url
for command in [
    [sys.executable, "-m", "ruff", "check", "src", "tests", "migrations", "scripts"],
    [sys.executable, "-m", "ruff", "format", "--check", "src", "tests", "migrations", "scripts"],
    [sys.executable, "-m", "alembic", "upgrade", "head"],
    [sys.executable, "-m", "alembic", "check"],
    [sys.executable, "-m", "pytest", "-q", "--tb=short"],
]:
    subprocess.run(command, env=env, check=True)
