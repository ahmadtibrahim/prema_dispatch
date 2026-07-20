from odoo import fields, models


class PremaDispatchDocument(models.Model):
    _name = "prema.dispatch.document"
    _description = "Load Plan Document Metadata (thin wrapper around ir.attachment)"
    _order = "uploaded_at desc"

    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="cascade", index=True)
    document_type = fields.Selection([
        ("route_sheet", "Route Sheet"),
        ("driver_sheet", "Driver Sheet"),
        ("pickup_invoice", "Pickup Invoice"),
        ("delivery_invoice", "Delivery Invoice"),
        ("pop", "Proof of Pickup"),
        ("pod", "Proof of Delivery"),
        ("loading_photo", "Loading Photo"),
        ("unloading_photo", "Unloading Photo"),
        ("pallet_photo", "Pallet Photo"),
        ("damage_photo", "Damage Photo"),
        ("temperature_photo", "Temperature Photo"),
        ("exception_photo", "Exception Photo"),
        ("other", "Other"),
    ], required=True, default="other")
    load_plan_id = fields.Many2one("prema.dispatch.load.plan", ondelete="cascade", index=True)
    job_id = fields.Many2one("prema.dispatch.job", ondelete="cascade", index=True)
    stop_id = fields.Many2one("prema.dispatch.stop", ondelete="cascade", index=True)
    item_id = fields.Many2one("prema.dispatch.item", ondelete="cascade", index=True)
    uploaded_by = fields.Many2one("res.users", default=lambda self: self.env.user)
    uploaded_at = fields.Datetime(default=fields.Datetime.now)
    driver_visible = fields.Boolean(default=True)
    dispatcher_visible = fields.Boolean(default=True)
    warehouse_visible = fields.Boolean(default=False)
    required = fields.Boolean(default=False)
    verified = fields.Boolean(default=False)
    checksum = fields.Char(help="SHA-256 of the decoded file, from the Phase 1C upload validator.")
    active = fields.Boolean(default=True)

    def name_get(self):
        return [(d.id, f"{dict(d._fields['document_type'].selection).get(d.document_type)}: {d.attachment_id.name}") for d in self]
