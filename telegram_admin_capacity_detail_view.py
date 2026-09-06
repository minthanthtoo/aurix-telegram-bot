"""Pure renderers for admin capacity and remote-inventory panels."""

from __future__ import annotations

from typing import Any, Callable, Iterable


def render_server_allocation(
    server: dict[str, Any],
    server_id: str,
    plans: Iterable[Any],
    *,
    is_owner: bool,
) -> tuple[str, list[list[tuple[str, str]]]]:
    """Render endpoint policy, lifecycle, and tier allocation controls."""
    max_keys = server.get("max_keys")
    traffic = server.get("monthly_traffic_bytes")
    lines = [
        f"⚙️ {server.get('label') or server_id}",
        f"Lifecycle: {str(server.get('lifecycle_state') or 'active').title()} · {'admission enabled' if server.get('enabled') else 'admission disabled'}",
        (
            "Connectivity: "
            f"{(server.get('connectivity') or {}).get('provider_name', 'unknown')} · "
            f"{(server.get('connectivity') or {}).get('region_name', 'unknown')} · "
            f"{(server.get('connectivity') or {}).get('transport_name', 'unknown')}"
        ),
        f"Health: {server.get('health_status')} · remote keys: {server.get('remote_key_count') or 0}",
        (
            "Probe: "
            f"{float(server.get('health_last_latency_ms')):g} ms · "
            f"success streak {int(server.get('health_success_streak') or 0)} · "
            f"failure streak {int(server.get('health_failure_streak') or 0)}"
            if server.get("health_last_latency_ms") is not None
            else "Probe: no latency recorded"
        ),
        (
            f"Inventory audit: ⚠️ {int(server.get('remote_orphan_key_count') or 0)} untracked"
            if int(server.get("remote_orphan_key_count") or 0)
            else "Inventory audit: ✅ all observed keys are managed"
        ),
        f"Maximum keys: {max_keys or 'not capped'} · protected headroom: {server.get('reserved_keys') or 0}",
        "Monthly traffic budget: "
        + (f"{int(traffic) / 1_000_000_000:g} GB" if traffic else "monitor only"),
        "",
        "Choose declared server capacity, then allocate paid and free/promo tiers. Changes apply to new issuance; existing keys are never silently moved.",
        "Drain mode blocks new issuance but keeps existing keys alive. Retirement is allowed only after every local and remotely observed key/order/setup is gone.",
    ]
    rows: list[list[tuple[str, str]]] = [
        [("🔎 Remote inventory", f"a:I:{server_id}:present:0")],
        [("🔁 Migrate active keys", f"a:G:{server_id}:0")],
        [("Keys 25", f"a:C:{server_id}|keys|25"), ("50", f"a:C:{server_id}|keys|50"), ("100", f"a:C:{server_id}|keys|100")],
        [("Reserve 1", f"a:C:{server_id}|reserve|1"), ("2", f"a:C:{server_id}|reserve|2"), ("5", f"a:C:{server_id}|reserve|5")],
        [("Traffic 500GB", f"a:C:{server_id}|traffic|500"), ("1TB", f"a:C:{server_id}|traffic|1000"), ("2TB", f"a:C:{server_id}|traffic|2000")],
    ]
    lifecycle = str(server.get("lifecycle_state") or "active")
    if is_owner:
        if lifecycle == "active":
            rows.append([("🚧 Start drain", f"a:L:{server_id}|draining")])
        elif lifecycle == "draining":
            rows.append([("▶ Resume admission", f"a:L:{server_id}|active"), ("⏹ Retire when empty", f"a:L:{server_id}|retired")])
        else:
            rows.append([("▶ Re-open endpoint", f"a:L:{server_id}|active")])
    allocations = {item["plan_code"]: item for item in server.get("allocations", [])}
    for plan in plans:
        current = int((allocations.get(plan.code) or {}).get("slot_limit") or 0)
        lines.append(f"{plan.name}: {current} allocated")
        rows.append([(f"{plan.name} 0", f"a:C:{server_id}|{plan.code}|0"), ("10", f"a:C:{server_id}|{plan.code}|10"), ("25", f"a:C:{server_id}|{plan.code}|25"), ("50", f"a:C:{server_id}|{plan.code}|50")])
    tier_labels = {"FREE300MB": "Daily 300 MB", "FREE3GB": "Monthly 3 GB", "PROMO": "Promo"}
    tier_allocations = {item["tier_code"]: item for item in server.get("tier_allocations", [])}
    for tier_code, label in tier_labels.items():
        current = int((tier_allocations.get(tier_code) or {}).get("slot_limit") or 0)
        lines.append(f"{label}: {current} allocated")
        rows.append([(f"{label} 0", f"a:C:{server_id}|{tier_code}|0"), ("10", f"a:C:{server_id}|{tier_code}|10"), ("25", f"a:C:{server_id}|{tier_code}|25"), ("50", f"a:C:{server_id}|{tier_code}|50")])
    rows.append([("◀ All Servers", "a:n:capacity"), ("🔄 Refresh", f"a:S:{server_id}")])
    return "\n".join(lines)[:4096], rows


def render_remote_inventory(
    all_rows: list[dict[str, Any]],
    server_id: str,
    status: str,
    page: int,
    *,
    is_owner: bool,
    inventory_bytes: Callable[[Any], str],
    format_datetime: Callable[[Any], str],
) -> tuple[str, list[list[tuple[str, str]]]]:
    """Render a paginated, non-secret remote inventory audit."""
    rows = [
        row for row in all_rows
        if status == "all" or str(row.get("status") or "") == status
    ]
    page_size = 5
    pages = max(1, (len(rows) + page_size - 1) // page_size)
    current_page = max(0, min(int(page or 0), pages - 1))
    current = rows[current_page * page_size : (current_page + 1) * page_size]
    present_count = sum(1 for row in all_rows if row.get("status") == "present")
    missing_count = sum(1 for row in all_rows if row.get("status") == "missing")
    unreviewed_count = sum(
        1 for row in all_rows
        if row.get("status") == "present"
        and not row.get("managed")
        and str(row.get("review_state") or "unreviewed") != "accepted_external"
    )
    lines = [
        f"🔎 Remote inventory · {server_id}",
        "",
        f"Present {present_count} · Missing {missing_count} · showing {status}",
        "IDs and telemetry only; access URLs are deliberately never shown here.",
        f"Unreviewed external keys: {unreviewed_count}",
        f"Page {current_page + 1}/{pages}",
    ]
    for row in current:
        key_id = str(row.get("outline_key_id") or "-")
        name = str(row.get("remote_name") or "unnamed")[:48]
        state = "✅ managed" if row.get("managed") else "⚠️ untracked"
        review_state = str(row.get("review_state") or "unreviewed")
        if not row.get("managed") and review_state == "accepted_external":
            state = "⚪ untracked · reviewed external"
        if row.get("status") == "missing":
            state = "🗃 missing · " + ("was managed" if row.get("managed") else "was untracked")
        lines.extend(["", f"• `{key_id}` · {name}", f"  {state} · usage {inventory_bytes(row.get('last_usage_bytes'))}", f"  last seen {format_datetime(row.get('last_seen_at'))}"])
    if not current:
        lines.extend(["", "No audit records match this filter."])
    rows_markup: list[list[tuple[str, str]]] = [
        [(f"✅ Present ({present_count})", f"a:I:{server_id}:present:0"), (f"🗃 Missing ({missing_count})", f"a:I:{server_id}:missing:0")],
        [("📋 All", f"a:I:{server_id}:all:0")],
    ]
    navigation: list[tuple[str, str]] = []
    if current_page > 0:
        navigation.extend([("⏮ First", f"a:I:{server_id}:{status}:0"), ("◀ Previous", f"a:I:{server_id}:{status}:{current_page - 1}")])
    navigation.append((f"{current_page + 1}/{pages}", f"a:I:{server_id}:{status}:{current_page}"))
    if current_page + 1 < pages:
        navigation.extend([("Next ▶", f"a:I:{server_id}:{status}:{current_page + 1}"), ("Last ⏭", f"a:I:{server_id}:{status}:{pages - 1}")])
    rows_markup.append(navigation)
    if is_owner:
        for row in current:
            if row.get("status") != "present" or row.get("managed"):
                continue
            key_id = str(row.get("outline_key_id") or "").strip()
            if not key_id:
                continue
            review_state = str(row.get("review_state") or "unreviewed")
            next_state = "unreviewed" if review_state == "accepted_external" else "accepted_external"
            action_data = f"a:R:{server_id}|{key_id}|{next_state}|{current_page}"
            if len(action_data.encode("utf-8")) <= 64:
                label = "↩ Reopen" if review_state == "accepted_external" else "✅ Mark reviewed"
                rows_markup.append([(f"{label} · {key_id[:12]}", action_data)])
    rows_markup.append([("🔄 Refresh", f"a:I:{server_id}:{status}:{current_page}"), ("⬅ Server policy", f"a:S:{server_id}")])
    return "\n".join(lines)[:4096], rows_markup
