#!/usr/bin/env python3
"""Explicitly provision/check the private Supabase recovery bucket."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploy import offsite_storage  # noqa: E402
from deploy.fleet_reconcile import FleetError, load_dotenv  # noqa: E402
from supabase_storage import ReceiptStorageError, SupabaseObjectStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ensure",))
    parser.add_argument(
        "--env-file",
        default=os.environ.get("AURIX_FLEET_ENV_FILE", "/etc/aurix-bot/aurix.env"),
    )
    args = parser.parse_args()
    try:
        env = load_dotenv(Path(args.env_file), overwrite=False)
        store = offsite_storage.from_env({**env, **os.environ})
        if not isinstance(store, SupabaseObjectStore):
            raise FleetError("recovery_storage.py requires the Supabase backup backend")
        created = store.ensure_private_bucket()
    except (FleetError, ReceiptStorageError, OSError, ValueError) as exc:
        print(f"recovery storage failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "ready", "bucket": store.bucket, "created": created}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
