"""tests/test_migrations.py

Basic migration test: create an old-schema SQLite DB and instantiate GemaDatabase.
The test asserts that the main table exists after initialization, demonstrating
that the application can detect/upgrade an older schema at startup.

Note: This test is intentionally conservative to avoid making assumptions about
migration implementation details. It verifies presence of the key table and at
least one expected column.
"""
import sqlite3
from pathlib import Path

from database import GemaDatabase


def test_migrations_from_old_schema(tmp_path):
    db_path = str(tmp_path / "old_schema.db")
    sql_file = Path(__file__).parent.parent / 'tools' / 'emit_old_schema.sql'

    # Write the old schema into the DB file
    with sqlite3.connect(db_path) as conn:
        with open(sql_file, 'r', encoding='utf8') as fh:
            conn.executescript(fh.read())

    # Instantiate application DB layer which should run migrations/initializers
    gdb = GemaDatabase(db_path=db_path)

    # Verify the key table exists and contains at least the job_hash column
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute("PRAGMA table_info('seen_jobs_registry')")
        cols = [r[1] for r in cur.fetchall()]

    assert 'job_hash' in cols
    assert 'url' in cols
