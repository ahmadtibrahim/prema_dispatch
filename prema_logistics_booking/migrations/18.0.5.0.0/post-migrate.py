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
        values = dict(day_fields, weekday=False, recurring_weekdays=False,
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
    Job = env["logistics.recurring.job"]
    for agreement in env["logistics.recurring.agreement"].with_context(active_test=False).search([]):
        if agreement.job_ids:
            continue
        pickup_region = agreement.pickup_fsa_id.region_id
        delivery_region = agreement.delivery_fsa_id.region_id
        if not pickup_region or not delivery_region:
            _logger.warning("Agreement %s needs manual endpoint migration", agreement.id)
            continue
        Job.create({
            "agreement_id": agreement.id,
            "name": "Migrated recurring route",
            "pickup_kind": "region", "pickup_region_id": pickup_region.id,
            "delivery_kind": "region", "delivery_region_id": delivery_region.id,
            "frequency": agreement.frequency or "weekly",
            "preferred_weekday": str(agreement.preferred_weekday or 0),
            "monthly_week": "1",
            "pallets": agreement.pallets or 1,
            "weight_lbs": agreement.weight_lbs or 0.0,
            "load_type": agreement.load_type or "ltl",
            "temperature_mode": agreement.temperature_mode or "dry",
            "required_temperature_c": agreement.required_temperature_c,
            "temperature_confirmed": agreement.temperature_mode != "reefer",
            "commodity": agreement.commodity or "",
            "auto_generate": False,
        })

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
