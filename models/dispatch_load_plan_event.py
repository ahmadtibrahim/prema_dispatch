from odoo import fields, models

EVENT_TYPES = [
    ("created", "Created"), ("job_added", "Job Added"), ("job_removed", "Job Removed"),
    ("layout_selected", "Layout Selected"), ("layout_changed", "Layout Changed"),
    ("pallet_assigned", "Pallet Assigned"), ("pallet_moved", "Pallet Moved"),
    ("pallets_swapped", "Pallets Swapped"), ("pallet_unassigned", "Pallet Unassigned"),
    ("pallet_loaded", "Pallet Loaded"), ("pallet_unloaded", "Pallet Unloaded"),
    ("pallet_handed_off", "Pallet Handed Off"), ("pallet_received", "Pallet Received"),
    ("stop_allocation_changed", "Stop Allocation Changed"), ("route_sequence_changed", "Route Sequence Changed"),
    ("marked_stale", "Marked Stale"), ("stale_cleared", "Stale Cleared"),
    ("recommendation_generated", "Recommendation Generated"),
    ("recommendation_accepted", "Recommendation Accepted"), ("recommendation_rejected", "Recommendation Rejected"),
    ("locked", "Locked"), ("unlocked", "Unlocked"), ("override_approved", "Override Approved"),
    ("document_uploaded", "Document Uploaded"), ("damage_reported", "Damage Reported"),
    ("unverified_layout_acknowledged", "Unverified Layout Acknowledged"),
]


class PremaDispatchLoadPlanEvent(models.Model):
    _name = "prema.dispatch.load.plan.event"
    _description = "Load Plan Audit Event"
    _order = "changed_at desc"

    load_plan_id = fields.Many2one("prema.dispatch.load.plan", required=True, ondelete="cascade", index=True)
    event_type = fields.Selection(EVENT_TYPES, required=True, index=True)
    item_id = fields.Many2one("prema.dispatch.item", ondelete="set null")
    from_position_id = fields.Many2one("prema.dispatch.vehicle.layout.position", ondelete="set null")
    to_position_id = fields.Many2one("prema.dispatch.vehicle.layout.position", ondelete="set null")
    from_load_plan_id = fields.Many2one("prema.dispatch.load.plan", ondelete="set null")
    to_load_plan_id = fields.Many2one("prema.dispatch.load.plan", ondelete="set null")
    changed_by = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    changed_at = fields.Datetime(default=fields.Datetime.now, readonly=True)
    reason = fields.Text()
    old_value_json = fields.Text()
    new_value_json = fields.Text()
    snapshot_json = fields.Text()
