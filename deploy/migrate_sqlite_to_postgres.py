#!/usr/bin/env python3
"""Safely copy an AuriX SQLite database into the PostgreSQL control plane.

The application can use PostgreSQL immediately after its migrations run, but
switching a live installation must not silently discard existing customers,
orders, wallet entries, or audit history.  This tool performs an explicit,
idempotent copy from a validated SQLite snapshot into the PostgreSQL database
named by ``COMMERCE_DATABASE_URL``.  It never updates or deletes a conflicting
target row; conflicting values are reported and cause a non-zero exit.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from commerce import PostgresCommerceDatabase  # noqa: E402
from deploy.fleet_reconcile import FleetError, load_dotenv  # noqa: E402


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SKIP_TABLES = {"schema_migrations"}


def quote_identifier(value: str) -> str:
    """Quote a database identifier after enforcing the migration's grammar."""
    if not IDENTIFIER.fullmatch(value):
        raise FleetError(f"unsafe database identifier: {value!r}")
    return f'"{value}"'


def normalize_value(value: Any) -> Any:
    """Make SQLite/psycopg values comparable without logging their contents."""
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalize_value(item) for item in value]
    return value


def comparable_row(row: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(normalize_value(value) for value in row)


def sqlite_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(
        str(row[0]) for row in rows if str(row[0]) not in SKIP_TABLES
    )


def sqlite_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    quoted = quote_identifier(table)
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted})")]


def sqlite_primary_key(connection: sqlite3.Connection, table: str) -> list[str]:
    quoted = quote_identifier(table)
    rows = list(connection.execute(f"PRAGMA table_info({quoted})"))
    return [str(row[1]) for row in sorted(rows, key=lambda item: int(item[5])) if int(row[5])]


def sqlite_dependencies(connection: sqlite3.Connection, table: str) -> set[str]:
    quoted = quote_identifier(table)
    return {
        str(row[2])
        for row in connection.execute(f"PRAGMA foreign_key_list({quoted})")
        if str(row[2]) not in SKIP_TABLES
    }


def dependency_order(connection: sqlite3.Connection, tables: list[str]) -> list[str]:
    """Return parents before children; append any irreducible cycle safely."""
    known = set(tables)
    parents: dict[str, set[str]] = {
        table: sqlite_dependencies(connection, table) & known for table in tables
    }
    children: dict[str, set[str]] = defaultdict(set)
    for table, dependencies in parents.items():
        for parent in dependencies:
            if parent != table:
                children[parent].add(table)
    ready = deque(sorted(table for table, deps in parents.items() if not deps - {table}))
    result: list[str] = []
    while ready:
        table = ready.popleft()
        if table in result:
            continue
        result.append(table)
        for child in sorted(children.get(table, ())):
            parents[child].discard(table)
            if not (parents[child] - {child}) and child not in result:
                ready.append(child)
    # A cycle is not expected in the commerce schema.  Keep a deterministic
    # fallback so the command can still report a useful FK error instead of
    # depending on SQLite's sqlite_master order.
    result.extend(sorted(set(tables) - set(result)))
    return result


def target_columns(connection: Any, table: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT column_name, is_nullable, column_default, is_identity,
                          is_generated
             FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position""",
        (table,),
    ).fetchall()
    return [
        {
            "name": str(row[0]),
            "nullable": str(row[1]).upper() == "YES",
            "default": row[2],
            "identity": str(row[3]).upper() == "YES",
            "generated": str(row[4]).upper() != "NEVER",
        }
        for row in rows
    ]


def target_primary_key(connection: Any, table: str) -> list[str]:
    rows = connection.execute(
        """SELECT kcu.column_name
             FROM information_schema.table_constraints tc
             JOIN information_schema.key_column_usage kcu
               ON kcu.constraint_name = tc.constraint_name
              AND kcu.constraint_schema = tc.constraint_schema
            WHERE tc.table_schema = 'public'
              AND tc.table_name = %s
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position""",
        (table,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def row_key(row: dict[str, Any], primary_key: list[str]) -> tuple[Any, ...]:
    if not primary_key:
        raise FleetError("target table has no primary key; refusing migration")
    return tuple(normalize_value(row.get(column)) for column in primary_key)


def ensure_source(path: Path) -> sqlite3.Connection:
    if not path.is_absolute():
        raise FleetError("--source must be an absolute SQLite path")
    if not path.is_file():
        raise FleetError(f"SQLite source is not readable: {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    check = connection.execute("PRAGMA integrity_check").fetchone()
    if not check or str(check[0]).lower() != "ok":
        connection.close()
        raise FleetError("SQLite integrity_check failed")
    return connection


def postgres_url(env: dict[str, str]) -> str:
    value = env.get("COMMERCE_DATABASE_URL", "").strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise FleetError("COMMERCE_DATABASE_URL must be a PostgreSQL URL")
    return value


def copy_database(source_path: Path, env: dict[str, str], *, confirm: bool, dry_run: bool) -> dict[str, Any]:
    if not confirm and not dry_run:
        raise FleetError("refusing to write PostgreSQL without --confirm")
    database_url = postgres_url(env)
    source = ensure_source(source_path)
    try:
        tables = sqlite_tables(source)
        order = dependency_order(source, tables)
        source_rows: dict[str, list[dict[str, Any]]] = {}
        for table in order:
            quoted = quote_identifier(table)
            source_rows[table] = [dict(row) for row in source.execute(f"SELECT * FROM {quoted}")]
        if dry_run:
            return {
                "status": "dry_run",
                "tables": [{"name": table, "rows": len(source_rows[table])} for table in order],
            }

        # Initializing applies every idempotent PostgreSQL migration before
        # any source data is copied.  It is safe to rerun on an already-
        # migrated target and gives the comparator a complete schema.
        target_database = PostgresCommerceDatabase(database_url)
        target_database.initialize()
        target_database.close()

        import psycopg

        inserted = 0
        existing = 0
        conflicts: list[str] = []
        with psycopg.connect(database_url, connect_timeout=10) as connection:
            for table in order:
                rows = source_rows[table]
                if not rows:
                    continue
                metadata = target_columns(connection, table)
                if not metadata:
                    raise FleetError(f"target table is missing: {table}")
                target_names = [item["name"] for item in metadata]
                source_names = list(rows[0])
                columns = [
                    name for name in source_names
                    if name in target_names
                    and not next(item for item in metadata if item["name"] == name)["identity"]
                    and not next(item for item in metadata if item["name"] == name)["generated"]
                ]
                if not columns:
                    raise FleetError(f"no writable common columns for target table: {table}")
                missing_required = [
                    item["name"] for item in metadata
                    if item["name"] not in columns
                    and not item["nullable"]
                    and item["default"] is None
                    and not item["identity"]
                    and not item["generated"]
                ]
                if missing_required:
                    raise FleetError(
                        f"target table {table} has required columns absent from SQLite: "
                        + ", ".join(missing_required)
                    )
                primary_key = target_primary_key(connection, table)
                if not set(primary_key).issubset(columns):
                    raise FleetError(f"target primary key is not present in source table: {table}")

                quoted_table = quote_identifier(table)
                quoted_columns = ", ".join(quote_identifier(column) for column in columns)
                placeholders = ", ".join(["%s"] * len(columns))
                statement = (
                    f"INSERT INTO {quoted_table} ({quoted_columns}) VALUES ({placeholders}) "
                    "ON CONFLICT DO NOTHING"
                )
                for source_row in rows:
                    values = [source_row[column] for column in columns]
                    before = connection.execute(
                        f"SELECT {quoted_columns} FROM {quoted_table} "
                        f"WHERE "
                        + " AND ".join(
                            f"{quote_identifier(key)} = %s" for key in primary_key
                        ),
                        tuple(source_row[key] for key in primary_key),
                    ).fetchone()
                    if before is None:
                        connection.execute(statement, values)
                        after = connection.execute(
                            f"SELECT {quoted_columns} FROM {quoted_table} "
                            f"WHERE "
                            + " AND ".join(
                                f"{quote_identifier(key)} = %s" for key in primary_key
                            ),
                            tuple(source_row[key] for key in primary_key),
                        ).fetchone()
                        if after is None:
                            raise FleetError(f"target insert did not persist row in {table}")
                        inserted += 1
                        target_row = dict(zip(columns, after))
                    else:
                        existing += 1
                        target_row = dict(zip(columns, before))
                    source_comparison = comparable_row(source_row[column] for column in columns)
                    target_comparison = comparable_row(target_row[column] for column in columns)
                    if source_comparison != target_comparison:
                        conflicts.append(f"{table}:{row_key(source_row, primary_key)}")
                connection.commit()
        if conflicts:
            preview = ", ".join(conflicts[:5])
            suffix = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
            raise FleetError(f"conflicting target rows detected: {preview}{suffix}")
        return {
            "status": "complete",
            "tables": len(order),
            "inserted_rows": inserted,
            "already_present_rows": existing,
        }
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="absolute SQLite database snapshot")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="allow writes to PostgreSQL; conflicts still fail closed",
    )
    parser.add_argument("--dry-run", action="store_true", help="inspect source without contacting PostgreSQL")
    args = parser.parse_args([] if argv is None else argv)
    try:
        env = load_dotenv(Path(args.env_file), overwrite=False)
        report = copy_database(
            Path(args.source), env, confirm=args.confirm, dry_run=args.dry_run
        )
    except (FleetError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"SQLite→PostgreSQL migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
