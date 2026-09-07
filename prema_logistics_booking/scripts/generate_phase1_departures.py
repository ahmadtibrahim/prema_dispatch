"""Maintain the customer-visible rolling departure horizon.

Corridors own their weekly days, start time, holiday calendars, default truck,
and eight-week booking horizon.  This script is intentionally data-driven: it
does not search corridor names, hardcode weekdays, or pick the first truck.
"""


def generate_phase1_departures(env, weeks=8):
    """Reconcile every configured corridor; kept as the cron API.

    Active corridors keep their rolling horizon of generated departures.
    Inactive corridors are reconciled too (heal loop): future generated
    departures that never began are retired there (once a real booking /
    open dispatch job cleared), so a disabled corridor can never keep
    blocking its truck. This also self-heals pre-existing stale rows that
    were left scheduled+active by the old inactive-write() defect.

    ``weeks`` remains accepted for older callers/tests, but the public horizon
    is capped at eight weeks by the corridor model.
    """
    requested_weeks = min(max(int(weeks or 8), 1), 8)
    totals = {
        "created": 0,
        "updated": 0,
        "removed": 0,
        "preserved_booked": 0,
        "preserved_job": 0,
        "reactivated": 0,
        "skipped": 0,
        "weeks": requested_weeks,
    }
    corridors = env["logistics.corridor"].search([("active", "=", True)])
    for corridor in corridors:
        if not corridor._operating_weekdays():
            totals["skipped"] += 1
            continue
        original_weeks = corridor.departure_horizon_weeks
        if original_weeks != requested_weeks:
            corridor.with_context(skip_departure_reconcile=True).write({
                "departure_horizon_weeks": requested_weeks,
            })
        result = corridor._reconcile_departure_horizon()
        for key in ("created", "updated", "removed", "preserved_booked",
                    "preserved_job", "reactivated"):
            totals[key] += result.get(key, 0)
    inactive = env["logistics.corridor"].with_context(active_test=False).search(
        [("active", "=", False)])
    for corridor in inactive:
        result = corridor._reconcile_departure_horizon()
        for key in ("removed", "preserved_booked", "preserved_job"):
            totals[key] += result.get(key, 0)
    return totals


if __name__ == "__main__":
    result = generate_phase1_departures(env, weeks=8)  # noqa: F821
    print(
        "Departure horizon: "
        f"{result['created']} created, {result['updated']} updated, "
        f"{result['removed']} removed over {result['weeks']} weeks"
    )
