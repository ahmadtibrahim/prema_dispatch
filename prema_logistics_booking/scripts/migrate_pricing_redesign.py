#!/usr/bin/env python3
"""Idempotent migration script for the Scheduled Shared LTL pricing redesign.

Run via odoo-bin shell:
    sudo -u odoo18 /opt/odoo/venv-18/bin/python3 /opt/odoo/odoo18/odoo-bin shell \\
        -c /etc/odoo18.conf -d Prod-db --no-http < scripts/migrate_pricing_redesign.py

Changes:
  1. Consolidate temperature offerings: keep dry, inactivate chilled/frozen
  2. Set planned_pallets on rate plans from lane.target_load_pallets
  3. Force recompute of lane names (human-readable region names)
  4. Copy estimated_one_way_cost from lane to rate plan
  5. Assert: all active plans have pallet tiers, no orphaned data
"""

import sys
import logging

_logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────

def _ensure_global_surcharge(env, code, name, calc_type, default_amount):
    """Idempotently ensure a global surcharge type exists."""
    SurchargeType = env["logistics.surcharge.type"]
    existing = SurchargeType.search([("code", "=", code)], limit=1)
    if existing:
        existing.write({
            "name": name,
            "calc_type": calc_type,
            "default_amount": default_amount,
            "is_global": True,
            "active": True,
        })
        return existing
    return SurchargeType.create({
        "code": code,
        "name": name,
        "calc_type": calc_type,
        "default_amount": default_amount,
        "is_global": True,
        "active": True,
    })


def _tier_count(rate_plan):
    return env["logistics.rate.tier"].search_count([
        ("rate_plan_id", "=", rate_plan.id),
        ("tier_type", "=", "pallet"),
    ])


# ── Main ────────────────────────────────────────────────────────────────

print("=" * 60)
print("Scheduled Shared LTL Pricing Redesign — Migration")
print("=" * 60)

Lane = env["logistics.lane"]
Offering = env["logistics.service.offering"]
RatePlan = env["logistics.rate.plan"]

# 1. Ensure global temperature surcharges exist
print("\n1. Ensuring global temperature surcharges...")
chilled = _ensure_global_surcharge(env, "TEMP_CHILLED", "Chilled Temperature Premium", "percent", 10.0)
frozen = _ensure_global_surcharge(env, "TEMP_FROZEN", "Frozen Temperature Premium", "percent", 15.0)
print(f"   TEMP_CHILLED: id={chilled.id} at {chilled.default_amount}%")
print(f"   TEMP_FROZEN:  id={frozen.id} at {frozen.default_amount}%")

# 2. Consolidate temperature offerings: keep dry, inactivate chilled/frozen
print("\n2. Consolidating temperature offerings...")
consolidated = 0
preserved = 0
lanes_processed = set()

for lane in Lane.search([("active", "=", True)]):
    dry_offerings = Offering.search([
        ("lane_id", "=", lane.id),
        ("temperature_mode", "=", "dry"),
        ("active", "=", True),
    ])
    if not dry_offerings:
        continue

    lanes_processed.add(lane.id)

    # Inactivate chilled/frozen offerings for this lane
    for temp_mode in ("chilled", "frozen"):
        temp_offerings = Offering.search([
            ("lane_id", "=", lane.id),
            ("temperature_mode", "=", temp_mode),
            ("active", "=", True),
        ])
        for off in temp_offerings:
            # Inactivate rate plans linked to this offering
            for rp in RatePlan.search([
                ("service_offering_id", "=", off.id),
                ("active", "=", True),
            ]):
                rp.write({"active": False})
            off.write({"active": False})
            consolidated += 1

    # Count preserved dry offerings
    preserved += len(dry_offerings)

print(f"   Lanes processed: {len(lanes_processed)}")
print(f"   Consolidated (chilled/frozen inactivated): {consolidated}")
print(f"   Preserved (dry offerings): {preserved}")

# 3. Update rate plans with planned_pallets and estimated cost
print("\n3. Updating rate plan commercial planning fields...")
updated_plans = 0
for rp in RatePlan.search([("active", "=", True)]):
    lane = rp.lane_id
    if not lane:
        continue

    needs_update = False
    vals = {}

    # planned_pallets: from lane, but only if currently 0 or default
    if not rp.planned_pallets or rp.planned_pallets == 6:
        vals["planned_pallets"] = lane.target_load_pallets or 8
        needs_update = True

    # estimated_one_way_cost: from lane if not already set
    if not rp.estimated_one_way_cost and lane.estimated_one_way_cost:
        vals["estimated_one_way_cost"] = lane.estimated_one_way_cost
        needs_update = True

    if needs_update:
        rp.write(vals)
        updated_plans += 1

print(f"   Rate plans updated: {updated_plans}")

# 4. Force recompute of lane names (human-readable region names)
print("\n4. Updating lane names to human-readable format...")
name_changes = 0
for lane in Lane.search([("active", "=", True)]):
    old_name = lane.name
    lane._compute_name()
    if lane.name != old_name:
        name_changes += 1
        print(f"   {old_name} --> {lane.name}")

# Force recompute of service_offering names (depends on lane.name)
print("\n5. Updating service offering names...")
for off in Offering.search([("active", "=", True)]):
    old_name = off.name
    off._compute_name()
    if off.name != old_name and name_changes > 0:
        pass  # silently update; don't spam output

# Force recompute of rate plan names (depends on offering.name)
print("6. Updating rate plan names...")
for rp in RatePlan.search([("active", "=", True)]):
    old_name = rp.name
    rp._compute_name()
    if old_name != rp.name:
        print(f"   {old_name} --> {rp.name}")

print(f"\n   Lane name changes: {name_changes}")

# 7. Assertions
print("\n7. Running assertions...")

# 7a. All active rate plans have pallet tiers
active_plans = RatePlan.search([("active", "=", True)])
plans_without_tiers = [
    rp for rp in active_plans
    if _tier_count(rp) == 0
]
if plans_without_tiers:
    print(f"   WARNING: {len(plans_without_tiers)} active plans have NO pallet tiers!")
    for rp in plans_without_tiers[:5]:
        print(f"     - {rp.name}")
else:
    print(f"   All {len(active_plans)} active plans have pallet tiers OK")

# 7b. No orphaned chilled/frozen offerings with active rate plans
orphan_count = RatePlan.search_count([
    ("active", "=", True),
    ("service_offering_id.active", "=", False),
])
if orphan_count:
    print(f"   WARNING: {orphan_count} active rate plans on inactive offerings!")
else:
    print("   No orphaned rate plans OK")

# 7c. All active rate plans have planned_pallets > 0
plans_no_planning = [
    rp for rp in active_plans if not rp.planned_pallets
]
if plans_no_planning:
    print(f"   WARNING: {len(plans_no_planning)} plans have planned_pallets = 0")
else:
    print(f"   All plans have planned_pallets set OK")

# Summary
print("\n" + "=" * 60)
print("Migration complete.")
print(f"  Active rate plans: {len(active_plans)}")
print(f"  Temperature offerings consolidated: {consolidated}")
print(f"  Lane names updated: {name_changes}")
print(f"  Rate plans updated: {updated_plans}")
print()
print("IMPORTANT: Restart Odoo and upgrade the module:")
print("  systemctl restart odoo18")
print("  sudo -u odoo18 /opt/odoo/venv-18/bin/python3 /opt/odoo/odoo18/odoo-bin -c /etc/odoo18.conf -d Prod-db -u prema_logistics_booking --stop-after-init")
print("=" * 60)
