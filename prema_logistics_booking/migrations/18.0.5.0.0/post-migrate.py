"""Unify pricing, weekly schedules, recurring jobs and obsolete navigation."""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    from odoo import SUPERUSER_ID, api, fields

    env = api.Environment(cr, SUPERUSER_ID, {})

    # Production-safe default: neither anonymous quote/booking access nor the
    # old public test bypass may be enabled by a module upgrade. Beta portal
    # customers continue to use their explicit partner approval.
    env["ir.config_parameter"].sudo().set_param(
        "logistics_booking.public_test_mode", "False",
    )

    obsolete_menus = (
        "prema_logistics_booking.menu_v4_pricing",
        "prema_logistics_booking.menu_v4_rate_plans",
        "prema_logistics_booking.menu_v4_rate_sim",
        "prema_logistics_booking.menu_v4_customer_rates",
        "prema_logistics_booking.menu_v4_new_booking",
        "prema_dispatch.menu_booking_templates",
    )
    for xmlid in obsolete_menus:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if menu:
            menu.unlink()

    # These XML files were removed from the manifest, but an upgrade does not
    # reliably delete records created by an earlier module version. Remove the
    # stale actions/views explicitly so users cannot reach duplicate pricing,
    # lane, simulator, or schedule-board applications through old bookmarks.
    obsolete_actions = (
        "prema_logistics_booking.action_new_booking",
        "prema_logistics_booking.action_logistics_region_destination",
        "prema_logistics_booking.action_logistics_customer_rate",
        "prema_logistics_booking.action_logistics_rate_plan",
        "prema_logistics_booking.action_logistics_surcharge_type",
        "prema_logistics_booking.action_logistics_lane_schedule",
        "prema_logistics_booking.action_logistics_lane",
        "prema_logistics_booking.action_logistics_schedule_calendar",
        "prema_logistics_booking.action_logistics_route_run",
        "prema_logistics_booking.action_logistics_rate_simulator",
        "prema_logistics_booking.action_logistics_schedule_simulator",
        "prema_logistics_booking.action_logistics_schedule_board",
        "prema_logistics_booking.action_logistics_network_map",
        "prema_logistics_booking.action_logistics_price_matrix",
    )
    obsolete_views = (
        "prema_logistics_booking.view_logistics_region_destination_form",
        "prema_logistics_booking.view_logistics_region_destination_list",
        "prema_logistics_booking.view_logistics_customer_rate_list",
        "prema_logistics_booking.view_logistics_rate_plan_form",
        "prema_logistics_booking.view_logistics_rate_plan_list",
        "prema_logistics_booking.view_logistics_surcharge_type_list",
        "prema_logistics_booking.view_logistics_lane_schedule_form",
        "prema_logistics_booking.view_logistics_lane_schedule_list",
        "prema_logistics_booking.view_logistics_lane_form",
        "prema_logistics_booking.view_logistics_lane_list",
        "prema_logistics_booking.view_logistics_route_run_form",
        "prema_logistics_booking.view_logistics_route_run_list",
        "prema_logistics_booking.view_logistics_rate_simulator_form",
        "prema_logistics_booking.view_logistics_schedule_simulator_form",
        "prema_logistics_booking.weekly_schedule_board",
        "prema_logistics_booking.logistics_network_map_page",
        "prema_logistics_booking.logistics_price_matrix_page",
    )
    removed_legacy_records = 0
    for xmlid in obsolete_actions + obsolete_views:
        record = env.ref(xmlid, raise_if_not_found=False)
        if record:
            record.unlink()
            removed_legacy_records += 1

    # Rate Plans remain as historical references but cannot be selected for
    # new work. Corridors are now the only active pricing authority.
    plans = env["logistics.rate.plan"].with_context(active_test=False).search([("active", "=", True)])
    if plans:
        plans.write({"active": False})

    Corridor = env["logistics.corridor"].with_context(skip_departure_reconcile=True)
    schedules = {
        "LOCAL & REGIONAL OPERATIONS": {
            "operate_monday": True, "operate_thursday": True,
            "same_day_return": True,
        },
        "GTA → QUEBEC (EASTBOUND)": {
            "operate_tuesday": True,
            "start_time": 0.0, "overnight": True, "same_day_return": False,
        },
        "QUEBEC → GTA (WESTBOUND)": {
            "operate_wednesday": True,
            "same_day_return": False,
        },
        "GTA → OTTAWA & RETURN": {
            "operate_friday": True,
            "same_day_return": True,
        },
    }
    day_fields = {
        "operate_monday": False, "operate_tuesday": False, "operate_wednesday": False,
        "operate_thursday": False, "operate_friday": False, "operate_saturday": False,
        "operate_sunday": False,
    }
    for name, schedule in schedules.items():
        corridor = Corridor.search([("name", "=", name)], limit=1)
        if not corridor:
            _logger.warning("Official schedule corridor not found: %s", name)
            continue
        values = dict(day_fields,
                      departure_horizon_weeks=8, rate_per_km=corridor.rate_per_km or 4.0,
                      planned_pallets=corridor.planned_pallets or 8,
                      included_weight_per_pallet=corridor.included_weight_per_pallet or 500.0,
                      minimum_booking_charge=corridor.minimum_booking_charge or 150.0)
        values.update(schedule)
        corridor.write(values)

    # The Planner's new conflict authority uses a timezone-aware local
    # operation date. Backfill old jobs without retroactively blocking the
    # module upgrade when historical data already contains overlaps; all new
    # edits and assignments are checked normally.
    company_tz = env.company.partner_id.tz or env.user.tz or "America/Toronto"
    legacy_jobs = env["prema.dispatch.job"].search([
        ("operation_date", "=", False),
        ("scheduled_pickup", "!=", False),
    ])
    for planner_job in legacy_jobs:
        local_job = planner_job.with_context(tz=company_tz, skip_planner_conflict_check=True)
        local_date = fields.Datetime.context_timestamp(
            local_job, planner_job.scheduled_pickup,
        ).date()
        local_job.write({"operation_date": local_date})

    # Convert each legacy one-route agreement into its first recurring job.
    # The pickup_fsa_id/delivery_fsa_id columns were removed in 18.0.6.0.0 —
    # this post-migration runs AFTER the schema sync drops them, so read them
    # via raw SQL guarded on column existence (the 18.0.6.0.0 pre-migrate
    # performs the same migration while the columns still exist).
    Job = env["logistics.recurring.job"]
    env.cr.execute("""
        SELECT count(*) FROM information_schema.columns
        WHERE table_name = 'logistics_recurring_agreement' AND column_name = 'pickup_fsa_id'
    """)
    if env.cr.fetchone()[0]:
        env.cr.execute("""
            SELECT a.id, fp.region_id, fd.region_id,
                   COALESCE(a.frequency, 'weekly'),
                   COALESCE(a.preferred_weekday, 0),
                   COALESCE(a.pallets, 1), COALESCE(a.weight_lbs, 0.0),
                   COALESCE(a.load_type, 'ltl'),
                   COALESCE(a.temperature_mode, 'dry'),
                   a.required_temperature_c, a.commodity
            FROM logistics_recurring_agreement a
            LEFT JOIN logistics_fsa fp ON a.pickup_fsa_id = fp.id
            LEFT JOIN logistics_fsa fd ON a.delivery_fsa_id = fd.id
            WHERE a.pickup_fsa_id IS NOT NULL OR a.delivery_fsa_id IS NOT NULL
        """)
        rows = env.cr.fetchall()
        for row in rows:
            (agreement_id, pickup_region_id, delivery_region_id, frequency,
             preferred_weekday, pallets, weight_lbs, load_type,
             temperature_mode, required_temperature_c, commodity) = row
            if env["logistics.recurring.job"].search_count([("agreement_id", "=", agreement_id)]):
                continue
            if not pickup_region_id or not delivery_region_id:
                _logger.warning("Agreement %s needs manual endpoint migration", agreement_id)
                continue
            Job.create({
                "agreement_id": agreement_id,
                "name": "Migrated recurring route",
                "pickup_kind": "region", "pickup_region_id": pickup_region_id,
                "delivery_kind": "region", "delivery_region_id": delivery_region_id,
                "frequency": frequency,
                "preferred_weekday": str(preferred_weekday or 0),
                "monthly_week": "1",
                "pallets": pallets,
                "weight_lbs": weight_lbs,
                "load_type": load_type,
                "temperature_mode": temperature_mode,
                "required_temperature_c": required_temperature_c,
                "temperature_confirmed": temperature_mode != "reefer",
                "commodity": commodity or "",
                "auto_generate": False,
            })
    else:
        _logger.info("pickup_fsa_id column gone — legacy agreement migration handled by 18.0.6.0.0 pre-migrate")

    # Rebuild only future unbooked schedule rows after the new weekly rules
    # are in place; booked/completed records are preserved by the model.
    for corridor in env["logistics.corridor"].search([("active", "=", True)]):
        if corridor._operating_weekdays():
            corridor._reconcile_departure_horizon()

    _logger.info(
        "18.0.5.0.0 dispatch unification migration complete: "
        "removed %d obsolete UI records; backfilled %d Planner operation dates",
        removed_legacy_records,
        len(legacy_jobs),
    )
