# Not auto-loaded by Odoo (no manifest entry) -- run manually via:
# odoo-bin shell -c /etc/odoo18.conf -d Prod-db --no-http < scripts/load_phase1_v1_rates.py
# Idempotent: safe to re-run after editing BANDS/MATRIX/GLOBAL_SURCHARGES.
from odoo.exceptions import UserError

Region = env["logistics.region"]
Lane = env["logistics.lane"]
SLevel = env["logistics.service.level"]
SOffering = env["logistics.service.offering"]
RatePlan = env["logistics.rate.plan"]
Tier = env["logistics.rate.tier"]
SurchargeType = env["logistics.surcharge.type"]
PlanSurcharge = env["logistics.rate.plan.surcharge"]

# ---------- Ordered region list (index 0..9 = R1..R10) ----------
codes = [f"R{i}" for i in range(1, 11)]
region_by_code = {c: Region.search([("code", "=", c)], limit=1) for c in codes}
for c, r in region_by_code.items():
    assert r, f"missing region {c}"

# ---------- Band definitions (PremaFirm Phase 1 v1, verified against every
# worked example in the business's own spec) ----------
BANDS = {
    "L": {"r1": 150.0, "r2": 125.0, "r3plus": 100.0, "cap": 650.0},
    "A": {"r1": 175.0, "r2": 150.0, "r3plus": 110.0, "cap": 700.0},
    "B": {"r1": 175.0, "r2": 150.0, "r3plus": 125.0, "cap": 900.0},
    "C": {"r1": 200.0, "r2": 175.0, "r3plus": 150.0, "cap": 1200.0},
    "D": {"r1": 250.0, "r2": 220.0, "r3plus": 185.0, "cap": 1500.0},
    "E": {"r1": 300.0, "r2": 260.0, "r3plus": 220.0, "cap": 1800.0},
    "F": {"r1": 350.0, "r2": 300.0, "r3plus": 250.0, "cap": 2200.0},
}

# ---------- Full 10x10 matrix exactly as given, row=origin, col=destination ----------
MATRIX = [
    #        R1   R2   R3   R4   R5   R6   R7   R8   R9   R10
    ["L", "B", "A", "A", "B", "C", "C", "D", "E", "F"],   # R1
    ["B", "L", "B", "C", "C", "D", "D", "E", "F", "F"],   # R2
    ["A", "B", "L", "B", "B", "C", "D", "E", "E", "F"],   # R3
    ["A", "C", "B", "L", "B", "C", "C", "D", "E", "F"],   # R4
    ["B", "C", "B", "B", "L", "B", "C", "C", "D", "E"],   # R5
    ["C", "D", "C", "C", "B", "L", "B", "C", "C", "D"],   # R6
    ["C", "D", "D", "C", "C", "B", "L", "B", "C", "C"],   # R7
    ["D", "E", "E", "D", "C", "C", "B", "L", "A", "C"],   # R8
    ["E", "F", "E", "E", "D", "C", "C", "A", "L", "B"],   # R9
    ["F", "F", "F", "F", "E", "D", "C", "C", "B", "L"],   # R10
]

# ---------- Standard automatic-booking capacity (Phase 1 v1: 12 pallets) ----------
STANDARD_PALLET_CAP = 12

# ---------- Global (network-wide) accessorial/surcharge catalog ----------
GLOBAL_SURCHARGES = [
    ("LIFTGATE_PICKUP", "Liftgate Pickup", "flat", 50.0),
    ("LIFTGATE_DELIVERY", "Liftgate Delivery", "flat", 50.0),
    ("APPOINTMENT", "Appointment", "flat", 35.0),
    ("RESIDENTIAL", "Residential / Limited Access", "flat", 75.0),
    ("TEMP_CHILLED", "Chilled Temperature Premium", "percent", 15.0),
    ("TEMP_FROZEN", "Frozen Temperature Premium", "percent", 20.0),
    ("SAME_DAY_EXPRESS", "Same-Day Express Premium", "percent", 25.0),
    ("WEEKEND", "Weekend Service Premium", "percent", 20.0),
]
for code, name, calc_type, amount in GLOBAL_SURCHARGES:
    st = SurchargeType.search([("code", "=", code)], limit=1)
    if st:
        st.write({"name": name, "calc_type": calc_type, "default_amount": amount, "is_global": True})
    else:
        SurchargeType.create({
            "code": code, "name": name, "calc_type": calc_type,
            "default_amount": amount, "is_global": True,
        })
print("SURCHARGES_LOADED", len(GLOBAL_SURCHARGES))

# ---------- Real "Next Day" service level (supersedes the old TEST ONLY one) ----------
slevel = SLevel.search([("code", "=", "NEXT_DAY")], limit=1)
if not slevel:
    slevel = SLevel.create({
        "code": "NEXT_DAY", "name": "Next Day", "reefer_food_eligible": True, "max_transit_hours": 24,
    })

old_test_slevel = SLevel.search([("code", "=", "TESTONLY_NEXTDAY")], limit=1)
if old_test_slevel:
    SOffering.search([("service_level_id", "=", old_test_slevel.id)]).write({"service_level_id": slevel.id})
    old_test_slevel.unlink()
    print("REPOINTED_OLD_TEST_SERVICE_LEVEL")

old_reefer_st = SurchargeType.search([("code", "=", "TESTONLY_REEFER")], limit=1)


def set_tiers(rate_plan, band_key):
    band = BANDS[band_key]
    rate_plan.tier_ids.filtered(lambda t: t.tier_type == "pallet").unlink()
    r1, r3plus, cap = band["r1"], band["r3plus"], band["cap"]
    # Derive band-4 per-pallet rate from cap at 8 pallets
    b4_rate = max(round(min(r3plus * 8, cap) / 8), 50.0) if cap else r3plus
    bands = [
        (1, 1, r1, "per_unit", 0),
        (2, 4, r3plus, "per_unit", 0),
        (5, 6, r3plus, "per_unit", 0),
        (7, 10, b4_rate, "per_unit", cap),
        (11, 13, cap, "flat", 0),
    ]
    for min_q, max_q, rate, calc_method, cap_amt in bands:
        Tier.create({
            "rate_plan_id": rate_plan.id, "tier_type": "pallet",
            "min_qty": min_q, "max_qty": max_q,
            "calc_method": calc_method, "rate": rate,
            "cap_amount": cap_amt,
        })


created_lanes = created_offerings = created_plans = updated_plans = 0

for i, origin_code in enumerate(codes):
    for j, dest_code in enumerate(codes):
        band_key = MATRIX[i][j]
        origin = region_by_code[origin_code]
        dest = region_by_code[dest_code]

        lane = Lane.search([
            ("origin_region_id", "=", origin.id), ("destination_region_id", "=", dest.id),
        ], limit=1)
        if not lane:
            lane = Lane.create({
                "origin_region_id": origin.id, "destination_region_id": dest.id,
                "max_pallets": STANDARD_PALLET_CAP, "ltl_capable": True, "ftl_capable": True,
            })
            created_lanes += 1
        elif not lane.max_pallets:
            lane.write({"max_pallets": STANDARD_PALLET_CAP})

        offering = SOffering.search([
            ("lane_id", "=", lane.id), ("service_level_id", "=", slevel.id),
            ("temperature_mode", "=", "dry"),
        ], limit=1)
        if not offering:
            offering = SOffering.create({
                "lane_id": lane.id, "service_level_id": slevel.id,
                "temperature_mode": "dry", "shipment_type": "both",
            })
            created_offerings += 1

        plan = RatePlan.search([("service_offering_id", "=", offering.id)], limit=1)
        if not plan:
            plan = RatePlan.create({"service_offering_id": offering.id, "base_rate": 0.0, "minimum_charge": 0.0})
            created_plans += 1
        else:
            # base_rate=0 and minimum_charge=0: the tier structure itself
            # (1-skid rate through the FTL cap) is the floor -- Phase1-v1
            # has no separate minimum-charge concept layered on top.
            plan.write({"base_rate": 0.0, "minimum_charge": 0.0})
            updated_plans += 1
        set_tiers(plan, band_key)
        # Remove any stale per-plan surcharge overrides from the earlier
        # TEST ONLY fixture -- global surcharges now cover these uniformly.
        plan.surcharge_ids.unlink()
        plan.fsa_adjustment_ids.unlink()  # not yet assigned -- see CLAUDE.md blockers

# ---------- R1<->R7 chilled offering: keep it (only pre-existing reefer
# offering), re-point to the real service level, correct its rate plan the
# same way, remove the old flat TESTONLY_REEFER assignment (global
# TEMP_CHILLED % now covers it automatically). ----------
r1, r7 = region_by_code["R1"], region_by_code["R7"]
lane_r1_r7 = Lane.search([("origin_region_id", "=", r1.id), ("destination_region_id", "=", r7.id)], limit=1)
chilled_offering = SOffering.search([
    ("lane_id", "=", lane_r1_r7.id), ("temperature_mode", "=", "chilled"),
], limit=1)
if chilled_offering:
    chilled_offering.write({"service_level_id": slevel.id, "shipment_type": "both"})
    chilled_plan = RatePlan.search([("service_offering_id", "=", chilled_offering.id)], limit=1)
    if chilled_plan:
        chilled_plan.write({"base_rate": 0.0, "minimum_charge": 0.0})
        set_tiers(chilled_plan, "C")
        chilled_plan.surcharge_ids.unlink()
        chilled_plan.fsa_adjustment_ids.unlink()
    print("R1_R7_CHILLED_UPDATED")

# Frozen offering didn't exist before -- add it as the natural complement to
# chilled on this one fully-configured example lane (dry/chilled/frozen all
# working end to end for R1<->R7 specifically).
frozen_offering = SOffering.search([
    ("lane_id", "=", lane_r1_r7.id), ("temperature_mode", "=", "frozen"),
], limit=1)
if not frozen_offering:
    frozen_offering = SOffering.create({
        "lane_id": lane_r1_r7.id, "service_level_id": slevel.id,
        "temperature_mode": "frozen", "shipment_type": "both",
    })
    frozen_plan = RatePlan.create({"service_offering_id": frozen_offering.id, "base_rate": 0.0, "minimum_charge": 0.0})
    set_tiers(frozen_plan, "C")
    # Reuse the same schedule as the chilled offering (same lane, same
    # transit-time reality) rather than duplicating schedule rows.
    chilled_schedule = env["logistics.lane.schedule"].search(
        [("service_offering_id", "=", chilled_offering.id)], limit=1)
    if chilled_schedule:
        env["logistics.lane.schedule"].create({
            "service_offering_id": frozen_offering.id,
            "pickup_monday": chilled_schedule.pickup_monday, "pickup_tuesday": chilled_schedule.pickup_tuesday,
            "pickup_wednesday": chilled_schedule.pickup_wednesday, "pickup_thursday": chilled_schedule.pickup_thursday,
            "pickup_friday": chilled_schedule.pickup_friday, "pickup_saturday": chilled_schedule.pickup_saturday,
            "pickup_sunday": chilled_schedule.pickup_sunday, "cutoff_time": chilled_schedule.cutoff_time,
            "delivery_offset_type": chilled_schedule.delivery_offset_type,
            "holiday_calendar_ids": [(6, 0, chilled_schedule.holiday_calendar_ids.ids)],
        })
    print("R1_R7_FROZEN_CREATED")

if old_reefer_st:
    old_reefer_st.unlink()

env.cr.commit()
print("DONE", "lanes_created", created_lanes, "offerings_created", created_offerings,
      "plans_created", created_plans, "plans_updated", updated_plans)
