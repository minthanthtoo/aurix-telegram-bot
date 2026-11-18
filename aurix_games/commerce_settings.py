"""Operator controls for commerce features and optional spending limits."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

FLAGS = {
    "checkout_enabled": "AURIX_GAMES_CUSTOMER_CHECKOUT_ENABLED",
    "account_checks_enabled": "AURIX_GAMES_RECIPIENT_VALIDATION_ENABLED",
    "payments_enabled": "AURIX_GAMES_ACCEPT_TRANSFERS",
    "supplier_orders_enabled": "FAZERCARDS_ENABLE_WRITES",
}


class SettingsConflict(ValueError):
    pass


def validate_settings(value):
    if not isinstance(value, dict) or set(value) != set(FLAGS) | {"daily_limit_usd", "maximum_order_mmk"}:
        raise ValueError("Commerce settings fields are invalid.")
    result = {}
    for field in FLAGS:
        if type(value[field]) is not bool:
            raise ValueError("Feature settings must be true or false.")
        result[field] = value[field]
    limit = value["daily_limit_usd"]
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, (str, int)):
            raise ValueError("Daily USD limit must be a positive decimal or empty.")
        try:
            number = Decimal(str(limit))
        except InvalidOperation:
            raise ValueError("Daily USD limit is invalid.") from None
        if not number.is_finite() or number <= 0:
            raise ValueError("Daily USD limit must be positive.")
        limit = format(number, "f")
    result["daily_limit_usd"] = limit
    maximum = value["maximum_order_mmk"]
    if maximum is not None and (type(maximum) is not int or maximum <= 0):
        raise ValueError("Maximum MMK order must be a positive integer or empty.")
    result["maximum_order_mmk"] = maximum
    return result


class CommerceSettingsStore:
    def __init__(self, path):
        self.path = Path(path)

    def status(self):
        if self.path.is_symlink():
            raise ValueError("Commerce settings file must be private.")
        if not self.path.exists():
            return {"revision": 0, "configured": False, "settings": {
                **{key: os.environ.get(env, "0").strip() == "1" for key, env in FLAGS.items()},
                "daily_limit_usd": None, "maximum_order_mmk": None}}
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or self.path.parent.stat().st_mode & 0o077:
            raise ValueError("Commerce settings file must be private.")
        record = json.loads(self.path.read_text())
        if type(record.get("revision")) is not int or record["revision"] < 1:
            raise ValueError("Commerce settings revision is invalid.")
        record["settings"] = validate_settings(record["settings"])
        return {**record, "configured": True}

    def save(self, settings, *, expected_revision, actor):
        settings = validate_settings(settings)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected settings revision is invalid.")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
            raise ValueError("Settings actor is required.")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.stat().st_mode & 0o077:
            raise ValueError("Commerce settings directory must be private.")
        lock = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            before = self.status()
            if before["revision"] != expected_revision:
                raise SettingsConflict("Commerce settings changed. Reload before saving.")
            record = {"revision": expected_revision + 1, "settings": settings, "actor": actor,
                      "updated_at": datetime.now(timezone.utc).isoformat()}
            descriptor, name = tempfile.mkstemp(dir=self.path.parent, prefix=".commerce-settings-")
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(record, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            return {**record, "configured": True}
        finally:
            os.close(lock)


def env_commerce_settings():
    base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    return CommerceSettingsStore(base / "commerce-settings.json")


def commerce_enabled(field):
    return env_commerce_settings().status()["settings"][field]


def commerce_paused(field):
    record = env_commerce_settings().status()
    return record["configured"] and not record["settings"][field]
