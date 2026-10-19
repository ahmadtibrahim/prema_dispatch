# -*- coding: utf-8 -*-
"""18.0.1.2.4 — clean the owner-operator kanban stage relationships.

The applicant kanban's stock group expansion returns every job-less stage, so
detaching a stage from job 5 did not remove its column. The code fix filters
the expansion by the selected job; this migration re-asserts the canonical
five stage links and removes duplicate job-5 relationships. It does not move
applicants, send mail, create operators or create Fleet vehicles.
"""
from odoo import SUPERUSER_ID, api

from odoo.addons.prema_owner_operator.hooks import ensure_pipeline


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_pipeline(env)
