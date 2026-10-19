import os

from sqlalchemy import BigInteger
from sqlalchemy.ext.compiler import compiles

# Lets app modules import without a real .env; no connection is opened at import time.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")


@compiles(BigInteger, "sqlite")
def bigint_as_sqlite_integer(type_, compiler, **kwargs):
    # Tests use SQLite, where only "INTEGER PRIMARY KEY" auto-increments
    # (PostgreSQL uses BIGSERIAL). SQLite INTEGER is 64-bit, so money values are
    # unaffected. This changes test databases only, never the real schema.
    return "INTEGER"
