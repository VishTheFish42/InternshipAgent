import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

from src.web.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Read SUPABASE_DB_URL from the environment — required, no local-dev fallback,
# since this schema only exists in the Supabase project's Postgres instance.
_db_url = os.environ.get("SUPABASE_DB_URL")
if not _db_url:
    raise RuntimeError(
        "SUPABASE_DB_URL is not set. This migrates the multi-tenant web app's "
        "schema (src/web/db.py) against the Supabase project's Postgres "
        "instance — see .env.example."
    )
config.set_main_option("sqlalchemy.url", _db_url)

# Distinct from the worker's "alembic_version" table so a future move of the
# worker onto this same Postgres instance (T-1110) can't collide on it.
_VERSION_TABLE = "alembic_version_web"


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=_VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, version_table=_VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
