from odoo import fields, models

class PremaDispatchLocationPhoto(models.Model):
    _name = "prema.dispatch.location.photo"
    _description = "Saved Location Photo History"
    _order = "uploaded_at desc, id desc"
    location_id = fields.Many2one("prema.dispatch.location", required=True, ondelete="cascade", index=True)
    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="cascade")
    photo_type = fields.Selection([("entrance", "Entrance"), ("truck_entrance", "Truck Entrance"), ("dock", "Dock"), ("parking", "Parking"), ("gate", "Gate"), ("receiving_office", "Receiving Office"), ("scale", "Scale"), ("loading_area", "Loading Area"), ("other", "Other")], required=True)
    source_job_id = fields.Many2one("prema.dispatch.job", ondelete="set null")
    source_stop_id = fields.Many2one("prema.dispatch.stop", ondelete="set null")
    uploaded_by = fields.Many2one("res.users", default=lambda self: self.env.user)
    uploaded_at = fields.Datetime(default=fields.Datetime.now)
    captured_at = fields.Datetime()
    gps_lat = fields.Float(digits=(10, 6)); gps_lng = fields.Float(digits=(10, 6)); gps_accuracy_m = fields.Float()
    notes = fields.Text()
    verification_state = fields.Selection([("driver_submitted", "Driver Submitted"), ("pending_review", "Pending Review"), ("verified", "Verified"), ("rejected", "Rejected")], default="driver_submitted")
    is_primary = fields.Boolean(); active = fields.Boolean(default=True)
