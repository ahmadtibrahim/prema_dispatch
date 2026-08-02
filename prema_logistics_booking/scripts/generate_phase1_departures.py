"""Generate Phase 1 corridor departures for the next 12 weeks.

Idempotent: uses (corridor_id, departure_date) as business key.
Run: python3 odoo-bin shell -c /etc/odoo18.conf -d Prod-db-test1a < this_file
Or: integrated as a cron job after module upgrade.

Schedule:
  Monday    Local & Regional + Quebec Collection/Staging ($1,200 target)
  Tuesday   Quebec Eastbound (00:00-01:00, $2,300 full/$1,600 Montreal)
  Wednesday Quebec Westbound Return
  Thursday  Local & Regional + Ottawa Collection/Staging ($1,200 target)
  Friday    Ottawa Corridor ($1,200 target)
  Saturday  No default operation
  Sunday    No default operation
"""
import datetime

def generate_phase1_departures(env, weeks=12):
    """Generate departures for the next N weeks. Idempotent."""
    Corridor = env["logistics.corridor"]
    Departure = env["logistics.corridor.departure"]
    Vehicle = env["fleet.vehicle"]

    today = datetime.date.today()
    start_monday = today - datetime.timedelta(days=today.weekday())

    # Find corridors by name (seeded by load_phase1_corridors.py)
    quebec_east = Corridor.search([("name", "ilike", "%quebec east%")], limit=1)
    quebec_west = Corridor.search([("name", "ilike", "%quebec west%")], limit=1)
    ottawa_cor = Corridor.search([("name", "ilike", "%ottawa%")], limit=1)
    local_cor = Corridor.search([("direction", "=", "local")], limit=1)

    # Get the operational truck
    truck = Vehicle.search([
        ("active", "=", True),
        ("x_operational_logistics", "=", True),
    ], limit=1)

    vehicle_id = truck.id if truck else False
    created = 0
    skipped = 0

    for week in range(weeks):
        monday = start_monday + datetime.timedelta(weeks=week)
        tuesday = monday + datetime.timedelta(days=1)
        wednesday = monday + datetime.timedelta(days=2)
        thursday = monday + datetime.timedelta(days=3)
        friday = monday + datetime.timedelta(days=4)
        # Saturday and Sunday: no default operation

        departures_to_create = []

        # Monday: Local & Regional Operation (feeds Tuesday Quebec)
        if local_cor:
            departures_to_create.append({
                "corridor": local_cor,
                "date": monday,
                "time": 8.0,  # 08:00
                "cutoff": 17.0,  # 17:00 cutoff
                "target": 1200.0,
            })

        # Tuesday: Quebec Eastbound
        if quebec_east:
            departures_to_create.append({
                "corridor": quebec_east,
                "date": tuesday,
                "time": 0.5,  # 00:30
                "cutoff": 16.0,  # 16:00 Monday cutoff
                "target": 2300.0,
            })

        # Wednesday: Quebec Westbound Return
        if quebec_west:
            departures_to_create.append({
                "corridor": quebec_west,
                "date": wednesday,
                "time": 6.0,  # 06:00 after rest period
                "cutoff": 20.0,  # 20:00 Tuesday cutoff
                "target": 0.0,  # Return target from return_corridor
            })

        # Thursday: Local & Regional (feeds Friday Ottawa)
        if local_cor:
            departures_to_create.append({
                "corridor": local_cor,
                "date": thursday,
                "time": 8.0,
                "cutoff": 17.0,
                "target": 1200.0,
            })

        # Friday: Ottawa Corridor
        if ottawa_cor:
            departures_to_create.append({
                "corridor": ottawa_cor,
                "date": friday,
                "time": 5.0,  # 05:00
                "cutoff": 16.0,  # 16:00 Thursday cutoff
                "target": 1200.0,
            })

        # Saturday/Sunday: only create if confirmed bookings exist
        # (handled by a separate cron or manual action)

        for spec in departures_to_create:
            # Idempotency check: (corridor_id, departure_date)
            existing = Departure.search([
                ("corridor_id", "=", spec["corridor"].id),
                ("departure_date", "=", spec["date"]),
            ], limit=1)

            if existing:
                skipped += 1
                continue

            Departure.create({
                "corridor_id": spec["corridor"].id,
                "departure_date": spec["date"],
                "departure_time": spec["time"],
                "cutoff_time": spec["cutoff"],
                "vehicle_id": vehicle_id,
                "status": "scheduled",
                "max_capacity": 12,
            })
            created += 1

    return {"created": created, "skipped": skipped, "weeks": weeks}


if __name__ == "__main__":
    # Called from odoo shell
    result = generate_phase1_departures(env, weeks=12)  # noqa: F821
    print(f"Phase 1 departures: {result['created']} created, {result['skipped']} skipped over {result['weeks']} weeks")
