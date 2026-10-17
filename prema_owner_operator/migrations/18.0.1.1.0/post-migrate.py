# -*- coding: utf-8 -*-
"""Fill structured owner-operator fields from saved website notes.

Runs on an upgrade (the production install uses post_init_hook for the same
repeat-safe call). The original chatter note is never modified.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["prema.owner.operator.application"]._backfill_from_other_information()
