"""Pure rendering for the administrator capacity view."""

from __future__ import annotations

from typing import Any


def render_capacity_text(snapshot: dict[str, Any]) -> str:
    """Render a bounded capacity snapshot without host, database, or network access."""
    advice = snapshot.get("scale_advice") or {}
    lines = [
        "📈 AuriX Server Capacity",
        "",
        f"Active paid keys: {snapshot.get('active_keys', 0)} · pending jobs: {snapshot.get('pending_jobs', 0)}",
        "Observed values come from Outline; limits and plan slots are owner policy.",
    ]
    status = str(advice.get("status") or "unknown")
    icon = {"stable": "🟢", "prepare": "🟡", "urgent": "🔴", "blocked": "🔴", "unconfigured": "⚪️"}.get(status, "⚪️")
    utilization = advice.get("utilization_percent")
    utilization_text = "" if utilization is None else f" · {float(utilization):g}% allocated"
    lines.extend(
        [
            "",
            f"{icon} Scale posture: {status.title()}{utilization_text}",
            str(advice.get("message") or "Capacity recommendation unavailable."),
            "Scaling mode: assisted · no automatic purchase or server deletion.",
        ]
    )
    if status in {"prepare", "urgent"}:
        observed = int(advice.get("consecutive_observations") or 0)
        required = int(advice.get("required_observations") or 2)
        gate = "ready" if advice.get("observation_ready") else "observe again before queueing"
        lines.append(f"Scale evidence: {observed}/{required} consecutive observations · {gate}")
    for item in snapshot.get("servers") or []:
        lines.extend(_server_capacity_lines(item))
    if not snapshot.get("servers"):
        lines.extend(["", "No environment-configured Outline servers were registered."])
    return "\n".join(lines)[:4096]


def _server_capacity_lines(item: dict[str, Any]) -> list[str]:
    max_keys = item.get("max_keys")
    remaining = item.get("remaining_key_slots")
    key_limit = (
        "not capped"
        if max_keys is None
        else f"{remaining} saleable left · max {max_keys}, reserve {item.get('reserved_keys') or 0}"
    )
    traffic = item.get("remote_transfer_bytes")
    traffic_text = "-" if traffic is None else f"{int(traffic) / 1_000_000_000:.1f} GB / 30d"
    orphan_count = int(item.get("remote_orphan_key_count") or 0)
    budget = item.get("monthly_traffic_bytes")
    commitment = int(item.get("committed_traffic_bytes") or 0)
    connectivity = item.get("connectivity") or {}
    connectivity_text = (
        f"Connectivity: {connectivity.get('provider_name', connectivity.get('provider_id', 'unknown'))} · "
        f"{connectivity.get('region_name', connectivity.get('region_id', 'unknown'))} · "
        f"{connectivity.get('transport_name', connectivity.get('protocol', 'unknown'))}"
        if connectivity
        else "Connectivity: registry pending"
    )
    lines = [
        "",
        f"{'🟢' if item.get('health_status') == 'healthy' else '🔴'} {item.get('label') or item.get('server_id')} · {str(item.get('lifecycle_state') or 'active').title()}",
        (
            "Health evidence: "
            f"{str(item.get('health_status') or 'unknown')} · last probe "
            f"{float(item.get('health_last_latency_ms')):g} ms"
            if item.get("health_last_latency_ms") is not None
            else "Health evidence: no latency recorded"
        ),
        connectivity_text,
        (
            "Admission: ✅ eligible"
            if item.get("admission_status") == "eligible"
            else "Admission: ⛔ blocked · "
            + ", ".join(str(value).replace("_", " ") for value in item.get("admission_blockers") or [])
        ),
        f"Remote keys: {item.get('remote_key_count') or 0} · {key_limit}",
        (
            f"⚠️ Untracked remote keys: {orphan_count} · audit before strict allocation"
            if orphan_count
            else "Untracked remote keys: 0"
        ),
        f"Traffic observed: {traffic_text}",
        "Traffic allocation: "
        + (
            f"{commitment / 1_000_000_000:g}/{int(budget) / 1_000_000_000:g} GB committed"
            if budget
            else "monitor only"
        ),
        _drain_text(item),
    ]
    lines.append(_policy_text(item, orphan_count))
    lines.append(_plan_slots_text(item))
    tier_allocations = item.get("tier_allocations") or []
    if tier_allocations:
        labels = {"FREE300MB": "Daily", "FREE3GB": "Monthly", "PROMO": "Promo"}
        lines.append(
            "Free/promo slots: "
            + " · ".join(
                f"{labels.get(allocation['tier_code'], allocation['tier_code'])} "
                f"{allocation['remaining_slots']}/{allocation['slot_limit']}"
                for allocation in tier_allocations
            )
        )
    return lines


def _drain_text(item: dict[str, Any]) -> str:
    if item.get("drain_ready_to_retire"):
        return "Drain: ✅ empty and ready to retire"
    blockers = ", ".join(
        label
        for label, value in (
            ("free keys", item.get("active_free_key_count")),
            ("paid keys", item.get("active_paid_key_count")),
            ("open orders", item.get("open_order_count")),
            ("pending setup", item.get("pending_provisioning_count")),
            ("remote keys", item.get("remote_key_count")),
            ("unreviewed remote", item.get("remote_orphan_key_count")),
        )
        if value is None or int(value or 0) > 0
    )
    return f"Drain: ⏳ {blockers}"


def _policy_text(item: dict[str, Any], orphan_count: int) -> str:
    status = str(item.get("allocation_policy_status") or "unknown")
    total = int(item.get("allocation_total_slots") or 0)
    gap = item.get("allocation_remaining_slots")
    if status == "overallocated":
        return f"⚠️ Policy: over-allocated {total} slots; {abs(int(gap or 0))} above saleable headroom"
    if status == "audit_required":
        return f"Policy audit: ⚠️ {orphan_count} untracked key(s) must be classified before strict validation"
    if status == "unconfigured":
        return "Policy: capacity is not declared; node cannot be sized safely"
    return f"Policy: ✅ {total} allocated slot(s) · {max(0, int(gap or 0))} unallocated"


def _plan_slots_text(item: dict[str, Any]) -> str:
    allocations = item.get("allocations") or []
    if not allocations:
        return "Plan slots: not allocated (server headroom only)"
    return "Plan slots: " + " · ".join(
        f"{allocation['name']} {allocation['remaining_slots']}/{allocation['slot_limit']}"
        for allocation in allocations
    )
