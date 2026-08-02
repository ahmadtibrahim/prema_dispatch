# Idempotent full loader — Phase 2 complete region/lane/rate/schedule setup.
# Run: cd /opt/odoo/odoo18 && sudo -u odoo18 python3 odoo-bin shell -c /etc/odoo18.conf -d Prod-db --no-http < /opt/odoo/custom-addons/prema_logistics_booking/scripts/load_phase2_full.py
import csv
import io
import os

Region = env["logistics.region"]
Fsa = env["logistics.fsa"]
Lane = env["logistics.lane"]
SLevel = env["logistics.service.level"]
SOffering = env["logistics.service.offering"]
RatePlan = env["logistics.rate.plan"]
Tier = env["logistics.rate.tier"]
SurchargeType = env["logistics.surcharge.type"]
Schedule = env["logistics.lane.schedule"]
HolidayCal = env["logistics.holiday.calendar"]
HolidayLine = env["logistics.holiday.calendar.line"]

def _generate_tiers_from_target(target, planning_pallets=8):
    """Generate 5 banded pricing tiers from revenue target and planning pallets."""
    base = target / max(planning_pallets, 1)
    ftl_flat = max(round(target * 1.07, 0), 500.0)
    tier_config = [
        (1,  1,   max(round(base * 1.73, 0), 50.0), "per_unit", 0),
        (2,  4,   max(round(base * 1.47, 0), 50.0), "per_unit", 0),
        (5,  6,   max(round(base * 1.20, 0), 50.0), "per_unit", 0),
        (7,  10,  max(round(base * 1.08, 0), 50.0), "per_unit", ftl_flat),
        (11, 13,  ftl_flat,                            "flat",     0),
    ]
    return tier_config

print("=== PHASE 2 FULL LOADER ===")

# ── STEP 1: 15-Region Master ──────────────────────────────────────────
REGIONS_15 = [
    ("R1",  "GTA Central",              "Mississauga",    0.00, 1),
    ("R2",  "Southwest West",           "Windsor",        0.00, 1),
    ("R3",  "Southwest Central",        "London",         0.00, 1),
    ("R4",  "Golden Horseshoe South",   "Hamilton",       0.00, 2),
    ("R5",  "Central North",            "Barrie",         0.05, 2),
    ("R6",  "Grey-Bruce",               "Owen Sound",     0.10, 2),
    ("R7",  "Northeast Ontario",        "Sudbury",        0.15, 2),
    ("R8",  "East-Central 401",         "Belleville",     0.00, 1),
    ("R9",  "Kawartha",                 "Peterborough",   0.05, 2),
    ("R10", "Eastern Ontario West",     "Kingston",       0.00, 1),
    ("R11", "Eastern Ontario East",     "Cornwall",       0.05, 1),
    ("R12", "Ottawa Valley",            "Ottawa",         0.05, 2),
    ("R13", "Greater Montreal",         "Montreal",       0.00, 1),
    ("R14", "Central Quebec",           "Drummondville",  0.05, 1),
    ("R15", "Quebec City Region",       "Quebec City",    0.05, 1),
]

created_regions = updated_regions = 0
for code, name, hub, zone_adj, phase in REGIONS_15:
    existing = Region.search([("code", "=", code)], limit=1)
    vals = {"name": name, "hub_name": hub, "phase": phase, "active": True}
    if existing:
        existing.write(vals)
        updated_regions += 1
    else:
        Region.create(dict(code=code, **vals))
        created_regions += 1
print(f"REGIONS: {created_regions} created, {updated_regions} updated")

# Delete any regions beyond R15 (from old 10-region system that don't match)
old_extra = Region.search([("code", "not in", [r[0] for r in REGIONS_15])])
if old_extra:
    print(f"ARCHIVING {len(old_extra)} old regions: {old_extra.mapped('code')}")
    old_extra.write({"active": False})

# ── STEP 2: FSA Data from Municipality CSV ─────────────────────────────
csv_path = "/tmp/municipality_region.csv"
region_by_code = {c: Region.search([("code", "=", c)], limit=1) for c in [r[0] for r in REGIONS_15]}

fsa_created = fsa_updated = 0
if os.path.exists(csv_path):
    # Build FSA→region mapping from CSV (CSDUID→Region→Province)
    fsa_map = {}
    with open(csv_path, "r", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            csdname = (row.get("CSDNAME") or "").strip()
            province = (row.get("Province") or "").strip()
            region_code = (row.get("Region") or "").strip()
            if not csdname or not region_code:
                continue
            # Derive FSA from city — first 3 chars of postal areas are FSAs
            # The CSV maps municipalities to regions; we derive FSA coverage
            # by creating FSA records for major cities in each region
            fsa_key = csdname.upper()[:3] if len(csdname) >= 3 else csdname.upper()
            if fsa_key not in fsa_map:
                fsa_map[fsa_key] = {"city": csdname, "province": province, "region": region_code}

    # For production: load known FSA-to-city mappings for Phase 1 corridors
    KNOWN_FSAS = [
        # GTA Central (R1)
        ("L5M", "Mississauga", "ON", "R1"), ("L5N", "Mississauga", "ON", "R1"),
        ("L4T", "Mississauga", "ON", "R1"), ("L4W", "Mississauga", "ON", "R1"),
        ("L4V", "Mississauga", "ON", "R1"), ("L4Z", "Mississauga", "ON", "R1"),
        ("M5V", "Toronto", "ON", "R1"), ("M5W", "Toronto", "ON", "R1"),
        ("M6K", "Toronto", "ON", "R1"), ("L6Y", "Brampton", "ON", "R1"),
        ("L6P", "Brampton", "ON", "R1"), ("L6R", "Brampton", "ON", "R1"),
        ("L3R", "Markham", "ON", "R1"), ("L4K", "Vaughan", "ON", "R1"),
        ("L4L", "Vaughan", "ON", "R1"), ("L5B", "Mississauga", "ON", "R1"),
        # Southwest West (R2)
        ("N8W", "Windsor", "ON", "R2"), ("N8X", "Windsor", "ON", "R2"),
        ("N8Y", "Windsor", "ON", "R2"), ("N9A", "Windsor", "ON", "R2"),
        ("N7M", "Chatham", "ON", "R2"), ("N7L", "Chatham", "ON", "R2"),
        ("N8A", "Wallaceburg", "ON", "R2"),
        # Southwest Central (R3)
        ("N6A", "London", "ON", "R3"), ("N6B", "London", "ON", "R3"),
        ("N6E", "London", "ON", "R3"), ("N5V", "London", "ON", "R3"),
        ("N2H", "Kitchener", "ON", "R3"), ("N2G", "Kitchener", "ON", "R3"),
        ("N1G", "Guelph", "ON", "R3"), ("N1H", "Guelph", "ON", "R3"),
        # Golden Horseshoe South (R4)
        ("L8E", "Hamilton", "ON", "R4"), ("L8H", "Hamilton", "ON", "R4"),
        ("L8W", "Hamilton", "ON", "R4"), ("L8L", "Hamilton", "ON", "R4"),
        ("L7M", "Burlington", "ON", "R4"), ("L7N", "Burlington", "ON", "R4"),
        ("L2E", "Niagara Falls", "ON", "R4"), ("L2G", "Niagara Falls", "ON", "R4"),
        ("L2R", "St. Catharines", "ON", "R4"), ("L2N", "St. Catharines", "ON", "R4"),
        # Central North (R5)
        ("L4M", "Barrie", "ON", "R5"), ("L4N", "Barrie", "ON", "R5"),
        ("L3V", "Orillia", "ON", "R5"), ("L9Y", "Collingwood", "ON", "R5"),
        # Grey-Bruce (R6)
        ("N4K", "Owen Sound", "ON", "R6"), ("N4L", "Meaford", "ON", "R6"),
        # Northeast Ontario (R7)
        ("P3A", "Sudbury", "ON", "R7"), ("P3C", "Sudbury", "ON", "R7"),
        ("P3E", "Sudbury", "ON", "R7"),
        # East-Central 401 (R8)
        ("L1H", "Oshawa", "ON", "R8"), ("L1G", "Oshawa", "ON", "R8"),
        ("K9A", "Cobourg", "ON", "R8"), ("K8N", "Belleville", "ON", "R8"),
        ("K8P", "Belleville", "ON", "R8"),
        # Kawartha (R9)
        ("K9H", "Peterborough", "ON", "R9"), ("K9J", "Peterborough", "ON", "R9"),
        ("K9V", "Lindsay", "ON", "R9"),
        # Eastern Ontario West (R10)
        ("K7K", "Kingston", "ON", "R10"), ("K7L", "Kingston", "ON", "R10"),
        ("K7M", "Kingston", "ON", "R10"), ("K6V", "Brockville", "ON", "R10"),
        # Eastern Ontario East (R11)
        ("K6H", "Cornwall", "ON", "R11"), ("K6J", "Cornwall", "ON", "R11"),
        # Ottawa Valley (R12)
        ("K1G", "Ottawa", "ON", "R12"), ("K1A", "Ottawa", "ON", "R12"),
        ("K2B", "Ottawa", "ON", "R12"), ("K1N", "Ottawa", "ON", "R12"),
        ("K1V", "Ottawa", "ON", "R12"), ("J8X", "Gatineau", "QC", "R12"),
        ("J8Y", "Gatineau", "QC", "R12"), ("J8Z", "Gatineau", "QC", "R12"),
        # Greater Montreal (R13)
        ("H1A", "Montreal", "QC", "R13"), ("H2X", "Montreal", "QC", "R13"),
        ("H3B", "Montreal", "QC", "R13"), ("H4B", "Montreal", "QC", "R13"),
        ("H7V", "Laval", "QC", "R13"), ("H7W", "Laval", "QC", "R13"),
        ("J4B", "Longueuil", "QC", "R13"), ("J4G", "Longueuil", "QC", "R13"),
        # Central Quebec (R14)
        ("G8Z", "Trois-Rivieres", "QC", "R14"), ("G9A", "Trois-Rivieres", "QC", "R14"),
        ("J2B", "Drummondville", "QC", "R14"), ("J2C", "Drummondville", "QC", "R14"),
        # Quebec City Region (R15)
        ("G1A", "Quebec City", "QC", "R15"), ("G1K", "Quebec City", "QC", "R15"),
        ("G1V", "Quebec City", "QC", "R15"), ("G6V", "Levis", "QC", "R15"),
        ("G6W", "Levis", "QC", "R15"),
    ]

    for fsa_code, city, province, region_code in KNOWN_FSAS:
        region = region_by_code.get(region_code)
        if not region:
            continue
        existing_fsa = Fsa.search([("fsa", "=", fsa_code)], limit=1)
        vals = {
            "province": province,
            "display_city": city,
            "region_id": region.id,
            "pickup_supported": True,
            "delivery_supported": True,
            "active": True,
        }
        if existing_fsa:
            existing_fsa.write(vals)
            fsa_updated += 1
        else:
            Fsa.create(dict(fsa=fsa_code, **vals))
            fsa_created += 1
    print(f"FSA: {fsa_created} created, {fsa_updated} updated")
else:
    print("WARNING: municipality CSV not found at /tmp/municipality_region.csv — skipping FSA load")

# ── STEP 3: Revenue Target Matrix (from spreadsheet) ──────────────────
# FINAL MIN TARGET / WAY from PremaFirm_Phase1_Regional_Target_Revenue.xlsx
TARGET_MATRIX = {
    ("R1","R2"):1000,("R1","R3"):700,("R1","R4"):350,("R1","R5"):550,("R1","R6"):800,
    ("R1","R7"):1300,("R1","R8"):700,("R1","R9"):600,("R1","R10"):850,("R1","R11"):1200,
    ("R1","R12"):1200,("R1","R13"):1500,("R1","R14"):1850,("R1","R15"):2100,
    ("R2","R1"):1000,("R2","R3"):700,("R2","R4"):950,("R2","R5"):1200,("R2","R6"):1150,
    ("R2","R7"):1700,("R2","R8"):1500,("R2","R9"):1450,("R2","R10"):1650,("R2","R11"):2150,
    ("R2","R12"):2050,("R2","R13"):2300,("R2","R14"):2700,("R2","R15"):3000,
    ("R3","R1"):700,("R3","R2"):700,("R3","R4"):550,("R3","R5"):800,("R3","R6"):800,
    ("R3","R7"):1400,("R3","R8"):1100,("R3","R9"):1000,("R3","R10"):1250,("R3","R11"):1750,
    ("R3","R12"):1600,("R3","R13"):1900,("R3","R14"):2300,("R3","R15"):2600,
    ("R4","R1"):350,("R4","R2"):950,("R4","R3"):550,("R4","R5"):600,("R4","R6"):750,
    ("R4","R7"):1350,("R4","R8"):850,("R4","R9"):750,("R4","R10"):1000,("R4","R11"):1450,
    ("R4","R12"):1350,("R4","R13"):1650,("R4","R14"):2000,("R4","R15"):2300,
    ("R5","R1"):550,("R5","R2"):1200,("R5","R3"):800,("R5","R4"):600,("R5","R6"):600,
    ("R5","R7"):1050,("R5","R8"):800,("R5","R9"):600,("R5","R10"):950,("R5","R11"):1300,
    ("R5","R12"):1150,("R5","R13"):1550,("R5","R14"):1850,("R5","R15"):2100,
    ("R6","R1"):800,("R6","R2"):1150,("R6","R3"):800,("R6","R4"):750,("R6","R5"):600,
    ("R6","R7"):950,("R6","R8"):1100,("R6","R9"):900,("R6","R10"):1250,("R6","R11"):1650,
    ("R6","R12"):1450,("R6","R13"):1900,("R6","R14"):2150,("R6","R15"):2450,
    ("R7","R1"):1300,("R7","R2"):1700,("R7","R3"):1400,("R7","R4"):1350,("R7","R5"):1050,
    ("R7","R6"):950,("R7","R8"):1400,("R7","R9"):1250,("R7","R10"):1550,("R7","R11"):1750,
    ("R7","R12"):1500,("R7","R13"):1950,("R7","R14"):2150,("R7","R15"):2400,
    ("R8","R1"):700,("R8","R2"):1500,("R8","R3"):1100,("R8","R4"):850,("R8","R5"):800,
    ("R8","R6"):1100,("R8","R7"):1400,("R8","R9"):500,("R8","R10"):450,("R8","R11"):900,
    ("R8","R12"):800,("R8","R13"):1100,("R8","R14"):1450,("R8","R15"):1750,
    ("R9","R1"):600,("R9","R2"):1450,("R9","R3"):1000,("R9","R4"):750,("R9","R5"):600,
    ("R9","R6"):900,("R9","R7"):1250,("R9","R8"):500,("R9","R10"):700,("R9","R11"):1050,
    ("R9","R12"):900,("R9","R13"):1300,("R9","R14"):1600,("R9","R15"):1900,
    ("R10","R1"):850,("R10","R2"):1650,("R10","R3"):1250,("R10","R4"):1000,("R10","R5"):950,
    ("R10","R6"):1250,("R10","R7"):1550,("R10","R8"):450,("R10","R9"):700,("R10","R11"):750,
    ("R10","R12"):700,("R10","R13"):950,("R10","R14"):1300,("R10","R15"):1600,
    ("R11","R1"):1200,("R11","R2"):2150,("R11","R3"):1750,("R11","R4"):1450,("R11","R5"):1300,
    ("R11","R6"):1650,("R11","R7"):1750,("R11","R8"):900,("R11","R9"):1050,("R11","R10"):750,
    ("R11","R12"):550,("R11","R13"):600,("R11","R14"):850,("R11","R15"):1150,
    ("R12","R1"):1200,("R12","R2"):2050,("R12","R3"):1600,("R12","R4"):1350,("R12","R5"):1150,
    ("R12","R6"):1450,("R12","R7"):1500,("R12","R8"):800,("R12","R9"):900,("R12","R10"):700,
    ("R12","R11"):550,("R12","R13"):750,("R12","R14"):1000,("R12","R15"):1250,
    ("R13","R1"):1500,("R13","R2"):2300,("R13","R3"):1900,("R13","R4"):1650,("R13","R5"):1550,
    ("R13","R6"):1900,("R13","R7"):1950,("R13","R8"):1100,("R13","R9"):1300,("R13","R10"):950,
    ("R13","R11"):600,("R13","R12"):750,("R13","R14"):600,("R13","R15"):900,
    ("R14","R1"):1850,("R14","R2"):2700,("R14","R3"):2300,("R14","R4"):2000,("R14","R5"):1850,
    ("R14","R6"):2150,("R14","R7"):2150,("R14","R8"):1450,("R14","R9"):1600,("R14","R10"):1300,
    ("R14","R11"):850,("R14","R12"):1000,("R14","R13"):600,("R14","R15"):600,
    ("R15","R1"):2100,("R15","R2"):3000,("R15","R3"):2600,("R15","R4"):2300,("R15","R5"):2100,
    ("R15","R6"):2450,("R15","R7"):2400,("R15","R8"):1750,("R15","R9"):1900,("R15","R10"):1600,
    ("R15","R11"):1150,("R15","R12"):1250,("R15","R13"):900,("R15","R14"):600,
}

# Phase 1 schedule (from spreadsheet "Phase 1 Sellable Lines")
PHASE1_SELLABLE = {
    ("R2","R1"): ("Monday", "Monday"),
    ("R2","R13"): ("Monday", "Tuesday"),
    ("R3","R1"): ("Monday", "Monday"),
    ("R3","R13"): ("Monday", "Tuesday"),
    ("R1","R8"): ("Tuesday", "Tuesday"),
    ("R1","R10"): ("Tuesday", "Tuesday"),
    ("R1","R11"): ("Tuesday", "Tuesday"),
    ("R1","R13"): ("Tuesday", "Tuesday"),
    ("R1","R14"): ("Tuesday", "Tue/Wed"),
    ("R1","R15"): ("Tuesday", "Tue/Wed"),
    ("R13","R1"): ("Wednesday", "Wed/Thu"),
    ("R13","R2"): ("Wednesday", "Thursday"),
    ("R15","R1"): ("Wednesday", "Wed/Thu"),
    ("R1","R2"): ("Thursday", "Thursday"),
    ("R1","R3"): ("Thursday", "Thursday"),
    ("R1","R12"): ("Friday", "Friday"),
}

# Weekday mapping
WEEKDAY_MAP = {0: "pickup_monday", 1: "pickup_tuesday", 2: "pickup_wednesday",
               3: "pickup_thursday", 4: "pickup_friday", 5: "pickup_saturday", 6: "pickup_sunday"}
DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

# ── STEP 4: Create Lanes, Offerings, Rate Plans ───────────────────────
slevel_next_day = SLevel.search([("code", "=", "NEXT_DAY")], limit=1)
if not slevel_next_day:
    slevel_next_day = SLevel.create({"code": "NEXT_DAY", "name": "Next Day", "reefer_food_eligible": True, "max_transit_hours": 24})
    print("Created NEXT_DAY service level")

slevel_two_day = SLevel.search([("code", "=", "TWO_DAY")], limit=1)
if not slevel_two_day:
    slevel_two_day = SLevel.create({"code": "TWO_DAY", "name": "Two Day", "reefer_food_eligible": True, "max_transit_hours": 48})

slevel_same_day = SLevel.search([("code", "=", "SAME_DAY")], limit=1)
if not slevel_same_day:
    slevel_same_day = SLevel.create({"code": "SAME_DAY", "name": "Same Day", "reefer_food_eligible": False, "max_transit_hours": 12})

lanes_created = lanes_updated = offerings_created = plans_created = plans_updated = 0
schedules_created = 0

for (origin_code, dest_code), target in sorted(TARGET_MATRIX.items()):
    origin = region_by_code.get(origin_code)
    dest = region_by_code.get(dest_code)
    if not origin or not dest:
        continue

    # Determine service level based on sellable schedule
    sellable = PHASE1_SELLABLE.get((origin_code, dest_code))
    service_level = slevel_next_day
    delivery_offset = "next_day"
    if sellable:
        pickup_day_name, delivery_day_name = sellable
        if "Wed" in delivery_day_name or "Tue/Wed" in delivery_day_name:
            delivery_offset = "next_day"  # could be next_business_day
        if pickup_day_name == "Friday":
            delivery_offset = "next_business_day"

    # Create/update lane
    lane = Lane.search([("origin_region_id", "=", origin.id), ("destination_region_id", "=", dest.id)], limit=1)
    lane_vals = {
        "origin_region_id": origin.id,
        "destination_region_id": dest.id,
        "active": sellable is not None,  # active only if in Phase 1 sellable
        "ltl_capable": True,
        "ftl_capable": True,
        "max_pallets": 12,
        "revenue_target": float(target),
    }
    if lane:
        lane.write(lane_vals)
        lanes_updated += 1
    else:
        lane = Lane.create(lane_vals)
        lanes_created += 1

    # Create service offering (dry, Next Day)
    offering = SOffering.search([
        ("lane_id", "=", lane.id), ("service_level_id", "=", service_level.id),
        ("temperature_mode", "=", "dry"),
    ], limit=1)
    if not offering:
        offering = SOffering.create({
            "lane_id": lane.id, "service_level_id": service_level.id,
            "temperature_mode": "dry", "shipment_type": "both",
        })
        offerings_created += 1

    # Create/update rate plan
    plan = RatePlan.search([("service_offering_id", "=", offering.id)], limit=1)
    plan_vals = {
        "service_offering_id": offering.id,
        "base_rate": 0.0,
        "minimum_charge": 0.0,
        "revenue_target": float(target),
        "active": lane.active,
    }
    if plan:
        plan.write(plan_vals)
        plans_updated += 1
    else:
        plan = RatePlan.create(plan_vals)
        plans_created += 1

    # Auto-generate tiers from revenue target (7-pallet target load)
    plan.tier_ids.filtered(lambda t: t.tier_type == "pallet").unlink()
    auto_tiers = _generate_tiers_from_target(float(target), 8)
    for min_q, max_q, rate, calc_method, cap in auto_tiers:
        Tier.create({
            "rate_plan_id": plan.id, "tier_type": "pallet",
            "min_qty": min_q, "max_qty": max_q,
            "calc_method": calc_method, "rate": rate,
            "cap_amount": cap,
        })

    # Create schedule if sellable
    if sellable and lane.active:
        existing_sched = Schedule.search([("service_offering_id", "=", offering.id)], limit=1)
        if not existing_sched:
            pickup_day_name = sellable[0]
            sched_vals = {"service_offering_id": offering.id, "cutoff_time": 16.0,
                          "delivery_offset_type": delivery_offset, "active": True}
            for i, day_name in enumerate(DAY_NAMES):
                sched_vals[WEEKDAY_MAP[i]] = (day_name == pickup_day_name)
            Schedule.create(sched_vals)
            schedules_created += 1

print(f"LANES: {lanes_created} created, {lanes_updated} updated")
print(f"OFFERINGS: {offerings_created} created")
print(f"RATE PLANS: {plans_created} created, {plans_updated} updated")
print(f"SCHEDULES: {schedules_created} created")

# ── STEP 5: Global Surcharges ────────────────────────────────────────
GLOBAL_SURCHARGES = [
    ("LIFTGATE_PICKUP", "Liftgate Pickup", "flat", 50.0),
    ("LIFTGATE_DELIVERY", "Liftgate Delivery", "flat", 50.0),
    ("APPOINTMENT", "Appointment", "flat", 35.0),
    ("RESIDENTIAL", "Residential / Limited Access", "flat", 75.0),
    ("TEMP_CHILLED", "Chilled Temperature Premium", "percent", 15.0),
    ("TEMP_FROZEN", "Frozen Temperature Premium", "percent", 20.0),
    ("SAME_DAY_EXPRESS", "Same-Day Express Premium", "percent", 25.0),
    ("WEEKEND", "Weekend Service Premium", "percent", 20.0),
    ("CROSS_BORDER", "Cross-Border Processing", "flat", 150.0),
    ("FUEL", "Fuel Surcharge", "percent", 0.0),
]
for code, name, calc_type, amount in GLOBAL_SURCHARGES:
    st = SurchargeType.search([("code", "=", code)], limit=1)
    vals = {"name": name, "calc_type": calc_type, "default_amount": amount, "is_global": True, "active": True}
    if st:
        st.write(vals)
    else:
        SurchargeType.create(dict(code=code, **vals))
print(f"SURCHARGES: {len(GLOBAL_SURCHARGES)} loaded")

# ── STEP 6: Holiday Calendar (Ontario 2026-2027) ─────────────────────
cal = HolidayCal.search([("name", "=", "Ontario Statutory Holidays")], limit=1)
if not cal:
    cal = HolidayCal.create({"name": "Ontario Statutory Holidays"})
ONTARIO_HOLIDAYS = [
    ("2026-01-01", "New Year's Day"), ("2026-02-16", "Family Day"),
    ("2026-04-03", "Good Friday"), ("2026-05-18", "Victoria Day"),
    ("2026-07-01", "Canada Day"), ("2026-08-03", "Civic Holiday"),
    ("2026-09-07", "Labour Day"), ("2026-10-12", "Thanksgiving"),
    ("2026-12-25", "Christmas Day"), ("2026-12-26", "Boxing Day"),
    ("2027-01-01", "New Year's Day"), ("2027-02-15", "Family Day"),
    ("2027-03-26", "Good Friday"), ("2027-05-24", "Victoria Day"),
    ("2027-07-01", "Canada Day"),
]
for date_str, desc in ONTARIO_HOLIDAYS:
    existing = HolidayLine.search([("calendar_id", "=", cal.id), ("date", "=", date_str)], limit=1)
    if not existing:
        HolidayLine.create({"calendar_id": cal.id, "date": date_str, "description": desc})
print(f"HOLIDAYS: {len(ONTARIO_HOLIDAYS)} dates loaded")

env.cr.commit()
print("=== PHASE 2 FULL LOADER COMPLETE ===")
