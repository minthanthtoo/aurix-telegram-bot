#!/usr/bin/env python3
"""Run bounded throughput tests with ping captured only during the transfer.

This is intentionally a small Linux-host probe for capacity evidence. It does
not test an Outline client path and it is not an SLA measurement. The default
download is a single 10 MiB file from a neutral endpoint because the
Cloudflare speed endpoint used during the first baseline rejected large
requests and rate-limited bursts of repeated chunks.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any


DEFAULT_DOWNLOAD_URL = "https://proof.ovh.net/files/10Mb.dat"
DEFAULT_UPLOAD_URL = "https://speed.cloudflare.com/__up"
DEFAULT_USER_AGENT = "AuriX-capacity-check/2026-09-07"


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def parse_ping(output: str) -> dict[str, Any]:
    replies = [float(match) for match in re.findall(r"time[=<]([0-9.]+) ms", output)]
    summary = re.search(
        r"(\d+) packets transmitted, (\d+) (?:packets )?received, ([0-9.]+)% packet loss",
        output,
    )
    if summary:
        sent = int(summary.group(1))
        received = int(summary.group(2))
        loss_pct = float(summary.group(3))
    else:
        sent = None
        received = len(replies)
        loss_pct = None
    return {
        "sent": sent,
        "received": received,
        "loss_pct": loss_pct,
        "reply_count": len(replies),
        "avg_ms": (sum(replies) / len(replies)) if replies else None,
        "p95_ms": percentile(replies, 0.95),
        "max_ms": max(replies) if replies else None,
    }


def stop_ping(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        return process.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()[0]


def curl_result(
    direction: str,
    url: str,
    expected_bytes: int,
    user_agent: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    args = [
        "curl",
        "-A",
        user_agent,
        "-f",
        "-sS",
        "-o",
        "/dev/null",
        "--connect-timeout",
        str(min(timeout_seconds, 10)),
        "--max-time",
        str(timeout_seconds),
    ]
    source: subprocess.Popen[bytes] | None = None
    if direction == "download":
        args.extend(["-L", "-w", "%{http_code}\t%{size_download}\t%{time_total}", url])
    else:
        source = subprocess.Popen(
            ["head", "-c", str(expected_bytes), "/dev/zero"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        args.extend(
            [
                "-X",
                "POST",
                "--data-binary",
                "@-",
                "-w",
                "%{http_code}\t%{size_upload}\t%{time_total}",
                url,
            ]
        )

    started = time.monotonic()
    try:
        process = subprocess.run(
            args,
            stdin=source.stdout if source else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        if source:
            assert source.stdout is not None
            source.stdout.close()
            source.wait()
    wall_seconds = time.monotonic() - started
    fields = process.stdout.strip().split()
    http_code = fields[-3] if len(fields) >= 3 else None
    actual_bytes = int(float(fields[-2])) if len(fields) >= 3 else None
    transfer_seconds = float(fields[-1]) if len(fields) >= 3 else None
    valid = (
        process.returncode == 0
        and http_code is not None
        and http_code.startswith("2")
        and actual_bytes == expected_bytes
        and transfer_seconds is not None
        and transfer_seconds > 0
    )
    return {
        "valid": valid,
        "returncode": process.returncode,
        "http_code": http_code,
        "expected_bytes": expected_bytes,
        "actual_bytes": actual_bytes,
        "transfer_seconds": transfer_seconds,
        "wall_seconds": round(wall_seconds, 3),
        "mbps": round(actual_bytes * 8 / transfer_seconds / 1_000_000, 2)
        if actual_bytes is not None and transfer_seconds
        else None,
        "error": process.stderr.strip()[-240:] or None,
    }


def run_once(args: argparse.Namespace, direction: str) -> dict[str, Any]:
    expected_bytes = args.download_bytes if direction == "download" else args.upload_bytes
    if direction == "download":
        url = args.download_url.format(bytes=expected_bytes)
    else:
        url = args.upload_url
    ping_process = subprocess.Popen(
        ["ping", "-n", "-i", str(args.ping_interval), args.ping_target],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        transfer = curl_result(
            direction,
            url,
            expected_bytes,
            args.user_agent,
            args.timeout_seconds,
        )
    finally:
        ping_output = stop_ping(ping_process)
    ended = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "host": socket.gethostname(),
        "direction": direction,
        "started_utc": started,
        "ended_utc": ended,
        "download_url": args.download_url if direction == "download" else None,
        "upload_url": args.upload_url if direction == "upload" else None,
        "ping_target": args.ping_target,
        "ping_interval_seconds": args.ping_interval,
        "transfer": transfer,
        "loaded_ping": parse_ping(ping_output),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direction", choices=("download", "upload", "both"), default="both")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--cooldown-seconds", type=float, default=5.0)
    parser.add_argument("--download-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--upload-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--download-url", default=DEFAULT_DOWNLOAD_URL)
    parser.add_argument("--upload-url", default=DEFAULT_UPLOAD_URL)
    parser.add_argument("--ping-target", default="1.1.1.1")
    parser.add_argument("--ping-interval", type=float, default=0.2)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    if args.download_bytes < 1 or args.upload_bytes < 1:
        parser.error("transfer byte counts must be positive")
    return args


def main() -> int:
    args = parse_args()
    directions = ["download", "upload"] if args.direction == "both" else [args.direction]
    for direction in directions:
        for repetition in range(args.repetitions):
            result = run_once(args, direction)
            result["repetition"] = repetition + 1
            print(json.dumps(result, sort_keys=True), flush=True)
            if repetition + 1 < args.repetitions or direction != directions[-1]:
                time.sleep(args.cooldown_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
