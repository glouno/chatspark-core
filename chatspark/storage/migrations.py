import sqlite3
from importlib.resources import files
from pathlib import Path

from alembic import command
from alembic.config import Config


def resolve_v3_migration_paths(candidates=None):
    return {"script_location": str(files("chatspark.migrations"))}


def initialize_v3_database(database):
    path = Path(database).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and path.stat().st_size:
        with sqlite3.connect(path) as con:
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if tables:
                version = (
                    con.execute(
                        "SELECT value FROM schema_metadata WHERE key='app_schema_version'"
                    ).fetchone()
                    if "schema_metadata" in tables
                    else None
                )
                if not version or str(version[0]) != "3":
                    raise ValueError("Refusing to initialize v3 over a non-v3 database")
    config = Config()
    config.set_main_option("script_location", str(files("chatspark.migrations")))
    config.set_main_option("sqlalchemy.url", "sqlite:///" + str(path))
    command.upgrade(config, "head")
    if path.exists():
        path.chmod(0o600)
    return {
        "report_version": 1,
        "ok": True,
        "schema": "v3",
        "db_path": str(path),
        "revision": "head",
    }
