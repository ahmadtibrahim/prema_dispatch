# -*- coding: utf-8 -*-
"""18.0.1.2.7 — approval wiring only; no data is migrated or moved."""


def migrate(cr, version):
    # The driver contact, Documents copies and Fleet fields are created at
    # approval time. Existing applications, partners, vehicles, attachments,
    # COI records and stage links are deliberately untouched.
    return
