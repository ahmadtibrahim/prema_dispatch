from odoo import fields, models

class PremaDispatchLocationExtraction(models.Model):
    _name = "prema.dispatch.location.extraction"
    _description = "Location Extraction Audit"
    _order = "created_at desc, id desc"
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null")
    job_id = fields.Many2one("prema.dispatch.job", ondelete="set null", index=True)
    stop_id = fields.Many2one("prema.dispatch.stop", ondelete="set null")
    extraction_context = fields.Selection([("ship_to", "Ship To"), ("pickup_from", "Pickup From"), ("both", "Both")], required=True)
    image_checksum = fields.Char(index=True)
    provider_name = fields.Char(); model_name = fields.Char()
    extracted_json = fields.Text(); normalized_json = fields.Text(); warnings = fields.Text()
    status = fields.Selection([("processing", "Processing"), ("succeeded", "Succeeded"), ("needs_review", "Needs Review"), ("failed", "Failed"), ("confirmed", "Confirmed"), ("cancelled", "Cancelled")], default="processing", index=True)
    created_by = fields.Many2one("res.users", default=lambda self: self.env.user); created_at = fields.Datetime(default=fields.Datetime.now)
    confirmed_by = fields.Many2one("res.users"); confirmed_at = fields.Datetime(); saved_location_id = fields.Many2one("prema.dispatch.location")
    error_code = fields.Char(); error_message = fields.Text()
