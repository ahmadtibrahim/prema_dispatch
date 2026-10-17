{
    "name": "Prema Owner-Operator Onboarding",
    "version": "18.0.1.0.0",
    "summary": "Owner-operator application, qualification, contract signing and Fleet hand-off for logistics.premafirm.com",
    "category": "Logistics",
    "description": """
Owner-Operator Onboarding
=========================

One application model, one document checklist, one approval path:

* public application form (no navigation, noindex, out of sitemap) that
  collects contact/business details, home city + province, equipment,
  capabilities, lanes and availability — with conditional authority fields;
* a private upload store (never a public /web/content URL) with per-record
  access rules;
* a staged qualification checklist (CVOR/provincial certificate, applicant
  COI, business documents, truck photos, driver licence, insurer-direct COI);
* stage-scoped document-request and acknowledgement emails;
* contract proposal through Odoo Sign — which is BLOCKED while no approved
  agreement template is configured (this module never invents terms);
* an idempotent finalisation that links the operator profile to its partner
  and its fleet.vehicle, without creating employees, without merging
  identities by email and without merging vehicles by plate;
* a Fleet availability predicate that also honours the
  "Issue / Temporarily Unavailable" state.

The workflow is scoped to the Owner Operator job (hr.job id 5); the other
job positions keep their own recruitment stages untouched.
""",
    "depends": [
        "hr_recruitment",
        "website_hr_recruitment",
        "website",
        "fleet",
        "sign",
        "mail",
    ],
    "data": [
        "security/owner_operator_security.xml",
        "security/ir.model.access.csv",
        "data/owner_operator_data.xml",
        "data/mail_templates.xml",
        "views/owner_operator_views.xml",
        "views/owner_operator_application_views.xml",
        "views/fleet_vehicle_views.xml",
        "views/website_apply_templates.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "prema_owner_operator/static/src/scss/apply.scss",
        ],
    },
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
