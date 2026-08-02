"""Idempotent migration: convert all existing per-pallet-count tiers to the
new 5-band structure.

Run via odoo-bin shell:
    cd /opt/odoo/odoo18 && sudo -u odoo18 python3 odoo-bin shell \
        -c /etc/odoo18.conf -d Prod-db --no-http \
        < /opt/odoo/custom-addons/prema_logistics_booking/scripts/migrate_to_banded_tiers.py

Safe to re-run — same formula as action_regenerate_tiers, so re-running
produces identical tiers (idempotent).
"""
import sys


def _build_banded_tiers(target, planning=8):
    """Duplicate of logistics.rate.plan._build_banded_tiers()
    (not importable in shell context)."""
    base = target / max(planning, 1)
    ftl_flat = max(round(target * 1.07, 0), 500.0)
    return [
        (1,  1,   max(round(base * 1.73, 0), 50.0), "per_unit", 0),
        (2,  4,   max(round(base * 1.47, 0), 50.0), "per_unit", 0),
        (5,  6,   max(round(base * 1.20, 0), 50.0), "per_unit", 0),
        (7,  10,  max(round(base * 1.08, 0), 50.0), "per_unit", ftl_flat),
        (11, 13,  ftl_flat,                            "flat",     0),
    ]


RatePlan = env["logistics.rate.plan"]
Tier = env["logistics.rate.tier"]
Lane = env["logistics.lane"]

# ── 1. Update lane.target_load_pallets from 6 → 8 FIRST ─────────────────
# Must run BEFORE tier generation so the formula uses the updated planning
# value and re-runs are fully idempotent.

lanes_updated = 0
for lane in Lane.search([("target_load_pallets", "=", 6)]):
    lane.write({"target_load_pallets": 8})
    lanes_updated += 1

print(f"Updated target_load_pallets 6→8: {lanes_updated} lanes")

# ── 2. Migrate all active rate plans to banded tiers ──────────────────

plans = RatePlan.search([("active", "=", True)])
converted = 0
skipped = 0
before_total_tiers = Tier.search_count([("tier_type", "=", "pallet")])

for plan in plans:
    lane = plan.service_offering_id.lane_id
    target = plan.revenue_target or lane.revenue_target or 500
    planning = lane.target_load_pallets or 8

    # Only migrate plans that actually have pallet tiers
    pallet_tiers = Tier.search([
        ("rate_plan_id", "=", plan.id),
        ("tier_type", "=", "pallet"),
    ])
    if not pallet_tiers:
        skipped += 1
        continue

    bands = _build_banded_tiers(target, planning)
    pallet_tiers.unlink()
    for min_q, max_q, rate, calc_method, cap in bands:
        Tier.create({
            "rate_plan_id": plan.id,
            "tier_type": "pallet",
            "min_qty": min_q,
            "max_qty": max_q,
            "calc_method": calc_method,
            "rate": rate,
            "cap_amount": cap,
        })
    converted += 1

after_total_tiers = Tier.search_count([("tier_type", "=", "pallet")])

# Inactive plans keep their original tiers (not migrated).  The assertion
# must account for those untouched rows.
inactive_plan_tier_count = Tier.search_count([
    ("tier_type", "=", "pallet"),
    ("rate_plan_id.active", "=", False),
])
expected_active = converted * 5
expected_total = expected_active + inactive_plan_tier_count

print(f"Converted: {converted} rate plans")
print(f"Skipped (no pallet tiers): {skipped}")
print(f"Inactive-plan tiers (untouched): {inactive_plan_tier_count}")
print(f"Tier rows: {before_total_tiers} → {after_total_tiers}")
print(f"Expected: {expected_total} ({expected_active} active × 5 bands + {inactive_plan_tier_count} inactive)")

assert after_total_tiers == expected_total, (
    f"Tier count mismatch: {after_total_tiers} != {expected_total}"
)

env.cr.commit()
print("=== MIGRATION COMPLETE ===")
