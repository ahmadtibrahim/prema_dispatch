"""Prema Logistics Settings — Freight Tax Configuration.

Settings → Prema AI → Logistics Settings → Freight Tax Configuration

Maps account.tax records for automatic freight tax resolution by
destination province. Used by logistics.booking._resolve_freight_tax().
"""

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    # ── Freight Tax Mappings ─────────────────────────────────────────────

    logistics_freight_tax_ontario_id = fields.Many2one(
        "account.tax", string="Ontario Freight Tax (HST 13%)",
        config_parameter="logistics.freight_tax_ontario_id",
        domain=[("type_tax_use", "=", "sale")],
        help="Tax applied to freight delivered to Ontario (HST).",
    )
    logistics_freight_tax_quebec_id = fields.Many2one(
        "account.tax", string="Quebec Interprovincial Freight Tax (GST)",
        config_parameter="logistics.freight_tax_quebec_id",
        domain=[("type_tax_use", "=", "sale")],
        help="Tax applied to interprovincial freight delivered to Quebec (GST portion).",
    )
    logistics_freight_tax_quebec_gst_id = fields.Many2one(
        "account.tax", string="Quebec Domestic GST Tax",
        config_parameter="logistics.freight_tax_quebec_gst_id",
        domain=[("type_tax_use", "=", "sale")],
        help="GST portion for Quebec-only pickup and delivery freight.",
    )
    logistics_freight_tax_quebec_qst_id = fields.Many2one(
        "account.tax", string="Quebec Domestic QST Tax",
        config_parameter="logistics.freight_tax_quebec_qst_id",
        domain=[("type_tax_use", "=", "sale")],
        help="QST portion for Quebec-only pickup and delivery freight.",
    )
    logistics_freight_tax_ns_id = fields.Many2one(
        "account.tax", string="Nova Scotia Freight Tax (HST 15%)",
        config_parameter="logistics.freight_tax_ns_id",
        domain=[("type_tax_use", "=", "sale")],
    )
    logistics_freight_tax_nb_id = fields.Many2one(
        "account.tax", string="New Brunswick Freight Tax (HST 15%)",
        config_parameter="logistics.freight_tax_nb_id",
        domain=[("type_tax_use", "=", "sale")],
    )
    logistics_freight_tax_pei_id = fields.Many2one(
        "account.tax", string="PEI Freight Tax (HST 15%)",
        config_parameter="logistics.freight_tax_pei_id",
        domain=[("type_tax_use", "=", "sale")],
    )
    logistics_freight_tax_nl_id = fields.Many2one(
        "account.tax", string="Newfoundland & Labrador Freight Tax (HST 15%)",
        config_parameter="logistics.freight_tax_nl_id",
        domain=[("type_tax_use", "=", "sale")],
    )
    logistics_freight_tax_gst_id = fields.Many2one(
        "account.tax", string="GST Freight Tax (5%)",
        config_parameter="logistics.freight_tax_gst_id",
        domain=[("type_tax_use", "=", "sale")],
        help="Tax applied to freight delivered to AB, BC, MB, SK, NT, YT, NU.",
    )
    logistics_freight_tax_zero_interlining_id = fields.Many2one(
        "account.tax", string="Interlining Zero-Rated Tax (0%)",
        config_parameter="logistics.freight_tax_zero_interlining_id",
        domain=[("type_tax_use", "=", "sale")],
        help="Zero-rated tax for interlining/subcontract carrier freight.",
    )
    logistics_freight_tax_zero_international_id = fields.Many2one(
        "account.tax", string="International Zero-Rated Tax (0%)",
        config_parameter="logistics.freight_tax_zero_international_id",
        domain=[("type_tax_use", "=", "sale")],
        help="Zero-rated tax for qualifying international freight shipments.",
    )

    # ── Phase 3: Default excess weight rate ──────────────────────────

    logistics_default_excess_weight_rate = fields.Float(
        string="Default Excess Weight $ / lb",
        config_parameter="logistics.default_excess_weight_rate",
        default=0.10,
        help="Global default excess weight charge per pound. Individual "
             "corridors can override this on their Customer Pricing tab.",
    )

    # ── Validation ──────────────────────────────────────────────────────

    @api.model
    def get_tax_config_status(self):
        """Return which tax mappings are configured and which are missing.
        Used by the booking tax engine to validate before confirmation."""
        fields_to_check = [
            ("logistics_freight_tax_ontario_id", "Ontario"),
            ("logistics_freight_tax_quebec_id", "Quebec Interprovincial"),
            ("logistics_freight_tax_gst_id", "GST (AB/BC/MB/SK/NT/YT/NU)"),
            ("logistics_freight_tax_zero_interlining_id", "Interlining Zero-Rated"),
            ("logistics_freight_tax_zero_international_id", "International Zero-Rated"),
        ]

        configured = []
        missing = []
        ICP = self.env["ir.config_parameter"].sudo()

        for param_key, label in fields_to_check:
            tax_id = int(ICP.get_param(param_key, "0") or "0")
            if tax_id:
                tax = self.env["account.tax"].sudo().browse(tax_id)
                if tax.exists():
                    configured.append({"label": label, "tax_name": tax.name, "tax_id": tax_id})
                else:
                    missing.append({"label": label, "param": param_key})
            else:
                missing.append({"label": label, "param": param_key})

        return {
            "configured": configured,
            "missing": missing,
            "all_required_configured": len(missing) == 0,
        }
