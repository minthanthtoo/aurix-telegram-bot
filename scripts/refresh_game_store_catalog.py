"""Refresh the approved pilot shortlist using the Games service environment."""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from aurix_games.pricing_store import PricingPolicyStore
from scripts.refresh_fazercards_public_catalog import refresh_shortlist


def refresh_from_environment():
    base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    config = Path(os.environ.get("FAZERCARDS_PUBLIC_CATALOG_CONFIG", str(base / "public-catalog-config.json")))
    output = Path(os.environ.get("AURIX_GAMES_PUBLIC_CATALOG", str(base / "public-catalog.json")))
    shortlist = Path(os.environ.get("AURIX_GAMES_CATALOG_SHORTLIST", str(base / "pilot-shortlist.json")))
    key = os.environ.get("FAZERCARDS_API_KEY", "")
    if not key:
        raise ValueError("Supplier key is unavailable.")
    # Resolve commercial approval before creating evidence or contacting the supplier.
    pricing = PricingPolicyStore(config)
    pricing.refresh_auto_fx_if_due()
    pricing.approved_snapshot(allow_expired=True)
    if not shortlist.is_file() or shortlist.is_symlink() or shortlist.stat().st_mode & 0o077:
        raise ValueError("A private approved shortlist is required.")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    if base.is_symlink() or stat.S_IMODE(base.stat().st_mode) & 0o077:
        raise ValueError("Catalog state directory must be private.")
    lock = os.open(base / "catalog-refresh.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
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
                "selected_count": result["selected_count"]}
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
