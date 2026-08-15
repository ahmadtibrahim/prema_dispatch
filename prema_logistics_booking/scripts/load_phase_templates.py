# V3: Phase 1-4 Corridor + Stop loader (canonical models).
# Run: cd /opt/odoo/odoo18 && sudo -u odoo18 python3 odoo-bin shell -c /etc/odoo18.conf -d Prod-db --no-http < this_file
# IDEMPOTENT: uses stable business keys (name, truck_slot) to avoid duplicates.

Corridor = env["logistics.corridor"]
CorridorStop = env["logistics.corridor.stop"]
Region = env["logistics.region"]
Lane = env["logistics.lane"]
SOffering = env["logistics.service.offering"]

def _r(code):
    """Resolve region by code, returns record or False."""
    return Region.search([("code", "=", code)], limit=1)

def _load_corridor(name, direction, weekday, truck_slot, phase, overnight,
                   conditional, start_code, end_code, stops_csv, target, **extra):
    """Create or update a corridor with idempotent business-key lookup.
    stops_csv: comma-separated region codes in route order, e.g. "R1,R8,R10,R11,R13,R14,R15"
    weekday: 0-5 legacy positional day, converted to the operate_* scheduling booleans.
    """
    # Stable business key: (name, truck_slot)
    existing = Corridor.search([
        ("name", "=", name), ("truck_slot", "=", truck_slot),
    ], limit=1)

    vals = {
        "name": name, "direction": direction, "phase": phase,
        "truck_slot": truck_slot,
        "overnight": overnight, "conditional": conditional,
        "min_departure_revenue": float(target) if conditional else 0.0,
        "full_revenue_target": float(target),
        "active": True,
    }
    for idx, field_name in enumerate((
        "operate_monday", "operate_tuesday", "operate_wednesday",
        "operate_thursday", "operate_friday", "operate_saturday", "operate_sunday",
    )):
        vals[field_name] = (weekday == idx)
    vals.update(extra)

    if existing:
        existing.write(vals)
        cor = existing
        action = "updated"
    else:
        cor = Corridor.create(vals)
        action = "created"

    # Stops: only create if none exist (idempotent)
    if stops_csv and not CorridorStop.search_count([("corridor_id", "=", cor.id)]):
        codes = [c.strip() for c in stops_csv.split(",") if c.strip()]
        dist = 0.0
        step = max(cor.full_distance_km / max(len(codes) - 1, 1), 10) if cor.full_distance_km else 100.0
        for i, code in enumerate(codes):
            r = _r(code)
            dist = i * step
            CorridorStop.create({
                "corridor_id": cor.id,
                "sequence": (i + 1) * 10,
                "region_id": r.id if r else False,
                "name": r.name if r else code,
                "pickup_allowed": True,
                "delivery_allowed": i > 0,  # first stop pickup only
                "distance_from_origin_km": dist,
            })

    return cor, action

# ── PHASE 1 (5 corridors) ─────────────────────────────────────────────
PHASE1 = [
    ("Monday Southwest Feeder","Southwest Feeder","bidirectional",0,1,False,False,"R1","R2","R1,R3,R2",700),
    ("Tuesday Eastbound Quebec","Eastbound Quebec","eastbound",1,1,True,False,"R1","R15","R1,R8,R10,R11,R13,R14,R15",2200),
    ("Wednesday Westbound Return","Westbound Return","westbound",2,1,True,False,"R15","R1","R15,R14,R13,R11,R10,R8,R1",2200),
    ("Thursday Southwest Distribution","Southwest Distribution","bidirectional",3,1,False,False,"R1","R2","R1,R3,R2",1000),
    ("Friday Ottawa Flex","Ottawa Flex","bidirectional",4,1,False,True,"R1","R12","R1,R12",1200),
]

# ── PHASE 2 (10 corridors, 2 trucks) ──────────────────────────────────
PHASE2 = [
    ("Mon East Prep","Eastern Linehaul","bidirectional",0,1,False,False,"R1","R1","R1",0),
    ("Tue Eastbound Quebec","Eastern Linehaul","eastbound",1,1,True,False,"R1","R15","R1,R8,R10,R11,R13,R14,R15",2200),
    ("Wed Westbound Return","Eastern Linehaul","westbound",2,1,True,False,"R15","R1","R15,R14,R13,R11,R10,R8,R1",2200),
    ("Thu East-Central","Eastern Linehaul","bidirectional",3,1,False,False,"R1","R8","R1,R8,R9",800),
    ("Fri Ottawa/Eastern","Eastern Linehaul","bidirectional",4,1,False,True,"R1","R12","R1,R10,R11,R12",1200),
    ("Mon Southwest Feeder","Regional Feeder","bidirectional",0,2,False,False,"R1","R2","R1,R3,R2",700),
    ("Tue Niagara/Golden Horseshoe","Regional Feeder","loop",1,2,False,False,"R1","R4","R1,R4",500),
    ("Wed North/Grey-Bruce","Regional Feeder","loop",2,2,False,False,"R1","R6","R1,R5,R6",800),
    ("Thu Southwest Distribution","Regional Feeder","bidirectional",3,2,False,False,"R1","R2","R1,R3,R2",1000),
    ("Fri Kawartha/East-Central","Regional Feeder","bidirectional",4,2,False,False,"R1","R9","R1,R9,R8",700),
]

stats = {"created": 0, "updated": 0}
for t in PHASE1:
    cor, action = _load_corridor(*t, temperature_capability="all")
    stats[action] += 1
print(f"Phase 1 corridors: {stats['created']} created, {stats['updated']} updated")

stats = {"created": 0, "updated": 0}
for t in PHASE2:
    cor, action = _load_corridor(*t, temperature_capability="all")
    stats[action] += 1
print(f"Phase 2 corridors: {stats['created']} created, {stats['updated']} updated")

# ── Road distances for all Phase 1 active lanes ────────────────────────
ROAD_KM = {("R1","R2"):380,("R1","R3"):190,("R1","R4"):85,("R1","R5"):100,("R1","R6"):190,
    ("R1","R7"):390,("R1","R8"):190,("R1","R9"):135,("R1","R10"):265,("R1","R11"):445,
    ("R1","R12"):450,("R1","R13"):540,("R1","R14"):680,("R1","R15"):800,
    ("R2","R1"):380,("R3","R1"):190,("R4","R1"):85,("R8","R1"):190,("R10","R1"):265,
    ("R11","R1"):445,("R12","R1"):450,("R13","R1"):540,("R14","R1"):680,("R15","R1"):800,
    ("R3","R2"):185,("R3","R13"):730,("R3","R15"):990,("R2","R13"):920,("R2","R15"):1180,
    ("R13","R2"):920,("R15","R2"):1180,("R4","R13"):625,("R13","R4"):625,
    ("R12","R13"):200,("R13","R12"):200,
}
updated_km = 0
for (orig,dest),km in ROAD_KM.items():
    o = _r(orig)
    d = _r(dest)
    if o and d:
        lane = Lane.search([("origin_region_id","=",o.id),("destination_region_id","=",d.id)],limit=1)
        if lane and (not lane.road_km or lane.road_km == 0):
            lane.write({"road_km":float(km)})
            updated_km += 1
print(f"Road distances updated: {updated_km}")

# ── Service status labels ──────────────────────────────────────────────
for off in SOffering.search([("active","=",True)]):
    has_sched = env["logistics.lane.schedule"].search_count([("service_offering_id","=",off.id)]) > 0
    has_plan = env["logistics.rate.plan"].search_count([("service_offering_id","=",off.id)]) > 0

env.cr.commit()
print(f"Total corridors: {Corridor.search_count([])} (V3 canonical)")
print("Phase templates loaded — V3 canonical (logistics.corridor).")
