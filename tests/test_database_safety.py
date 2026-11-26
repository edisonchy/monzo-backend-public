import runpy
from unittest.mock import Mock

import pytest
from sqlalchemy.engine import make_url

from monzo_backend.db import require_test_database, require_test_database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://monzo_test:password@localhost/monzo",
        "postgresql://user:monzo_test@localhost/monzo",
        "postgresql://localhost/monzo?application_name=monzo_test",
        "postgresql://localhost/monzo_test_backup",
    ],
)
def test_test_database_guard_rejects_substring_matches(url):
    with pytest.raises(ValueError):
        require_test_database_url(url)


def test_test_database_guard_checks_actual_database():
    connection = Mock()
    connection.engine.url = make_url("postgresql://localhost/monzo_test")
    connection.scalar.return_value = "monzo"
    with pytest.raises(ValueError, match="Connected database"):
        require_test_database(connection)
    connection.scalar.return_value = "monzo_test"
    require_test_database(connection)


def test_check_runner_overrides_inherited_migration_database(monkeypatch):
    from monzo_backend.config import settings

    url = settings().database_url.get_secret_value()
    monkeypatch.setenv("MONZO_MIGRATION_DATABASE_URL", "postgresql://localhost/monzo")
    monkeypatch.setattr("dotenv.dotenv_values", lambda _: {})
    commands = []
    monkeypatch.setattr(
        "subprocess.run", lambda command, **kwargs: commands.append((command, kwargs))
    )
    runpy.run_path("scripts/run_checks.py")
    assert commands
    assert all(kwargs["env"]["MONZO_MIGRATION_DATABASE_URL"] == url for _, kwargs in commands)
