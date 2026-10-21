#!/bin/sh
# Bring the database in DATABASE_URL (or backend/.env) up to date and load the
# demo data. Safe to run on every deploy: both steps skip work already done.
set -e
cd "$(dirname "$0")/.."
alembic upgrade head
python -m scripts.seed
