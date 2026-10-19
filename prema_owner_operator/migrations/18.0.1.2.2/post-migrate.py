# -*- coding: utf-8 -*-
"""18.0.1.2.2 — one canonical stage per name for job 5.

The 1.2.1 hook could reuse a stock stage with the same name while the
module's own stage remained linked, producing duplicate columns. This run
selects a stable canonical stage, remaps applicants off duplicates, unlinks
job 5 from the duplicates and preserves the stage records for other jobs.
No email is sent.
"""
from odoo import SUPERUSER_ID, api

from odoo.addons.prema_owner_operator.hooks import (
    ensure_pipeline, ensure_verification_alias)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_pipeline(env)
    ensure_verification_alias(env)
