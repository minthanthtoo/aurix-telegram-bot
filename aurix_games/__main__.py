"""Run the local preview with: python -m aurix_games."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time

from .game_admin import (
    build_env_admin_service,
    build_env_shop2topup_webhook_reconciler,
    build_env_webhook_reconciler,
)
from .customer_api import build_env_customer_api
from .customer_checkout import build_env_customer_checkout
from .checkout_journey import GameAccountLookup, build_env_checkout_journey
from .payment_service import CustomerPaymentService
from .order_repository import GameOrderRepository
from .providers.fazercards_commands import FazerCardsCommandClient
from .providers.shop2topup_commands import Shop2TopupCommandClient
from .fulfillment import SupplierOrderRouter
from .web_api import create_server
from .payment_review import build_env_payment_review
from .topup_preview import load_topup_preview
from .catalog_policy import env_catalog_policy_store
from .pricing_store import PricingPolicyStore
from .public_catalog import PublicCatalogStore
from .supplier_routes import PrivateSupplierRouteStore
from .store_features import StoreFeatureStore


def _supplier_discovery_refresh_loop() -> None:
    from scripts.refresh_game_store_catalog import (
        DISCOVERY_RECHECK_INTERVAL,
        refresh_supplier_discovery_from_environment,
    )

    logger = logging.getLogger(__name__)
    while True:
        try:
            result = refresh_supplier_discovery_from_environment()
            if result.get("status") == "published":
                logger.info("FazerCards supplier title index refreshed (%s titles)", result.get("count"))
        except Exception as exc:
            logger.warning("FazerCards supplier title index refresh failed (%s)", type(exc).__name__)
        time.sleep(DISCOVERY_RECHECK_INTERVAL.total_seconds())


def _start_supplier_discovery_refresh() -> None:
    if not os.environ.get("FAZERCARDS_API_KEY", "").strip():
        return
    threading.Thread(
        target=_supplier_discovery_refresh_loop,
        name="supplier-title-index-refresh",
        daemon=True,
    ).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AuriX Game Store preview")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: loopback only)")
    parser.add_argument("--port", type=int, default=0, help="port (default: choose a free local port)")
    parser.add_argument("--live-store", action="store_true", help="open the public HTTPS store from the local storefront address")
    parser.add_argument("--local-mlbb-lookup", action="store_true", help="enable local MLBB account checks at 127.0.0.1 without customer sign-in")
    parser.add_argument("--topup-preview-snapshot", type=Path, action="append", default=[],
                        help="show package previews from a private supplier snapshot on 127.0.0.1")
    args = parser.parse_args()
    if args.local_mlbb_lookup and (args.host != "127.0.0.1" or not os.environ.get("FAZERCARDS_API_KEY")):
        parser.error("--local-mlbb-lookup requires 127.0.0.1 and FAZERCARDS_API_KEY")
    if args.topup_preview_snapshot and args.host != "127.0.0.1":
        parser.error("--topup-preview-snapshot requires 127.0.0.1")
    store_base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    snapshot_path = Path(os.environ.get(
        "FAZERCARDS_CATALOG_SNAPSHOT", str(store_base / "catalog-20260921.json")
    ))
    preview_paths = args.topup_preview_snapshot or ([snapshot_path] if snapshot_path.is_file() else [])
    try:
        topup_preview = load_topup_preview(
            preview_paths, require_private=not bool(args.topup_preview_snapshot)
        ) if preview_paths else None
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        logging.getLogger(__name__).warning(
            "Supplier package snapshot could not be loaded (%s)", type(exc).__name__
        )
        topup_preview = None
    customer_api = build_env_customer_api()
    customer_checkout = build_env_customer_checkout(customer_api=customer_api)
    wallet_payments = None if customer_api is None else CustomerPaymentService(
        customer_api.service.repository, customer_api.wallet_service.ledger
    )
    journey = build_env_checkout_journey(customer_checkout, wallet_payment_service=wallet_payments)
    mlbb_lookup = None
    if args.local_mlbb_lookup or os.environ.get("AURIX_GAMES_RECIPIENT_VALIDATION_ENABLED") == "1":
        fazer_key = os.environ.get("FAZERCARDS_API_KEY", "").strip()
        shop2topup_key = os.environ.get("SHOP2TOPUP_API_KEY", "").strip()
        if not fazer_key and not shop2topup_key:
            parser.error("account checks require FAZERCARDS_API_KEY or SHOP2TOPUP_API_KEY")
        base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
        repository = customer_api.service.repository if customer_api is not None else GameOrderRepository(
            base / "local-account-lookup.sqlite3",
        )
        zones_path = os.environ.get("AURIX_GAMES_MLBB_ZONES_FILE")
        zones = json.loads(Path(zones_path).read_text(encoding="utf-8")) if zones_path else []
        if not isinstance(zones, list):
            parser.error("MLBB zone directory must be a JSON list of id and name entries")
        config_path = Path(os.environ.get("FAZERCARDS_PUBLIC_CATALOG_CONFIG", str(base / "public-catalog-config.json")))
        shop2topup_mapping_path = Path(os.environ.get(
            "SHOP2TOPUP_MAPPING_CONFIG", str(base / "shop2topup-mapping-config.json")
        ))
        public_catalog_path = Path(os.environ.get("AURIX_GAMES_PUBLIC_CATALOG", str(base / "public-catalog.json")))
        public_catalog = PublicCatalogStore(
            public_catalog_path,
            catalog_policy_store=env_catalog_policy_store(base),
            pricing_policy_store=PricingPolicyStore(config_path),
        )
        supplier_router = SupplierOrderRouter(
            FazerCardsCommandClient(fazer_key, allow_recipient_validation=True, timeout=10) if fazer_key else None,
            Shop2TopupCommandClient(
                shop2topup_key,
                allow_player_validation=os.environ.get("SHOP2TOPUP_PLAYER_VALIDATION_ENABLED") == "1",
            ) if shop2topup_key else None,
        )
        fingerprint_key = fazer_key or shop2topup_key
        mlbb_lookup = GameAccountLookup(
            supplier_router,
            repository, fingerprint_secret=os.environ.get("AURIX_GAMES_CHECKOUT_FINGERPRINT_SECRET")
            or hashlib.sha256(fingerprint_key.encode()).digest(),
            local_guest=args.local_mlbb_lookup, zones=zones,
            catalog_store=public_catalog,
            supplier_route_store=PrivateSupplierRouteStore(
                config_path, additional_paths=(shop2topup_mapping_path,)
            ),
        )
    store_database = Path(os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(store_base / "game-orders.sqlite3")))
    server = create_server(
        args.host,
        args.port,
        admin_service=build_env_admin_service(),
        order_reconciler=build_env_webhook_reconciler(),
        shop2topup_webhook_reconciler=build_env_shop2topup_webhook_reconciler(),
        customer_order_service=None if customer_api is None else customer_api.service,
        customer_auth=None if customer_api is None else customer_api.verifier,
        customer_identity_service=None if customer_api is None else customer_api.identity_service,
        customer_wallet_service=None if customer_api is None else customer_api.wallet_service,
        customer_checkout_service=customer_checkout,
        checkout_journey=journey,
        mlbb_lookup=mlbb_lookup,
        topup_preview=topup_preview,
        payment_review=build_env_payment_review(customer_api),
        store_features=StoreFeatureStore(store_database),
        redirect_live_store=args.live_store,
    )
    _start_supplier_discovery_refresh()
    print(f"AuriX Game Store preview: http://{args.host}:{server.server_port}/game-store/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
