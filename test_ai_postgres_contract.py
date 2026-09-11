import re
import tempfile
import unittest
from pathlib import Path

from aurix_ai.api_keys import _PostgresConnection
from aurix_ai.conversations import AI_CONVERSATION_MIGRATIONS, AIConversationStore
from persistence import open_sqlite_connection


class _FakeCursor:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeRawPostgresConnection:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def connection(self):
        return self

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return _FakeCursor()


def _postgres_schema_contract(statements):
    tables = {}
    indexes = set()
    for statement in statements:
        table_match = re.match(
            r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)\s*\((.*)\)\s*$",
            statement.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if table_match:
            table_name, body = table_match.groups()
            columns = set()
            for line in body.splitlines():
                token = line.strip().split(None, 1)[0].rstrip(",") if line.strip() else ""
                if token.upper() in {"CHECK", "CONSTRAINT", "FOREIGN", "PRIMARY", "UNIQUE"}:
                    continue
                if re.fullmatch(r"[a-z_][a-z0-9_]*", token, re.IGNORECASE):
                    columns.add(token.lower())
            tables[table_name.lower()] = columns
            continue
        index_match = re.match(
            r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+([a-z_]+)",
            statement.strip(),
            re.IGNORECASE,
        )
        if index_match:
            indexes.add(index_match.group(1).lower())
    return {
        "tables": {name: sorted(columns) for name, columns in sorted(tables.items())},
        "indexes": sorted(indexes),
    }


def _sqlite_schema_contract(path):
    with open_sqlite_connection(path) as connection:
        table_names = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        tables = {
            table_name: sorted(
                row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")
            )
            for table_name in table_names
        }
        indexes = sorted(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"
            )
        )
    return {"tables": tables, "indexes": indexes}


class AIConversationPostgresContractTest(unittest.TestCase):
    def test_postgres_migrations_match_sqlite_schema_and_use_postgres_sql(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            sqlite_path = Path(temporary_directory) / "conversations.db"
            sqlite_store = AIConversationStore(sqlite_path)
            sqlite_store.initialize()
            sqlite_contract = _sqlite_schema_contract(sqlite_path)

        raw = _FakeRawPostgresConnection()
        postgres_store = AIConversationStore(
            connection_factory=lambda: _PostgresConnection(raw),
            dialect="postgres",
        )
        postgres_store.initialize()
        postgres_statements = [query for query, _params in raw.calls]

        self.assertEqual(_postgres_schema_contract(postgres_statements), sqlite_contract)
        self.assertTrue(any("owner_telegram_id BIGINT" in query for query in postgres_statements))
        self.assertFalse(any("PRAGMA" in query for query in postgres_statements))
        self.assertFalse(any("?" in query for query in postgres_statements))

    def test_postgres_connection_adapter_translates_conversation_parameters(self):
        raw = _FakeRawPostgresConnection()
        connection = _PostgresConnection(raw)
        with connection:
            connection.execute(
                "SELECT id FROM ai_conversations WHERE owner_telegram_id = ? LIMIT ?",
                (101, 1),
            )
        self.assertEqual(
            raw.calls,
            [(
                "SELECT id FROM ai_conversations WHERE owner_telegram_id = %s LIMIT %s",
                (101, 1),
            )],
        )

    def test_registry_contains_one_immutable_conversation_migration(self):
        self.assertEqual(
            [(migration.version, migration.name) for migration in AI_CONVERSATION_MIGRATIONS],
            [(1, "durable_conversations_and_attempts")],
        )


if __name__ == "__main__":
    unittest.main()
