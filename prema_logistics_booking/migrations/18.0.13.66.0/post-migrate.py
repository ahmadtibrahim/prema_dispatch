# -*- coding: utf-8 -*-
"""Retire the Rate Confirmation workflow — remove what the files no longer define.

Deleting a record's definition from the XML/Python does NOT delete the row
Odoo created for it: an upgrade leaves the `ir.ui.view`, the window action,
the ACL and the wizard model sitting in the database, pointing at code that
no longer exists. Left alone they are exactly the kind of clutter the
workflow cleanup is meant to remove — and one of them is worse than clutter.

What this migration removes, and why each one is dead rather than precious:

* ``logistics.custom.quote.revise`` — the "Revise & Resend" wizard. Its model,
  its form, its window action and its three ACLs are gone from the module. The
  table is transient and verified empty before it is dropped.
* ``crm.lead.ml.buttons`` — a DB-only child view (no xmlid, so no file ever
  owned it) that put a second button named **"AI Rate Quote"** on the
  opportunity header, bound to ``action_ml_rate_quote``. That method was
  deleted from the AI engine in 18.0.7.14.0 and its removal migration missed
  this row, so the button has been raising "not a valid action on crm.lead"
  ever since. It would also have competed, visibly and confusingly, with the
  real AI Rate Quote button this release adds.

Nothing here touches a Rate Confirmation: CQ-0019 and CQ-0020 are commercial
records the customer may still cite, and they keep their model, their views,
their report and their read-only history entry points.
"""

import logging

_logger = logging.getLogger(__name__)

# (module, name) of xmlids whose records must go, and the table each one lives in.
_RETIRED_XMLIDS = [
    ("prema_logistics_booking", "view_logistics_custom_quote_revise_form",
     "ir_ui_view"),
    ("prema_logistics_booking", "action_logistics_custom_quote_revise_wizard",
     "ir_act_window"),
    ("prema_logistics_booking", "access_logistics_custom_quote_revise_manager",
     "ir_model_access"),
    ("prema_logistics_booking",
     "access_logistics_custom_quote_revise_booking_manager",
     "ir_model_access"),
    ("prema_logistics_booking", "access_logistics_custom_quote_revise_admin",
     "ir_model_access"),
]

# The retired transient wizard model and its table.
_RETIRED_MODEL = "logistics.custom.quote.revise"
_RETIRED_TABLE = "logistics_custom_quote_revise"

# A second, competing "AI Rate Quote" button that has no owner in any file.
_ORPHAN_VIEW_NAME = "crm.lead.ml.buttons"


def _drop_data_rows(cr, module, name):
    """Remove the ir_model_data rows, returning the record ids they pointed at."""
    cr.execute(
        "SELECT id, res_id FROM ir_model_data WHERE module = %s AND name = %s",
        (module, name),
    )
    rows = cr.fetchall()
    if rows:
        cr.execute(
            "DELETE FROM ir_model_data WHERE module = %s AND name = %s",
            (module, name),
        )
    return [res_id for _xid, res_id in rows]


def migrate(cr, version):
    if not version:
        return

    # 1. The retired wizard's view / window action / ACLs.
    for module, name, table in _RETIRED_XMLIDS:
        res_ids = _drop_data_rows(cr, module, name)
        if res_ids:
            cr.execute(
                "DELETE FROM %s WHERE id = ANY(%%s)" % table, (res_ids,))
            _logger.info(
                "Retired Rate Confirmation workflow: removed %s %s (%s)",
                table, res_ids, name)

    # 2. The wizard model itself — its fields and xmlids cascade off ir_model,
    #    and its (transient) table is dropped only once it is proven empty.
    cr.execute("SELECT id FROM ir_model WHERE model = %s", (_RETIRED_MODEL,))
    model_rows = cr.fetchall()
    for (model_id,) in model_rows:
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.model' AND res_id = %s",
            (model_id,))
        cr.execute("DELETE FROM ir_model WHERE id = %s", (model_id,))
        _logger.info("Retired Rate Confirmation workflow: removed ir.model %s "
                     "(%s)", model_id, _RETIRED_MODEL)
    cr.execute("SELECT to_regclass(%s)", (_RETIRED_TABLE,))
    exists = cr.fetchone()[0]
    if exists:
        cr.execute("SELECT count(*) FROM %s" % _RETIRED_TABLE)
        remaining = cr.fetchone()[0]
        if remaining:
            # Never drop a table that turns out to hold something: a transient
            # wizard should be empty, and if it is not, a human has to look.
            _logger.warning(
                "Retired Rate Confirmation Revise table %s still holds %s "
                "row(s) — left in place for review.", _RETIRED_TABLE, remaining)
        else:
            cr.execute("DROP TABLE %s" % _RETIRED_TABLE)
            _logger.info("Retired Rate Confirmation workflow: dropped table %s",
                         _RETIRED_TABLE)

    # 3. The orphan duplicate "AI Rate Quote" button. Matched on the view's
    #    own name because it has no xmlid — the reason it outlived its
    #    module's removal migration in the first place.
    cr.execute(
        "SELECT id FROM ir_ui_view WHERE name = %s AND model = 'crm.lead'",
        (_ORPHAN_VIEW_NAME,))
    orphans = [row[0] for row in cr.fetchall()]
    for view_id in orphans:
        cr.execute(
            "SELECT count(*) FROM ir_ui_view WHERE inherit_id = %s", (view_id,))
        if cr.fetchone()[0]:
            _logger.warning(
                "Orphan CRM view %s (%s) has child views — left in place.",
                view_id, _ORPHAN_VIEW_NAME)
            continue
        cr.execute(
            "DELETE FROM ir_model_data WHERE model = 'ir.ui.view' AND res_id = %s",
            (view_id,))
        cr.execute("DELETE FROM ir_ui_view WHERE id = %s", (view_id,))
        _logger.info(
            "Removed the dead duplicate 'AI Rate Quote' button view %s (%s).",
            view_id, _ORPHAN_VIEW_NAME)
