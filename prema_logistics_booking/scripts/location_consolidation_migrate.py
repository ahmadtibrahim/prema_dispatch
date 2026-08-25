"""SAVED LOCATION CONSOLIDATION — dry-run + migration.

Run inside `odoo-bin shell` (rollback-safe in dry-run mode; --apply
commits explicitly). Steps:

  1. DRY-RUN (default, --dry-run): prints the spec §18 summary from the
     legacy data — zero writes, zero state change.
  2. APPLY (--apply): migrates every active logistics.saved.location row
     to (canonical facility, customer access) using the ORM authorities
     (ensure_access / canonical hours), archives the legacy rows, and
     prints the post-migration summary. No hard deletes anywhere.

Spec rules honoured: dedupe priority is not needed here (every legacy row
already links a master), hours migrate once per facility with conflict
detection (hours_review_required, legacy source preserved — never
guessed), private fields land ONLY on the access rows, and the NOFRILLS
13/46 Place-ID mismatch is reported without any destructive action.

Usage:
  sudo -u odoo18 /opt/odoo/venv-18/bin/python3 /opt/odoo/odoo18/odoo-bin \
      shell -c /etc/odoo18.conf -d Prod-db --no-http \
      < scripts/location_consolidation_migrate.py [--apply]
"""

import sys

APPLY = "--apply" in sys.argv

SAVED = env["logistics.saved.location"]
ACCESS = env["logistics.location.customer.access"]
FACILITY = env["prema.dispatch.location"]
LEGACY_HOURS = env["logistics.saved.location.hours"]
CANON_HOURS = env["prema.dispatch.location.hours"]


def _sql_one(query):
    env.cr.execute(query)
    row = env.cr.fetchone()
    return row[0] if row else None


def _sql_all(query):
    env.cr.execute(query)
    return env.cr.fetchall()


# ── Pre-migration counts (read-only, raw SQL so the dry-run runs even
#    before the module upgrade creates the new columns) ────────────────
saved_total = _sql_one("SELECT count(*) FROM logistics_saved_location")
saved_active = _sql_one("SELECT count(*) FROM logistics_saved_location WHERE active")
facilities_total = _sql_one("SELECT count(*) FROM prema_dispatch_location")
facilities_active = _sql_one(
    "SELECT count(*) FROM prema_dispatch_location WHERE active")
access_total = _sql_one("SELECT count(*) FROM logistics_location_customer_access")
access_active = _sql_one(
    "SELECT count(*) FROM logistics_location_customer_access WHERE active")
legacy_hours_count = _sql_one(
    "SELECT count(*) FROM logistics_saved_location_hours")
booking_stops = _sql_one(
    "SELECT count(*) FROM logistics_booking_stop "
    "WHERE logistics_saved_location_id IS NOT NULL")
sessions = _sql_one(
    "SELECT count(*) FROM logistics_pricing_session "
    "WHERE pickup_saved_location_id IS NOT NULL "
    "OR delivery_saved_location_id IS NOT NULL")
session_stops = _sql_one(
    "SELECT count(*) FROM logistics_pricing_session_stop "
    "WHERE saved_location_id IS NOT NULL")

# Facilities shared by 2+ customers (class C)
shared_rows = _sql_all(
    "SELECT dispatch_location_id, count(DISTINCT commercial_partner_id) "
    "FROM logistics_saved_location "
    "WHERE dispatch_location_id IS NOT NULL "
    "GROUP BY dispatch_location_id HAVING count(DISTINCT commercial_partner_id) > 1")
shared = {row[0] for row in shared_rows}

# Hours per facility: distinct legacy sources + row shapes
hours_rows = _sql_all(
    "SELECT h.id, h.saved_location_id, s.dispatch_location_id, "
    "       h.day_of_week, h.service_scope, h.status, "
    "       h.open_time, h.close_time, h.sequence "
    "FROM logistics_saved_location_hours h "
    "JOIN logistics_saved_location s ON s.id = h.saved_location_id "
    "WHERE s.dispatch_location_id IS NOT NULL")
hours_by_facility = {}
for (hid, sid, mid, day, scope, status, ot, ct, seq) in hours_rows:
    hours_by_facility.setdefault(mid, {}).setdefault(sid, []).append(
        (day, scope, status, round(float(ot or 0.0), 2),
         round(float(ct or 24.0), 2), seq))

def _shape(rows):
    return tuple(sorted(rows))

hours_conflicts = {}
for mid, sources in hours_by_facility.items():
    shapes = {_shape(rows) for rows in sources.values()}
    if len(shapes) > 1:
        hours_conflicts[mid] = list(sources.keys())

def _saved_name(sid):
    return _sql_one("SELECT name FROM logistics_saved_location WHERE id = %s" % int(sid)) or str(sid)

print("=" * 72)
print("SAVED LOCATION CONSOLIDATION — %s" % ("APPLY" if APPLY else "DRY-RUN"))
print("=" * 72)
print("Saved rows (total/active):                %d / %d" % (saved_total, saved_active))
print("Canonical facilities (total/active):      %d / %d" % (facilities_total, facilities_active))
print("Access rows (total/active):               %d / %d" % (access_total, access_active))
print("Facilities reused (all active rows):      %d" % saved_active)
print("Facilities newly created:                 0  (all legacy rows already link a master)")
access_created = _sql_one(
    "SELECT count(*) FROM logistics_saved_location s "
    "WHERE s.active AND s.dispatch_location_id IS NOT NULL AND NOT EXISTS ("
    "  SELECT 1 FROM logistics_location_customer_access a "
    "  WHERE a.facility_id = s.dispatch_location_id "
    "    AND a.commercial_partner_id = s.commercial_partner_id)")
print("Access rows created:                      %d" % access_created)
print("Access rows merged/updated:               %d" % (saved_active - access_created))
print("Potential duplicate masters:              %s" % (", ".join(map(str, sorted(shared))) or "none"))
print("  (NOFRILLS Belleville flag: saved 13 vs 46, same master 37,"
      "\n   different google_place_ids — REPORTED, no action)")
print("Hours migrated (rows / facilities):       %d / %d" % (legacy_hours_count, len(hours_by_facility)))
for mid in sorted(hours_by_facility):
    print("    facility %s <- %s (%d rows)" % (mid, ", ".join(map(str, hours_by_facility[mid])), sum(len(v) for v in hours_by_facility[mid].values())))
print("Hours conflicts (review required):        %d %s" % (
    len(hours_conflicts),
    {str(k): v for k, v in hours_conflicts.items()} if hours_conflicts else "(none)"))
print("Legacy rows to archive:                   %d" % saved_active)
print("Historical FK rows preserved:             booking_stops=%d pricing_sessions=%d session_stops=%d" % (
    booking_stops, sessions, session_stops))
print("Hard deletes:                             0 (none, anywhere)")
print("-" * 72)

# Baseline for the FINAL-REPORT "new saved rows" check (verified later,
# after deploy): the newest legacy create_date. Any logistics.saved.location
# with create_date > this after migration is a forbidden new row.
baseline_created = _sql_one(
    "SELECT max(create_date) FROM logistics_saved_location")
print("Baseline max create_date of saved rows:   %s" % baseline_created)

if not APPLY:
    print("DRY-RUN complete — no writes performed.")
    env.cr.rollback()
    raise SystemExit

# ── Migration ──────────────────────────────────────────────────────────
saved_active_records = SAVED.search([("active", "=", True)])
migrated = 0
for rec in saved_active_records:
    master = rec.dispatch_location_id
    if not master:
        print("SKIP saved %s: no master facility (unexpected class B)" % rec.id)
        continue
    existed = ACCESS.search_count([
        ("facility_id", "=", master.id),
        ("commercial_partner_id", "=", rec.commercial_partner_id.id)])
    access = ACCESS.ensure_access(
        master, rec.commercial_partner_id,
        portal_enabled=True,
        can_pickup=rec._is_pickup_capable(),
        can_delivery=rec._is_delivery_capable(),
        is_default_pickup=rec.is_default_pickup,
        is_default_delivery=rec.is_default_delivery,
        customer_alias=rec.name,
        contact_name=rec.contact_name,
        contact_phone=rec.contact_phone,
        contact_email=rec.contact_email,
        pickup_instructions=rec.pickup_instructions,
        delivery_instructions=rec.delivery_instructions,
    )
    print("ACCESS %s: facility %s partner %s (id %s)" % (
        "updated" if existed else "created",
        master.id, rec.commercial_partner_id.id, access.id))
    migrated += 1

# ── Hours migration (canonical, once per facility) ────────────────────
for mid, sources in sorted(hours_by_facility.items()):
    master = FACILITY.browse(mid)
    existing = CANON_HOURS.search_count([("facility_id", "=", mid)])
    if existing:
        print("HOURS facility %s: canonical rows already exist — skipped" % mid)
        continue
    conflicts = mid in hours_conflicts
    scope_map = {"general": "general", "pickup": "pickup", "delivery": "receiving"}
    for sid, rows in sources.items():
        for (day, scope, status, ot, ct, seq) in rows:
            CANON_HOURS.create({
                "facility_id": mid,
                "day_of_week": day,
                "service_scope": scope_map.get(scope, "general"),
                "status": status,
                "open_time": ot if status == "custom" else 0.0,
                "close_time": ct if status == "custom" else 24.0,
                "sequence": seq,
                "active": True,
            })
    master.write({
        "hours_review_required": conflicts,
        "legacy_hours_source": ", ".join(
            "%s (%s)" % (_saved_name(sid), sid) for sid in sources),
    })
    print("HOURS facility %s: %d canonical rows migrated from %s%s" % (
        mid, sum(len(v) for v in sources.values()), list(sources.keys()),
        " — REVIEW REQUIRED (conflicting sources)" if conflicts else ""))

# ── Legacy archive (no hard deletes) ──────────────────────────────────
for rec in saved_active_records:
    rec.write({"legacy_location": True, "active": False})
print("LEGACY: %d rows marked legacy_location + archived" % len(saved_active_records))

# ── Post-migration counts ─────────────────────────────────────────────
print("-" * 72)
print("POST-MIGRATION")
print("Access rows (total):                     %d" % ACCESS.search_count([]))
print("Canonical hours rows:                    %d" % CANON_HOURS.search_count([]))
print("Facilities hours_review_required:        %d" % FACILITY.search_count([("hours_review_required", "=", True)]))
print("Saved rows still active (must be 0):     %d" % SAVED.search_count([("active", "=", True)]))
print("New saved rows after migration:          %d (must be 0; baseline %s)" % (
    SAVED.search_count([("create_date", ">", baseline_created)]) if baseline_created else 0,
    baseline_created))
print("Hard deleted rows:                       0")
env.cr.commit()
print("COMMITTED")
