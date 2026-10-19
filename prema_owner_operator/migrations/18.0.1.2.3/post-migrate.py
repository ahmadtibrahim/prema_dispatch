# -*- coding: utf-8 -*-
"""18.0.1.2.3 — five-stage job-5 pipeline, no automatic COI advancement.

Final Review leaves job 5; an applicant historically on it returns to
Authority & Insurance Review, not forward. Removed stage records are kept and
other jobs keep their links. Existing queue records that used the retired
``auto_advanced`` state are relabelled ``matched``; nothing is attached,
advanced or emailed by this migration.
"""
from odoo import SUPERUSER_ID, api

from odoo.addons.prema_owner_operator.hooks import (
    ensure_pipeline, ensure_verification_alias)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_pipeline(env)
    ensure_verification_alias(env)
    queue = env["prema.owner.operator.verification"].sudo().search(
        [("state", "=", "auto_advanced")])
    if queue:
        queue.write({
            "state": "matched",
            "match_reason": (
                "Legacy auto-advance state migrated; the applicant remains in "
                "Authority & Insurance Review for manual review."),
        })
