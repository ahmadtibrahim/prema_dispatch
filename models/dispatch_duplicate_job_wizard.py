from odoo import fields, models


class DispatchDuplicateJobWizard(models.TransientModel):
    """Shown when 'Book Load' is clicked on an invoice that already has a
    dispatch job — lets the dispatcher choose to reopen it instead of
    silently creating a duplicate, per audit item 2."""
    _name = "prema.dispatch.duplicate.job.wizard"
    _description = "Dispatch Job Already Exists"

    move_id = fields.Many2one("account.move", required=True)
    existing_job_ids = fields.Many2many(
        "prema.dispatch.job", string="Existing Dispatch Job(s)", readonly=True
    )
    message = fields.Text(readonly=True)

    def action_open_existing(self):
        self.ensure_one()
        jobs = self.existing_job_ids
        if len(jobs) == 1:
            return {
                "type": "ir.actions.act_window",
                "name": "Dispatch Job",
                "res_model": "prema.dispatch.job",
                "res_id": jobs.id,
                "view_mode": "form",
            }
        return {
            "type": "ir.actions.act_window",
            "name": "Dispatch Jobs",
            "res_model": "prema.dispatch.job",
            "view_mode": "list,form",
            "domain": [("id", "in", jobs.ids)],
        }

    def action_create_additional(self):
        self.ensure_one()
        return self.move_id._do_action_book_load()
