"""Read-only operational checks across the three Games workflow stores."""

from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import sqlite3
import time


def audit_fulfillment(orders_db, ledger_db, tasks_db, *, limit=100):
    """Return sanitized findings; concurrent writes can produce transient findings."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("audit limit must be between 1 and 1000")
    paths = [Path(value).resolve(strict=True).as_uri() + "?mode=ro" for value in (orders_db, ledger_db, tasks_db)]
    with closing(sqlite3.connect(paths[0], uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("ATTACH DATABASE ? AS ledger", (paths[1],))
        connection.execute("ATTACH DATABASE ? AS tasks", (paths[2],))
        connection.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + 5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        rows = connection.execute("""
            WITH ids AS (
                SELECT order_id FROM game_orders
                UNION SELECT order_id FROM ledger.game_order_reservations
                UNION SELECT order_id FROM tasks.game_fulfillment_tasks
            ), facts AS (
                SELECT ids.order_id, o.state, o.provider_order_id AS order_receipt,
                    t.status AS task_status, t.provider_order_id AS task_receipt,
                    r.status AS reservation_status,
                    CASE WHEN o.order_id IS NULL THEN 'orphan_workflow_record'
                         WHEN o.state = 'awaiting_payment' AND r.status = 'reserved'
                              THEN 'payment_handoff_pending'
                         WHEN t.provider_order_id IS NOT NULL AND o.provider_order_id IS NOT NULL
                              AND t.provider_order_id != o.provider_order_id THEN 'receipt_conflict'
                         WHEN o.state IN ('submitted','processing','delivered')
                              AND r.status = 'reserved' THEN 'accepted_funds_not_captured'
                         WHEN o.state IN ('paid_reserved','fulfillment_queued') AND r.order_id IS NULL
                              THEN 'paid_reservation_missing'
                         WHEN o.state IN ('paid_reserved','fulfillment_queued')
                              AND t.order_id IS NULL THEN 'queue_handoff_missing'
                         WHEN t.provider_order_id IS NOT NULL AND o.state IN ('fulfillment_queued','unknown')
                              THEN 'receipt_handoff_pending'
                         WHEN o.state IN ('submitted','processing','delivered')
                              AND r.order_id IS NULL THEN 'accepted_reservation_missing'
                         WHEN o.state IN ('submitted','processing','delivered')
                              AND r.status IN ('released','refunded') THEN 'accepted_funds_released'
                         WHEN t.provider_order_id IS NOT NULL AND t.status<>'submitted'
                              AND o.state IN ('submitted','processing','delivered','failed','reconciliation')
                              THEN 'accepted_task_handoff_pending'
                         WHEN t.status = 'failed' AND r.status = 'reserved'
                              THEN 'failed_task_funds_reserved'
                    END AS issue
                FROM ids LEFT JOIN game_orders o USING(order_id)
                    LEFT JOIN ledger.game_order_reservations r USING(order_id)
                    LEFT JOIN tasks.game_fulfillment_tasks t USING(order_id)
            )
            SELECT order_id, state, task_status, reservation_status, issue,
                   COUNT(*) OVER () AS total_findings
            FROM facts WHERE issue IS NOT NULL ORDER BY order_id LIMIT ?
        """, (limit,)).fetchall()
    return {
        "findings": [{key: row[key] for key in ("order_id", "state", "task_status", "reservation_status", "issue")} for row in rows],
        "total_findings": rows[0]["total_findings"] if rows else 0,
        "truncated": bool(rows and rows[0]["total_findings"] > len(rows)),
        "read_only": True,
    }


def audit_money_integrity(orders_db, ledger_db, tasks_db, *, limit=100):
    """Observe local accounting and handoffs, without asserting bank reconciliation."""
    workflow = audit_fulfillment(orders_db, ledger_db, tasks_db, limit=limit)
    paths = [Path(value).resolve(strict=True).as_uri() + "?mode=ro"
             for value in (orders_db, ledger_db, tasks_db)]
    with closing(sqlite3.connect(paths[0], uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("ATTACH DATABASE ? AS ledger", (paths[1],))
        db.execute("ATTACH DATABASE ? AS tasks", (paths[2],))
        db.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + 5
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("BEGIN")
        counts = {
            "orders": db.execute("SELECT COUNT(*) FROM game_orders").fetchone()[0],
            "accounts": db.execute("SELECT COUNT(*) FROM ledger.game_customer_accounts").fetchone()[0],
            "entries": db.execute("SELECT COUNT(*) FROM ledger.game_ledger_entries").fetchone()[0],
            "reservations": db.execute("SELECT COUNT(*) FROM ledger.game_order_reservations").fetchone()[0],
        }
        checks = ["""
            SELECT 'account' AS scope, a.customer_id AS reference, 'account_balance_drift' AS issue
            FROM ledger.game_customer_accounts a
            WHERE a.available_minor <> COALESCE((SELECT SUM(e.available_delta_minor)
                    FROM ledger.game_ledger_entries e WHERE e.customer_id=a.customer_id),0)
               OR a.reserved_minor <> COALESCE((SELECT SUM(e.reserved_delta_minor)
                    FROM ledger.game_ledger_entries e WHERE e.customer_id=a.customer_id),0)
            UNION ALL
            SELECT 'account', a.customer_id, 'reservation_balance_drift'
            FROM ledger.game_customer_accounts a
            WHERE a.reserved_minor <> COALESCE((SELECT SUM(r.amount_minor)
                FROM ledger.game_order_reservations r
                WHERE r.customer_id=a.customer_id AND r.status='reserved'),0)
            UNION ALL
            SELECT 'entry', CAST(entry_id AS TEXT), 'entry_running_balance_drift'
            FROM (
                SELECT entry_id, available_after_minor, reserved_after_minor,
                    SUM(available_delta_minor) OVER (PARTITION BY customer_id ORDER BY entry_id) av,
                    SUM(reserved_delta_minor) OVER (PARTITION BY customer_id ORDER BY entry_id) rv
                FROM ledger.game_ledger_entries
            ) WHERE available_after_minor<>av OR reserved_after_minor<>rv
            UNION ALL
            SELECT 'entry', CAST(e.entry_id AS TEXT), 'entry_account_mismatch'
            FROM ledger.game_ledger_entries e
            LEFT JOIN ledger.game_customer_accounts a USING(customer_id)
            WHERE a.customer_id IS NULL OR a.currency<>e.currency
            UNION ALL
            SELECT 'order', r.order_id, 'reservation_identity_mismatch'
            FROM ledger.game_order_reservations r
            LEFT JOIN ledger.game_customer_accounts a USING(customer_id)
            LEFT JOIN game_orders o USING(order_id)
            WHERE a.customer_id IS NULL OR a.currency<>r.currency OR o.order_id IS NULL
                OR o.customer_id<>r.customer_id OR NOT json_valid(o.snapshot_json)
                OR r.amount_minor IS NOT CASE WHEN json_valid(o.snapshot_json)
                    THEN json_extract(o.snapshot_json,'$.amount_minor') END
                OR r.currency IS NOT CASE WHEN json_valid(o.snapshot_json)
                    THEN json_extract(o.snapshot_json,'$.currency') END
            UNION ALL
            SELECT 'order', t.order_id, 'unresolved_supplier_dispatch'
            FROM tasks.game_fulfillment_tasks t LEFT JOIN game_orders o USING(order_id)
            WHERE t.dispatch_started_at IS NOT NULL AND t.provider_order_id IS NULL
                AND o.provider_order_id IS NULL
        """]
        optional = {
            "game_payment_transfer_identity_claims": """
                SELECT 'order', c.order_id, 'settlement_handoff_pending'
                FROM game_payment_transfer_identity_claims c
                LEFT JOIN ledger.game_ledger_entries e ON e.event_key=c.incoming_transfer_key
                WHERE c.resolution='settlement' AND (e.entry_id IS NULL
                    OR e.kind<>'verified_transfer' OR e.order_id<>c.order_id)
            """,
            "game_payment_review_cases": """
                SELECT 'order', order_id, 'payment_decision_not_applied'
                FROM game_payment_review_cases WHERE decision_json IS NOT NULL AND applied_at IS NULL
            """,
            "game_payment_external_refund_authorizations": """
                SELECT 'order', a.order_id, 'external_refund_not_completed'
                FROM game_payment_external_refund_authorizations a
                LEFT JOIN game_payment_external_refund_completions c USING(order_id,revision)
                WHERE c.order_id IS NULL
                UNION ALL
                SELECT 'order', c.order_id, 'bank_and_wallet_refund_conflict'
                FROM game_payment_external_refund_completions c
                JOIN ledger.game_order_reservations r USING(order_id)
                WHERE r.status IN ('released','refunded')
                UNION ALL
                SELECT 'order', c.order_id, 'bank_refund_with_allocated_transfer'
                FROM game_payment_external_refund_completions c
                JOIN game_payment_external_refund_authorizations a USING(order_id,revision)
                JOIN ledger.game_ledger_entries e ON e.event_key=a.incoming_transfer_key
                WHERE e.kind='verified_transfer'
            """,
            "game_order_webhook_inbox": """
                SELECT 'event', i.event_id, 'unmatched_supplier_event'
                FROM game_order_webhook_inbox i LEFT JOIN game_orders o USING(provider_order_id)
                WHERE i.processed_at IS NULL AND o.order_id IS NULL
            """,
        }
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        unavailable = []
        ledger_tables={row[0] for row in db.execute("SELECT name FROM ledger.sqlite_master WHERE type='table'")}
        if 'game_bank_refund_intents' in ledger_tables:
            checks.append("""
                SELECT 'order', b.order_id, 'settled_bank_refund_pending'
                FROM ledger.game_bank_refund_intents b WHERE b.completion_json IS NULL
                UNION ALL
                SELECT 'order', b.order_id, 'settled_bank_refund_order_handoff'
                FROM ledger.game_bank_refund_intents b LEFT JOIN game_orders o USING(order_id)
                WHERE b.completion_json IS NOT NULL AND (o.order_id IS NULL OR o.state<>'refunded')
                UNION ALL
                SELECT 'order', b.order_id, 'bank_and_wallet_refund_conflict'
                FROM ledger.game_bank_refund_intents b JOIN ledger.game_order_reservations r USING(order_id)
                WHERE r.status IN ('released','refunded')
            """)
        for table, query in optional.items():
            if (table in tables and (table != "game_payment_external_refund_authorizations"
                    or "game_payment_external_refund_completions" in tables)):
                checks.append(query)
            else:
                unavailable.append(table)
        rows = db.execute("WITH findings AS (" + " UNION ALL ".join(checks) + """
            ) SELECT *, COUNT(*) OVER () AS total_findings FROM findings
            ORDER BY scope, reference, issue LIMIT ?""", (limit,)).fetchall()
    findings = [{"scope": "order", "reference": row["order_id"], "issue": row["issue"]}
                for row in workflow["findings"]]
    findings += [{"scope": row["scope"],
                  "reference": ("account-" + hashlib.sha256(row["reference"].encode()).hexdigest()[:16]
                                if row["scope"] == "account" else row["reference"]),
                  "issue": row["issue"]} for row in rows]
    total = workflow["total_findings"] + (rows[0]["total_findings"] if rows else 0)
    return {
        "status": "attention" if total else "incomplete" if unavailable else "empty" if not counts["entries"] else "clear",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts, "findings": findings[:limit], "total_findings": total,
        "truncated": total > limit, "unavailable_checks": unavailable, "read_only": True,
        "consistency": "cross_store_observation",
        "merchant_funds_verified": False, "supplier_funds_verified": False,
    }
