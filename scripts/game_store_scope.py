"""Games runtime ownership and CI routing for the shared AuriX repository."""

import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SHARED_MODULES = frozenset({
    "commerce_models.py", "migrations.py", "persistence.py", "telegram_web_app.py", "scripts/__init__.py",
})
LOCK_FILE = "config/games/requirements.lock"
GAME_SCRIPTS = frozenset({
    "scripts/approve_fazercards_mapping.py",
    "scripts/audit_games_fulfillment.py",
    "scripts/aurix_games_backup.py",
    "scripts/aurix_games_backup_job.py",
    "scripts/build_fazercards_public_catalog.py",
    "scripts/dispatch_paid_game_orders.py",
    "scripts/fazercards_catalog_inventory.py",
    "scripts/fazercards_wallet.py",
    "scripts/import_fazercards_snapshot.py",
    "scripts/reconcile_game_orders.py",
    "scripts/record_verified_game_transfer.py",
    "scripts/recover_game_supplier_order.py",
    "scripts/refresh_fazercards_public_catalog.py",
    "scripts/refresh_game_store_catalog.py",
    "scripts/review_game_payment.py",
    "scripts/review_game_store_mapping.py",
    "scripts/run_games_fulfillment.py",
    "scripts/seed_fazercards_mapping_queue.py",
})
GAME_TOOLS = frozenset({
    ".github/workflows/games-ci.yml",
    ".github/workflows/games-production.yml",
    "scripts/accept_fazercards_topup.py",
    "deploy/install_aurix_games.py",
    "scripts/analyze_game_store_competition.py",
    "scripts/capture_game_store_surfaces.mjs",
    "scripts/check_game_store_interactions.mjs",
    "scripts/check_game_store_ui.mjs",
    "scripts/fazercards_reference_match.py",
    "scripts/package_game_store.py",
    "scripts/run_game_store_checks.py",
    "scripts/run_game_store_sandbox_acceptance.py",
    "scripts/refresh_mlbb_strategy.py",
    "scripts/sync_game_icon_assets.py",
    "scripts/verify_game_store_domain.py",
})
OTHER_GAME_TESTS = frozenset({
    "test_cbm_fx.py",
    "test_live_catalog.py",
    "test_order_reconciliation.py",
    "test_pricing_store.py",
    "test_public_catalog.py",
    "test_public_catalog_builder.py",
    "test_supplier_routes.py",
    "test_tgi_android_reference.py",
    "test_wallet_monitor.py",
})
WEB_SUFFIXES = frozenset({
    ".html", ".css", ".js", ".mjs", ".json", ".webmanifest", ".svg", ".png", ".jpg",
    ".jpeg", ".webp", ".avif", ".gif", ".ico", ".txt", ".woff", ".woff2",
})
REQUIRED_RELEASE_FILES = SHARED_MODULES | frozenset({
    LOCK_FILE,
    "aurix_games/__main__.py", "aurix_games/web_api.py",
    "web/game-store/index.html", "web/game-store/app.js",
    "web/game-store/service-worker.js", "scripts/refresh_game_store_catalog.py",
    "scripts/run_games_fulfillment.py", "scripts/dispatch_paid_game_orders.py",
    "scripts/reconcile_game_orders.py", "scripts/audit_games_fulfillment.py",
    "scripts/aurix_games_backup_job.py",
})


def is_game_test(path: str) -> bool:
    name = PurePosixPath(path).name
    return path in OTHER_GAME_TESTS or name in OTHER_GAME_TESTS or (
        name.startswith(("test_game_", "test_fazercards_")) and name.endswith((".py", ".mjs"))
    )


def is_release_input(path: str) -> bool:
    parsed = PurePosixPath(path)
    if parsed.as_posix() != path or any(part.startswith(".") for part in parsed.parts):
        return False
    return (
        path in SHARED_MODULES | GAME_SCRIPTS or path == LOCK_FILE
        or (path.startswith("aurix_games/") and parsed.suffix == ".py")
        or (path.startswith("config/games/") and parsed.suffix == ".json")
        or (
            path.startswith("web/game-store/") and parsed.suffix.lower() in WEB_SUFFIXES
            and "token" not in parsed.name.lower()
        )
    )


def classify(paths: list[str]) -> tuple[bool, bool]:
    """Return whether to run Games and whether to run other product checks."""
    games = other = False
    for path in paths:
        if path in SHARED_MODULES or path in {
            "pyproject.toml", "uv.lock", "requirements.txt", "requirements-dev.txt", "scripts/game_store_scope.py",
            "scripts/run_product_checks.py", ".github/workflows/ci.yml",
        }:
            return True, True
        if path.startswith("docs/") or path in {"README.md", "AGENTS.md"}:
            continue
        if path.startswith(("aurix_games/", "web/game-store/", "config/games/")) or (
            is_game_test(path) or path in GAME_SCRIPTS | GAME_TOOLS
        ):
            games = True
        elif path.startswith((
            "aurix_vpn/", "aurix_ai/", "web/vpn-", "web/ai-app/", "pay_monitor/",
            "test_vpn_", "test_aurix_ai_", "test_telegram_", "test_outline_",
        )) or path == "app.py":
            other = True
        else:
            return True, True
    return games, other


def changed_scope(base: str, head: str, *, pull_request: bool, root: Path = ROOT) -> tuple[bool, bool]:
    if any(re.fullmatch(r"[0-9a-f]{40}", sha) is None or sha == "0" * 40 for sha in (base, head)):
        return True, True
    try:
        if pull_request:
            base = subprocess.check_output(
                ["git", "merge-base", base, head], cwd=root, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        paths = subprocess.check_output(
            ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--"], cwd=root,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return True, True
    return classify([path.decode("utf-8", errors="surrogateescape") for path in paths.split(b"\0") if path])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="")
    parser.add_argument("--pull-request", action="store_true")
    args = parser.parse_args()
    games, other = changed_scope(args.base, args.head, pull_request=args.pull_request)
    print(f"games={str(games).lower()}\nother={str(other).lower()}")


if __name__ == "__main__":
    main()
