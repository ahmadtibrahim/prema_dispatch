# -*- coding: utf-8 -*-
"""18.0.1.2.5 — insert the manual Verified stage and COI verification.

The manually created Verified stage is reused, never duplicated. Existing
applicants are not moved and the migration sends no email, creates no
operator/Fleet record and does not start signing.
"""
from odoo import SUPERUSER_ID, api

from odoo.addons.prema_owner_operator.hooks import ensure_pipeline


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_pipeline(env)
