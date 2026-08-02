"""V4 LTL Hub Pricing — Anchor Rate Plans from Mississauga Hub.

Creates or updates Rate Plans for all Hub-to-destination lanes.
Idempotent: uses (origin_region, destination_region) as business key.

Run: python3 odoo-bin shell -c /etc/odoo18.conf -d Prod-db < this_file
"""
ANCHORS = {
    # (origin_code, dest_code): (target_revenue, target_load_qty, direction)
    ("R1", "R8"):  (1600, 7, "east"),    # Mississauga Hub → Montreal
    ("R1", "R7"):  (1400, 7, "east"),    # Mississauga Hub → Ottawa
    ("R1", "R10"): (2200, 7, "east"),    # Mississauga Hub → Quebec City
    ("R1", "R4"):  (1400, 7, "north"),   # Mississauga Hub → Sudbury
    ("R1", "R5"):  (1100, 7, "west"),    # Mississauga Hub → Windsor
    ("R1", "R9"):  (600,  7, "west"),    # Mississauga Hub → London
    ("R1", "R11"): (870,  7, "west"),    # Mississauga Hub → Sarnia
    ("R1", "R12"): (600,  7, "north"),   # Mississauga Hub → Owen Sound
    ("R1", "R13"): (1000, 7, "north"),   # Mississauga Hub → Tobermory
    ("R1", "R14"): (1300, 7, "east"),    # Mississauga Hub → Cornwall
    ("R1", "R15"): (2100, 7, "east"),    # Mississauga Hub → Sherbrooke
    ("R1", "R16"): (2800, 7, "north"),   # Mississauga Hub → Saguenay
    ("R1", "R17"): (420,  7, "east"),    # Mississauga Hub → Peterborough
}

COMMON_DEFAULTS = {
    "target_load_quantity": 7,
    "included_weight_per_pallet": 500.0,
    "safe_weight_capacity": 11000.0,
    "pricing_method": "simple",
    "service_type": "linehaul",
    "suggested_rate_per_km": 2.80,
}


def load_v4_hub_anchors(env):
    Region = env["logistics.region"].sudo()
    Lane = env["logistics.lane"].sudo()
    RatePlan = env["logistics.rate.plan"].sudo()
    ServiceOffering = env["logistics.service.offering"].sudo()
    ServiceLevel = env["logistics.service.level"].sudo()

    # Get or create default service level and offering
    level = ServiceLevel.search([], limit=1)
    if not level:
        level = ServiceLevel.create({"name": "Scheduled LTL", "code": "SCHED_LTL"})

    created = 0
    updated = 0

    for (orig_code, dest_code), (target, tlq, direction) in ANCHORS.items():
        orig = Region.search([("code", "=", orig_code)], limit=1)
        dest = Region.search([("code", "=", dest_code)], limit=1)
        if not orig or not dest:
            print(f"  SKIP: Region {orig_code} or {dest_code} not found")
            continue

        # Find or create lane
        lane = Lane.search([
            ("origin_region_id", "=", orig.id),
            ("destination_region_id", "=", dest.id),
        ], limit=1)
        if not lane:
            # Use SQL to avoid unique constraint violation on concurrent/prior seeds
            env.cr.execute("""
                INSERT INTO logistics_lane (origin_region_id, destination_region_id,
                    revenue_target, target_load_pallets, ltl_capable, create_uid, write_uid)
                VALUES (%s, %s, %s, %s, true, 1, 1)
                ON CONFLICT (origin_region_id, destination_region_id) DO NOTHING
                RETURNING id
            """, [orig.id, dest.id, target, tlq])
            result = env.cr.fetchone()
            if result:
                lane = Lane.browse(result[0])
                print(f"  Created lane: {orig.name} → {dest.name}")
            else:
                lane = Lane.search([
                    ("origin_region_id", "=", orig.id),
                    ("destination_region_id", "=", dest.id),
                ], limit=1)

        # Find or create offering
        offering = ServiceOffering.search([
            ("lane_id", "=", lane.id),
            ("service_level_id", "=", level.id),
        ], limit=1)
        if not offering:
            offering = ServiceOffering.create({
                "lane_id": lane.id,
                "service_level_id": level.id,
                "shipment_type": "ltl",
                "temperature_mode": "dry",
            })

        # Find or update rate plan
        plan = RatePlan.search([
            ("service_offering_id", "=", offering.id),
            ("active", "=", True),
        ], limit=1)

        vals = {
            "revenue_target": target,
            "target_load_quantity": tlq,
            "direction": direction,
            **COMMON_DEFAULTS,
        }

        if plan:
            plan.write(vals)
            updated += 1
        else:
            vals["service_offering_id"] = offering.id
            RatePlan.create(vals)
            created += 1

    env.cr.commit()
    return {"created": created, "updated": updated}


if __name__ == "__main__":
    result = load_v4_hub_anchors(env)  # noqa: F821
    print(f"Hub anchors: {result['created']} created, {result['updated']} updated")
