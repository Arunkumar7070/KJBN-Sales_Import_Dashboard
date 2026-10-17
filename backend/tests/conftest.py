import os

# Lets app modules import without a real .env; no connection is opened at import time.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
