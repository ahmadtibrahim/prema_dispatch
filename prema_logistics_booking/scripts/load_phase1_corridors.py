"""Phase 14: Seed Phase 1 Operating Schedule as corridors with ordered stops.

Creates 6 operating corridors for the Phase 1 weekly schedule.
Run via: odoo-bin shell -c /etc/odoo18.conf -d Prod-db < this_script.py
Idempotent — searches by name before creating.
"""
env = locals().get("env")
if env is None:
    print("ERROR: No Odoo environment. Run via odoo-bin shell.")
    exit(1)

Region = env["logistics.region"]
Corridor = env["logistics.corridor"]
CorridorStop = env["logistics.corridor.stop"]

def get_region(code):
    return Region.search([("code", "=", code)], limit=1)

def get_or_create_corridor(name, **vals):
    existing = Corridor.search([("name", "=", name)], limit=1)
    if existing:
        print(f"  EXISTS: {name}")
        return existing
    cor = Corridor.create({"name": name, **vals})
    env.cr.commit()
    print(f"  CREATED: {name}")
    return cor

def add_stops(corridor, stop_list):
    existing = CorridorStop.search([("corridor_id", "=", corridor.id)])
    if existing:
        print(f"    {len(existing)} stops already exist")
        return
    for i, (code, name, pu, dl, dist) in enumerate(stop_list):
        region = get_region(code)
        CorridorStop.create({
            "corridor_id": corridor.id, "sequence": (i + 1) * 10,
            "region_id": region.id if region else False, "name": name,
            "pickup_allowed": pu, "delivery_allowed": dl,
            "distance_from_origin_km": dist,
        })
    env.cr.commit()
    print(f"    Created {len(stop_list)} stops")

print("=== Phase 14: Phase 1 Corridor Seed ===\n")

# 1. Monday Local GTA
mon_local = get_or_create_corridor(
    "Monday Local GTA", direction="local", equipment_type="dry",
    phase=1, truck_slot=1, operate_monday=True, start_time=7.0,
    full_distance_km=120.0, full_revenue_target=800.0,
    planned_pallets=8,
)
add_stops(mon_local, [
    ("R1", "Mississauga Hub", True, True, 0),
    ("R3", "Hamilton", True, True, 70),
    ("R1", "Mississauga Hub", False, True, 120),
])

# 2. Tuesday Eastbound Quebec
tue_eb = get_or_create_corridor(
    "Tuesday Eastbound Quebec", direction="eastbound", equipment_type="dry",
    phase=1, truck_slot=1, operate_tuesday=True, start_time=1.0, overnight=True,
    full_distance_km=1050.0, full_revenue_target=2300.0,
    planned_pallets=8,
)
add_stops(tue_eb, [
    ("R1", "Mississauga Hub", True, True, 0),
    ("R6", "Belleville", False, True, 200),
    ("R6", "Kingston", False, True, 280),
    ("R6", "Brockville", False, True, 360),
    ("R6", "Cornwall", False, True, 450),
    ("R8", "Montreal", False, True, 600),
    ("R9", "Drummondville", False, True, 780),
    ("R10", "Quebec City", False, True, 1050),
])

# 3. Wednesday Westbound Return
wed_wb = get_or_create_corridor(
    "Wednesday Westbound Return", direction="westbound", equipment_type="dry",
    phase=1, truck_slot=1, operate_wednesday=True, start_time=8.0, overnight=True,
    full_distance_km=1050.0, full_revenue_target=2300.0,
    planned_pallets=8,
    paired_return_service_id=tue_eb.id,
)
tue_eb.write({"paired_return_service_id": wed_wb.id})
add_stops(wed_wb, [
    ("R10", "Quebec City", True, True, 0),
    ("R9", "Drummondville", True, True, 270),
    ("R8", "Montreal", True, True, 450),
    ("R6", "Cornwall", True, True, 600),
    ("R6", "Kingston", True, True, 770),
    ("R6", "Belleville", True, True, 850),
    ("R1", "Mississauga Hub", True, True, 1050),
])

# 4. Thursday Local GTA
thu_local = get_or_create_corridor(
    "Thursday Local GTA", direction="local", equipment_type="dry",
    phase=1, truck_slot=1, operate_thursday=True, start_time=7.0,
    full_distance_km=120.0, full_revenue_target=800.0,
    planned_pallets=8,
)
add_stops(thu_local, [
    ("R1", "Mississauga Hub", True, True, 0),
    ("R1", "GTA Local", True, True, 60),
    ("R1", "Mississauga Hub", False, True, 120),
])

# 5. Friday Ottawa Direct
fri_ott = get_or_create_corridor(
    "Friday Ottawa Direct", direction="bidirectional", equipment_type="dry",
    phase=1, truck_slot=1, operate_friday=True, start_time=6.0,
    full_distance_km=920.0, full_revenue_target=1200.0,
    planned_pallets=8,
)
add_stops(fri_ott, [
    ("R1", "Mississauga Hub", True, True, 0),
    ("R6", "Kingston", False, True, 280),
    ("R7", "Ottawa", False, True, 460),
    ("R6", "Kingston", True, False, 640),
    ("R1", "Mississauga Hub", True, True, 920),
])

# 6. Saturday Optional
get_or_create_corridor(
    "Saturday Optional Deliveries", direction="local", equipment_type="dry",
    phase=1, truck_slot=1, operate_saturday=True, start_time=8.0,
    conditional=True, min_departure_revenue=300.0,
    full_distance_km=200.0, full_revenue_target=500.0,
    planned_pallets=4,
)

env.cr.commit()
print("\n=== DONE: 6 corridors seeded for Phase 1 schedule ===")
print(f"Corridors: {Corridor.search_count([])}, Stops: {CorridorStop.search_count([])}")
