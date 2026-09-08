#!/usr/bin/env python3
"""One-time, fail-closed migration of AuriX state from SQLite to PostgreSQL.

The bot must be stopped before ``--apply`` so the source snapshot cannot change
during copy. The destination must not contain existing operational state. Secrets and
row values are never printed or written to the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from commerce import PostgresCommerceDatabase


CONFIRMATION = "STOP_BOT_AND_MIGRATE"
EXCLUDED_TABLES = {"schema_migrations", "sqlite_sequence"}
SEEDED_TABLES = {"plans", "vpn_endpoints"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"Unsafe database identifier: {identifier!r}")
    return f'"{identifier}"'


def source_tables(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows if str(row[0]) not in EXCLUDED_TABLES]


def dependency_order(connection: sqlite3.Connection, tables: list[str]) -> list[str]:
    """Return parent-before-child order, tolerating self references."""
    available = set(tables)
    dependencies: dict[str, set[str]] = {}
    for table in tables:
        parents = {
            str(row[2])
            for row in connection.execute(f"PRAGMA foreign_key_list({_quote(table)})")
            if str(row[2]) in available and str(row[2]) != table
        }
        dependencies[table] = parents
    ordered: list[str] = []
    pending = set(tables)
    while pending:
        ready = sorted(table for table in pending if not (dependencies[table] & pending))
        if not ready:
            cycle = ", ".join(sorted(pending))
            raise RuntimeError(f"Foreign-key dependency cycle requires manual migration: {cycle}")
        ordered.extend(ready)
        pending.difference_update(ready)
    return ordered


def sqlite_manifest(path: Path) -> tuple[dict[str, int], list[str], str]:
    if not path.is_file():
        raise SystemExit(f"SQLite source does not exist: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise SystemExit("SQLite integrity_check failed; migration was not started")
        tables = source_tables(connection)
        order = dependency_order(connection, tables)
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
            for table in order
        }
        return counts, order, digest.hexdigest()
    finally:
        connection.close()


def _target_schema(connection: Any) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    columns = connection.execute(
        """SELECT table_name, column_name, data_type
           FROM information_schema.columns
           WHERE table_schema = 'public'
           ORDER BY table_name, ordinal_position"""
    ).fetchall()
    schema: dict[str, dict[str, str]] = {}
    for row in columns:
        schema.setdefault(str(row["table_name"]), {})[str(row["column_name"])] = str(
            row["data_type"]
        )
    primary_rows = connection.execute(
        """SELECT kcu.table_name, kcu.column_name
           FROM information_schema.table_constraints tc
           JOIN information_schema.key_column_usage kcu
             ON tc.constraint_name = kcu.constraint_name
            AND tc.table_schema = kcu.table_schema
           WHERE tc.table_schema = 'public' AND tc.constraint_type = 'PRIMARY KEY'
           ORDER BY kcu.table_name, kcu.ordinal_position"""
    ).fetchall()
    primary_keys: dict[str, list[str]] = {}
    for row in primary_rows:
        primary_keys.setdefault(str(row["table_name"]), []).append(str(row["column_name"]))
    return schema, primary_keys


def _convert(value: Any, data_type: str) -> Any:
    if value is not None and data_type == "boolean":
        return bool(value)
    return value


def migrate(source: Path, target_url: str) -> dict[str, Any]:
    database = PostgresCommerceDatabase(target_url)
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    source_connection.row_factory = sqlite3.Row
    try:
        database.initialize()
        tables = dependency_order(source_connection, source_tables(source_connection))
        with database.connect() as target:
            target.execute("SELECT pg_advisory_xact_lock(68474912026)")
            schema, primary_keys = _target_schema(target)
            missing_tables = sorted(set(tables) - set(schema))
            if missing_tables:
                raise RuntimeError("PostgreSQL schema is missing: " + ", ".join(missing_tables))
            occupied = []
            for table in sorted((set(tables) - SEEDED_TABLES) & set(schema)):
                count = int(target.execute(f"SELECT COUNT(*) AS n FROM {_quote(table)}").fetchone()["n"])
                if count:
                    occupied.append(f"{table}={count}")
            if occupied:
                raise RuntimeError(
                    "Destination already contains operational state; restore or choose a fresh database: "
                    + ", ".join(occupied)
                )

            copied: dict[str, int] = {}
            for table in tables:
                source_columns = [
                    str(row[1])
                    for row in source_connection.execute(f"PRAGMA table_info({_quote(table)})")
                ]
                missing_columns = sorted(set(source_columns) - set(schema[table]))
                if missing_columns:
                    raise RuntimeError(
                        f"PostgreSQL table {table} is missing source columns: "
                        + ", ".join(missing_columns)
                    )
                keys = primary_keys.get(table, [])
                if not keys:
                    raise RuntimeError(f"PostgreSQL table {table} has no primary key")
                quoted_columns = ", ".join(_quote(column) for column in source_columns)
                placeholders = ", ".join("?" for _ in source_columns)
                conflict = ", ".join(_quote(column) for column in keys)
                updated = [column for column in source_columns if column not in keys]
                if updated:
                    action = "DO UPDATE SET " + ", ".join(
                        f"{_quote(column)} = EXCLUDED.{_quote(column)}" for column in updated
                    )
                else:
                    action = "DO NOTHING"
                statement = (
                    f"INSERT INTO {_quote(table)} ({quoted_columns}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({conflict}) {action}"
                )
                rows = source_connection.execute(
                    f"SELECT {quoted_columns} FROM {_quote(table)}"
                ).fetchall()
                for row in rows:
                    values = tuple(
                        _convert(row[column], schema[table][column]) for column in source_columns
                    )
                    target.execute(statement, values)
                copied[table] = len(rows)

            target_counts: dict[str, int] = {}
            for table, expected in copied.items():
                observed = int(
                    target.execute(f"SELECT COUNT(*) AS n FROM {_quote(table)}").fetchone()["n"]
                )
                target_counts[table] = observed
                reconciled = observed >= expected if table in SEEDED_TABLES else observed == expected
                if not reconciled:
                    raise RuntimeError(
                        f"Row-count reconciliation failed for {table}: source={expected}, target={observed}"
                    )
            for table, keys in primary_keys.items():
                if table not in copied or len(keys) != 1:
                    continue
                key = keys[0]
                sequence = target.execute(
                    "SELECT pg_get_serial_sequence(?, ?) AS name", (table, key)
                ).fetchone()["name"]
                if not sequence:
                    continue
                maximum = target.execute(
                    f"SELECT MAX({_quote(key)}) AS maximum FROM {_quote(table)}"
                ).fetchone()["maximum"]
                target.execute(
                    "SELECT setval(?::regclass, ?, ?)",
                    (sequence, int(maximum or 1), maximum is not None),
                )
        return {
            "source_counts": copied,
            "target_counts": target_counts,
            "total_rows": sum(copied.values()),
        }
    finally:
        source_connection.close()
        database.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.environ.get("DATABASE_PATH", "data/bot.db"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--report", default="")
    args = parser.parse_args(argv)
    source = Path(args.source).expanduser()
    counts, order, source_sha256 = sqlite_manifest(source)
    print(f"SQLite integrity: ok; {sum(counts.values())} row(s) across {len(counts)} table(s)")
    print("Copy order: " + ", ".join(order))
    if not args.apply:
        print(f"Dry run only. Stop the bot, then use --apply --confirm {CONFIRMATION}.")
        return 0
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing write: --confirm must equal {CONFIRMATION}")
    target_url = os.environ.get("COMMERCE_DATABASE_URL", "").strip()
    if not target_url:
        raise SystemExit("COMMERCE_DATABASE_URL is required; it is never accepted as a CLI argument")
    result = migrate(source, target_url)
    report = {
        "completed_at": datetime.now(UTC).isoformat(),
        "source_sha256": source_sha256,
        "source_counts": counts,
        "target_counts": result["target_counts"],
        "total_rows": result["total_rows"],
    }
    print(f"Migration committed and reconciled: {result['total_rows']} row(s)")
    if args.report:
        report_path = Path(args.report).expanduser().resolve()
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            raise SystemExit(
                f"Migration committed, but the reconciliation report could not be written: {type(exc).__name__}"
            ) from exc
        print(f"Reconciliation report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
