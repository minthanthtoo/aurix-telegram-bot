#!/usr/bin/env python3
"""Safely inventory the configured 9Router account and model capabilities.

Discovery is read-only. Optional probes make one small provider request and are
disabled by default because a successful probe can consume quota or credits.
The output never contains bearer credentials, prompts, or response bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from aurix_ai.capabilities import build_capability_report
from aurix_ai.router import AIRouterError, NineRouterClient


def _client(args: argparse.Namespace) -> NineRouterClient:
    base_url = (args.base_url or os.environ.get("AURIX_AI_ROUTER_BASE_URL", "")).strip()
    api_key = (args.api_key or os.environ.get("AURIX_AI_ROUTER_API_KEY", "")).strip()
    model = (args.model or os.environ.get("AURIX_AI_MODEL", "probe-model")).strip()
    return NineRouterClient(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=args.timeout,
    )


def _probe_chat(client: NineRouterClient, model: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = client.request_json(
            "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 8,
                "temperature": 0,
            },
        )
        return {
            "status": "ok",
            "model": result.get("model"),
            "usage": result.get("usage"),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except (AIRouterError, OSError, ValueError) as exc:
        return {
            "status": "error",
            "error": {"type": type(exc).__name__, "message": str(exc)[:240]},
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = _client(args)
    report = build_capability_report(client, include_quota=not args.no_quota)
    report["probe_policy"] = {
        "discovery_only": not bool(args.probe_chat),
        "chat_probe_requested": bool(args.probe_chat),
    }
    if args.probe_chat:
        report["probes"] = {"chat": _probe_chat(client, args.probe_chat)}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="9Router API base; defaults to AURIX_AI_ROUTER_BASE_URL")
    parser.add_argument("--api-key", help="router key; defaults to AURIX_AI_ROUTER_API_KEY")
    parser.add_argument("--model", help="configured model used for optional probe setup")
    parser.add_argument("--probe-chat", metavar="MODEL", help="send one small chat probe")
    parser.add_argument("--no-quota", action="store_true", help="skip /api/quota and /api/usage")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, help="also save the JSON report to this path")
    args = parser.parse_args(argv)
    try:
        report = run(args)
    except (AIRouterError, OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
