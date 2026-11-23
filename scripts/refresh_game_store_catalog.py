"""Refresh the approved pilot shortlist using the Games service environment."""

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

from aurix_games.pricing_store import PricingPolicyStore
from aurix_games.public_catalog import build_supplier_discovery, load_supplier_discovery
from aurix_games.providers.fazercards import FazerCardsReadOnlyClient
from scripts.fazercards_catalog_inventory import _list_cursor_items
from scripts.refresh_fazercards_public_catalog import refresh_shortlist


DISCOVERY_REFRESH_INTERVAL = timedelta(hours=24)
DISCOVERY_RECHECK_INTERVAL = timedelta(hours=6)


def _write_supplier_discovery(path: Path, payload: dict[str, object]) -> None:
    if path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValueError("supplier discovery destination is unavailable")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise ValueError("supplier discovery directory must be private")
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > 1_000_000:
        raise ValueError("supplier discovery exceeded its safety limit")
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".supplier-discovery.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        if path.is_symlink():
            raise ValueError("supplier discovery destination must not be a symlink")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _supplier_discovery_status(path: Path, *, now: datetime) -> dict[str, object]:
    catalog = load_supplier_discovery(path, now=now)
    if catalog is None:
        return {"status": "unavailable", "count": 0, "updated_at": None}
    current = load_supplier_discovery(path, now=now, max_age=DISCOVERY_REFRESH_INTERVAL) is not None
    return {
        "status": "current" if current else "stale",
        "count": len(catalog["products"]),
        "updated_at": catalog["generated_at"],
    }


def _refresh_supplier_discovery(path: Path, *, api_key: str, now: datetime) -> dict[str, object]:
    current = load_supplier_discovery(path, now=now, max_age=DISCOVERY_REFRESH_INTERVAL)
    if current is not None:
        return {"status": "current", "count": len(current["products"]), "updated_at": current["generated_at"]}
    client = FazerCardsReadOnlyClient(api_key, min_interval_seconds=1.0)
    categories, _metadata = _list_cursor_items(client, "/topups", item_id_key="category_id")
    payload = build_supplier_discovery(categories, generated_at=datetime.now(timezone.utc))
    _write_supplier_discovery(path, payload)
    published = load_supplier_discovery(path, now=datetime.now(timezone.utc))
    if published is None:
        raise ValueError("supplier discovery publication failed validation")
    return {"status": "published", "count": len(published["products"]), "updated_at": published["generated_at"]}


def refresh_supplier_discovery_from_environment() -> dict[str, object]:
    """Refresh only the names-only index, sharing the full catalog's lock."""

    base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.is_symlink() or stat.S_IMODE(base.stat().st_mode) & 0o077:
        raise ValueError("Catalog state directory must be private.")
    key = os.environ.get("FAZERCARDS_API_KEY", "").strip()
    if not key:
        raise ValueError("Supplier key is unavailable.")
    path = Path(os.environ.get(
        "AURIX_GAMES_SUPPLIER_DISCOVERY", str(base / "supplier-discovery.json")
    ))
    lock = os.open(
        base / "catalog-refresh.lock",
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        return _refresh_supplier_discovery(path, api_key=key, now=datetime.now(timezone.utc))
    finally:
        os.close(lock)


def refresh_from_environment():
    base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.is_symlink() or stat.S_IMODE(base.stat().st_mode) & 0o077:
        raise ValueError("Catalog state directory must be private.")
    config = Path(os.environ.get("FAZERCARDS_PUBLIC_CATALOG_CONFIG", str(base / "public-catalog-config.json")))
    output = Path(os.environ.get("AURIX_GAMES_PUBLIC_CATALOG", str(base / "public-catalog.json")))
    shortlist = Path(os.environ.get("AURIX_GAMES_CATALOG_SHORTLIST", str(base / "pilot-shortlist.json")))
    key = os.environ.get("FAZERCARDS_API_KEY", "")
    if not key:
        raise ValueError("Supplier key is unavailable.")
    lock = os.open(base / "catalog-refresh.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        discovery_path = Path(os.environ.get(
            "AURIX_GAMES_SUPPLIER_DISCOVERY", str(base / "supplier-discovery.json")
        ))
        attempted_at = datetime.now(timezone.utc)
        discovery_result = _supplier_discovery_status(discovery_path, now=attempted_at)
        try:
            discovery_result = _refresh_supplier_discovery(discovery_path, api_key=key, now=attempted_at)
        except Exception:
            # This index is browse-only; preserve its last valid copy and keep
            # supplier-price refresh on its existing approval path.
            discovery_result = _supplier_discovery_status(discovery_path, now=datetime.now(timezone.utc))

        # Resolve commercial approval before creating evidence or refreshing prices.
        pricing = PricingPolicyStore(config)
        pricing.refresh_auto_fx_if_due()
        pricing.approved_snapshot(allow_expired=True)
        if not shortlist.is_file() or shortlist.is_symlink() or shortlist.stat().st_mode & 0o077:
            raise ValueError("A private approved shortlist is required.")
        evidence = base / "catalog-refresh-evidence"
        evidence.mkdir(mode=0o700, exist_ok=True)
        if evidence.is_symlink() or stat.S_IMODE(evidence.stat().st_mode) & 0o077:
            raise ValueError("Catalog evidence directory must be private.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        result = refresh_shortlist(
            shortlist_path=shortlist, config_path=config, output_path=output,
            snapshot_path=evidence / f"{stamp}-{uuid.uuid4().hex}.json",
            api_key=key, min_interval=1.0, max_age_minutes=15,
        )
        return {"status": "published", "offer_count": result["offer_count"],
                "selected_count": result["selected_count"], "supplier_discovery": discovery_result}
    finally:
        os.close(lock)


def main():
    try:
        result = refresh_from_environment()
    except (OSError, RuntimeError, ValueError, SystemExit):
        print(json.dumps({"status": "failed", "error": "Catalog refresh rejected or unavailable. Check private inputs and current approval."}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
