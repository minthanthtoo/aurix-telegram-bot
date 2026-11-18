"""Run the local preview with: python -m aurix_games."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from .game_admin import build_env_admin_service, build_env_webhook_reconciler
from .customer_api import build_env_customer_api
from .customer_checkout import build_env_customer_checkout
from .checkout_journey import GameAccountLookup, build_env_checkout_journey
from .payment_service import CustomerPaymentService
from .order_repository import GameOrderRepository
from .providers.fazercards_commands import FazerCardsCommandClient
from .web_api import create_server
from .payment_review import build_env_payment_review
from .topup_preview import load_topup_preview
from .catalog_policy import env_catalog_policy_store
from .pricing_store import PricingPolicyStore
from .public_catalog import PublicCatalogStore
from .supplier_routes import PrivateSupplierRouteStore
from .store_features import StoreFeatureStore


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
    topup_preview = load_topup_preview(args.topup_preview_snapshot) if args.topup_preview_snapshot else None
    customer_api = build_env_customer_api()
    customer_checkout = build_env_customer_checkout(customer_api=customer_api)
    wallet_payments = None if customer_api is None else CustomerPaymentService(
        customer_api.service.repository, customer_api.wallet_service.ledger
    )
    journey = build_env_checkout_journey(customer_checkout, wallet_payment_service=wallet_payments)
    mlbb_lookup = None
    if args.local_mlbb_lookup or os.environ.get("AURIX_GAMES_RECIPIENT_VALIDATION_ENABLED") == "1":
        key = os.environ.get("FAZERCARDS_API_KEY", "")
        if not key:
            parser.error("account checks require FAZERCARDS_API_KEY")
        base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
        repository = customer_api.service.repository if customer_api is not None else GameOrderRepository(
            base / "local-account-lookup.sqlite3",
        )
        zones_path = os.environ.get("AURIX_GAMES_MLBB_ZONES_FILE")
        zones = json.loads(Path(zones_path).read_text(encoding="utf-8")) if zones_path else []
        if not isinstance(zones, list):
            parser.error("MLBB zone directory must be a JSON list of id and name entries")
        config_path = Path(os.environ.get("FAZERCARDS_PUBLIC_CATALOG_CONFIG", str(base / "public-catalog-config.json")))
        public_catalog_path = Path(os.environ.get("AURIX_GAMES_PUBLIC_CATALOG", str(base / "public-catalog.json")))
        public_catalog = PublicCatalogStore(
            public_catalog_path,
            catalog_policy_store=env_catalog_policy_store(base),
            pricing_policy_store=PricingPolicyStore(config_path),
        )
        mlbb_lookup = GameAccountLookup(
            FazerCardsCommandClient(key, allow_recipient_validation=True, timeout=10),
            repository, fingerprint_secret=os.environ.get("AURIX_GAMES_CHECKOUT_FINGERPRINT_SECRET")
            or hashlib.sha256(key.encode()).digest(),
            local_guest=args.local_mlbb_lookup, zones=zones,
            catalog_store=public_catalog,
            supplier_route_store=PrivateSupplierRouteStore(config_path),
        )
    store_base = Path(os.environ.get("AURIX_GAMES_BASE_DIR", ".local/fazercards"))
    store_database = Path(os.environ.get("AURIX_GAMES_ORDER_DATABASE", str(store_base / "game-orders.sqlite3")))
    server = create_server(
        args.host,
        args.port,
        admin_service=build_env_admin_service(),
        order_reconciler=build_env_webhook_reconciler(),
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
    print(f"AuriX Game Store preview: http://{args.host}:{server.server_port}/game-store/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
