"""SAVED LOCATION CONSOLIDATION 18.0.13.25.0 — retire the legacy models.

Drops logistics.saved.location, logistics.saved.location.hours and
logistics.saved.location.exception (tables + ir.model metadata + the
views/action/menu/rules/ACL records and xmlids of the removed files).

Safe because the audit in 18.0.13.24.0 proved zero live references:
all Prema Dispatch bookings/jobs/quotes were test data and cleared; the
FK scan showed only the legacy children (hours 14 rows, exception 0)
and 0-row constraints on the 6 referencing columns.
"""

RETIRED_MODELS = [
    "logistics.saved.location",
    "logistics.saved.location.hours",
    "logistics.saved.location.exception",
]

RETIRED_TABLES = [
    "logistics_saved_location_exception",
    "logistics_saved_location_hours",
    "logistics_saved_location",
]

RETIRED_XMLIDS = [
    "view_logistics_saved_location_list",
    "view_logistics_saved_location_form",
    "view_logistics_saved_location_search",
    "action_logistics_saved_location",
    "menu_saved_locations",
    "ir_rule_saved_location_portal_own",
    "ir_rule_saved_location_internal",
    "ir_rule_saved_location_hours_portal_own",
    "ir_rule_saved_location_hours_internal",
    "access_logistics_saved_location_viewer",
    "access_logistics_saved_location_admin",
    "access_logistics_saved_location_portal",
    "access_logistics_saved_location_hours_viewer",
    "access_logistics_saved_location_hours_admin",
    "access_logistics_saved_location_hours_user",
    "access_logistics_saved_location_hours_portal",
    "access_logistics_saved_location_exception_viewer",
    "access_logistics_saved_location_exception_admin",
    "access_logistics_saved_location_exception_user",
    "access_logistics_saved_location_exception_portal",
]


def migrate(cr, version):
    # 1. Physical tables (children first; the hours FK cascades anyway).
    for table in RETIRED_TABLES:
        cr.execute("DROP TABLE IF EXISTS %s CASCADE" % table)

    # 2. ir.model metadata + model-scoped rows.
    cr.execute("SELECT id FROM ir_model WHERE model = ANY(%s)", (RETIRED_MODELS,))
    model_ids = [r[0] for r in cr.fetchall()]
    if model_ids:
        cr.execute("DELETE FROM ir_model_fields WHERE model_id = ANY(%s)", (model_ids,))
        cr.execute("DELETE FROM ir_model_access WHERE model_id = ANY(%s)", (model_ids,))
        cr.execute("DELETE FROM ir_rule WHERE model_id = ANY(%s)", (model_ids,))
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.model' AND res_id = ANY(%s)",
            (model_ids,))
        cr.execute("DELETE FROM ir_model WHERE id = ANY(%s)", (model_ids,))

    # 3. Records behind the removed views/action/menu/rules/ACL xmlids.
    cr.execute(
        "SELECT model, res_id FROM ir_model_data "
        "WHERE module = 'prema_logistics_booking' AND name = ANY(%s)",
        (RETIRED_XMLIDS,))
    by_model = {}
    for model, res_id in cr.fetchall():
        by_model.setdefault(model, []).append(res_id)
    for model, ids in by_model.items():
        if model == "ir.ui.view":
            cr.execute("DELETE FROM ir_ui_view WHERE id = ANY(%s)", (ids,))
        elif model == "ir.actions.act_window":
            cr.execute("DELETE FROM ir_act_window WHERE id = ANY(%s)", (ids,))
        elif model == "ir.ui.menu":
            cr.execute("DELETE FROM ir_ui_menu WHERE id = ANY(%s)", (ids,))
        elif model == "ir.rule":
            cr.execute("DELETE FROM ir_rule WHERE id = ANY(%s)", (ids,))
        elif model == "ir.model.access":
            cr.execute("DELETE FROM ir_model_access WHERE id = ANY(%s)", (ids,))
    cr.execute(
        "DELETE FROM ir_model_data "
        "WHERE module = 'prema_logistics_booking' AND name = ANY(%s)",
        (RETIRED_XMLIDS,))
