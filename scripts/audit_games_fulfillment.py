#!/usr/bin/env python3
"""Inspect Games workflow consistency without keys, supplier calls or writes.

Use explicit paths to the orders, ledger and fulfillment databases. Findings
are observations; repeat against a quiescent snapshot before taking action.
"""
import argparse
import json
import sqlite3

from aurix_games.fulfillment_audit import audit_fulfillment, audit_money_integrity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--orders-db", required=True)
    parser.add_argument("--ledger-db", required=True)
    parser.add_argument("--tasks-db", required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--money-integrity", action="store_true", help="include ledger and payment handoff checks")
    parser.add_argument("--summary", action="store_true", help="log aggregate counts without account, event or order references")
    args = parser.parse_args(argv)
    try:
        audit = audit_money_integrity if args.money_integrity else audit_fulfillment
        result = audit(args.orders_db, args.ledger_db, args.tasks_db, limit=args.limit)
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({"error": "audit unavailable: check database paths, schema and access"}))
        return 2
    if args.summary:
        print(json.dumps({"read_only": True, "total_findings": result["total_findings"],
            "status": result.get("status", "attention" if result["total_findings"] else "clear"),
            "unavailable_checks": len(result.get("unavailable_checks", [])), "counts": result.get("counts", {})}))
    else:
        print(json.dumps(result, indent=2))
    return 1 if result["total_findings"] else 2 if result.get("unavailable_checks") else 0


if __name__ == "__main__":
    raise SystemExit(main())
