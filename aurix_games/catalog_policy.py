"""Admin-owned merchandising policy, separate from supplier credentials and prices."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat

from persistence import open_sqlite_connection
from migrations import Migration, apply_migrations

FAMILY_BY_KIND = {'direct_topup': 'topups', 'gift_card': 'giftcards',
                  'game_key': 'gamekeys', 'manual_service': 'manual_services'}
FAMILIES = tuple(FAMILY_BY_KIND.values())
_MIGRATIONS = (Migration(1, 'versioned_catalog_merchandising_policy', sqlite_statements=(
    """CREATE TABLE IF NOT EXISTS game_catalog_policies (
        revision INTEGER PRIMARY KEY CHECK(revision>0), policy_json TEXT NOT NULL,
        updated_at TEXT NOT NULL, actor TEXT NOT NULL CHECK(actor='admin'))""",
    """CREATE TRIGGER IF NOT EXISTS game_catalog_policy_no_update BEFORE UPDATE ON game_catalog_policies
        BEGIN SELECT RAISE(ABORT, 'catalog policies are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS game_catalog_policy_no_delete BEFORE DELETE ON game_catalog_policies
        BEGIN SELECT RAISE(ABORT, 'catalog policies are immutable'); END""",
)),)


class CatalogPolicyConflict(ValueError):
    pass


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != {'enabled_families', 'excluded_products', 'excluded_offers'}:
        raise ValueError('catalog policy fields are invalid')
    result = {}
    for key, limit in (('enabled_families', 4), ('excluded_products', 200), ('excluded_offers', 500)):
        values = value[key]
        if (not isinstance(values, list) or len(values) > limit
                or any(not isinstance(v, str) or not re.fullmatch(r'[a-zA-Z0-9._:-]{1,160}', v) for v in values)
                or len(set(values)) != len(values)):
            raise ValueError(f'invalid {key}')
        result[key] = sorted(values)
    if not set(result['enabled_families']) <= set(FAMILIES):
        raise ValueError('unsupported catalog family')
    return result


def allows_offer(policy, offer):
    return (FAMILY_BY_KIND.get(offer.get('fulfillment_kind')) in policy['enabled_families']
            and offer.get('product_slug') not in policy['excluded_products']
            and offer.get('offer_id') not in policy['excluded_offers'])


class CatalogPolicyStore:
    def __init__(self, path):
        self.path = Path(path)

    def _check(self):
        if self.path.is_symlink():
            raise ValueError('catalog policy database must not be a symlink')
        if self.path.exists() and (not self.path.is_file() or stat.S_IMODE(self.path.stat().st_mode) & 0o077):
            raise ValueError('catalog policy database must be private')
        if self.path.exists() and stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise ValueError('catalog policy directory must be private')

    def status(self):
        self._check()
        if not self.path.exists():
            return {'revision': 0, 'configured': False, 'policy': {
                'enabled_families': list(FAMILIES), 'excluded_products': [], 'excluded_offers': []}}
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            row = db.execute('SELECT revision, policy_json, updated_at FROM game_catalog_policies ORDER BY revision DESC LIMIT 1').fetchone()
        except sqlite3.Error as exc:
            raise ValueError('catalog policy database is unavailable') from exc
        finally:
            db.close()
        if row is None:
            return {'revision': 0, 'configured': False, 'policy': {
                'enabled_families': list(FAMILIES), 'excluded_products': [], 'excluded_offers': []}}
        return {'revision': row[0], 'configured': True, 'policy': validate_policy(json.loads(row[1])), 'updated_at': row[2]}

    def save(self, policy, *, expected_revision):
        policy = validate_policy(policy)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('expected revision must be a non-negative integer')
        self._check()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(self.path.parent.stat().st_mode) & 0o077:
            raise ValueError('catalog policy directory must be private')
        # Create privately before SQLite can open the file, including concurrent first saves.
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            self._check()
        else:
            os.close(fd)
        with open_sqlite_connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            apply_migrations(db, component='games_catalog_policy', dialect='sqlite', migrations=_MIGRATIONS)
            revision = db.execute('SELECT coalesce(max(revision),0) FROM game_catalog_policies').fetchone()[0]
            if revision != expected_revision:
                raise CatalogPolicyConflict('catalog policy changed; reload before saving')
            updated_at = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO game_catalog_policies VALUES(?,?,?,?)',
                       (revision+1, json.dumps(policy, sort_keys=True), updated_at, 'admin'))
        return {'revision': revision+1, 'configured': True, 'policy': policy, 'updated_at': updated_at}

    def active_policy(self):
        current = self.status()
        return current['policy'] if current['configured'] else None


def env_catalog_policy_store(base=None):
    base = Path(base or os.environ.get('AURIX_GAMES_BASE_DIR', '.local/fazercards'))
    return CatalogPolicyStore(os.environ.get('AURIX_GAMES_CATALOG_POLICY_DATABASE', str(base / 'catalog-policy.sqlite3')))
