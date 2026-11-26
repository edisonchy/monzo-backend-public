import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from monzo_backend.models import Base

fileConfig(context.config.config_file_name)
url = os.environ.get("MONZO_MIGRATION_DATABASE_URL") or os.environ["MONZO_DATABASE_URL"]
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url, hide_parameters=True).connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
