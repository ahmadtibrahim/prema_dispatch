"""Idempotent production config loader: lanes, offerings, schedules, rate plans, surcharges.
No FSA rows created — Seattle CSV import is a separate manual step.
Also creates all 3 temperature-mode offerings per lane (dry, chilled, frozen) with
identical base pricing + global TEMP_CHILLED/TEMP_FROZEN surcharges.

Update 2026-07-27: 5-band pallet tiers replace 8-tier + MONTREAL_TIERS.
All lanes use generate_tiers() with the same formula.
"""
from datetime import date

# ── Bootstrap references ─────────────────────────────────────────────
Lane = env["logistics.lane"]
Region = env["logistics.region"]
SLevel = env["logistics.service.level"]
SOffering = env["logistics.service.offering"]
RatePlan = env["logistics.rate.plan"]
Tier = env["logistics.rate.tier"]
Schedule = env["logistics.lane.schedule"]
SurchargeType = env["logistics.surcharge.type"]

slevel_next = SLevel.search([("code","=","NEXT_DAY")],limit=1).ensure_one()

created_lanes = offerings_created = plans_created = 0
tiers_created = 0
schedules_created = 0

# 5-band pallet pricing tiers (all lanes use the same formula)
def generate_tiers(target):
    """Generate 5 banded pricing tiers from revenue target (planning=8)."""
    base = target / 8
    ftl_flat = max(round(target * 1.07, 0), 500.0)
    return [
        (1,  1,   max(round(base * 1.73, 0), 50.0), "per_unit", 0),
        (2,  4,   max(round(base * 1.47, 0), 50.0), "per_unit", 0),
        (5,  6,   max(round(base * 1.20, 0), 50.0), "per_unit", 0),
        (7,  10,  max(round(base * 1.08, 0), 50.0), "per_unit", ftl_flat),
        (11, 13,  ftl_flat,                            "flat",     0),
    ]

active_lanes = Lane.search([("active","=",True)])
for lane in active_lanes:
    target = lane.revenue_target or 700
    tiers_config = generate_tiers(target)

    for temp_mode in ["dry","chilled","frozen"]:
        if not lane.active: continue

        offering = SOffering.search([
            ("lane_id","=",lane.id),("temperature_mode","=",temp_mode),
        ],limit=1)
        if not offering:
            offering = SOffering.create({
                "lane_id":lane.id,"service_level_id":slevel_next.id,
                "temperature_mode":temp_mode,"shipment_type":"both","active":True,
            })
            offerings_created += 1

        plan = RatePlan.search([("service_offering_id","=",offering.id)],limit=1)
        if not plan:
            plan = RatePlan.create({
                "service_offering_id":offering.id,"base_rate":0.0,"minimum_charge":0.0,
                "revenue_target":float(target),"active":True,
            })
            plans_created += 1
        else:
            plan.write({"revenue_target":float(target),"active":True})

        existing_tiers = Tier.search_count([("rate_plan_id","=",plan.id)])
        if existing_tiers == 0:
            for min_q, max_q, rate, calc_method, cap in tiers_config:
                Tier.create({
                    "rate_plan_id": plan.id, "tier_type": "pallet",
                    "min_qty": min_q, "max_qty": max_q,
                    "calc_method": calc_method,
                    "rate": float(rate), "cap_amount": float(cap),
                })
                tiers_created += 1

        sched = Schedule.search([("service_offering_id","=",offering.id)],limit=1)
        if not sched:
            Schedule.create({
                "service_offering_id":offering.id,
                "mon":True,"tue":True,"wed":True,"thu":True,"fri":True,
                "pickup_cutoff_time":16.0,"delivery_offset_days":1,
            })
            schedules_created += 1

print(f"Lanes: {created_lanes} created")
print(f"Offerings: {offerings_created} created")
print(f"Rate Plans: {plans_created} created")
print(f"Tiers: {tiers_created} created ({tiers_created//5} plans x 5 bands)")
print(f"Schedules: {schedules_created} created")
print("=== PRODUCTION CONFIG LOADED ===")
