# -*- coding: utf-8 -*-
"""Post-install wiring.

Deliberately additive and idempotent: it only touches the ONE job this
workflow owns (configured, defaulting to hr.job 5) and only ADDS the
Authority & Insurance Review stage to it. The other job positions keep
their own stages — stage 2-6 are linked to jobs 1-4 and are never edited
here.
"""
import logging

_logger = logging.getLogger(__name__)

OWNER_OPERATOR_JOB_ID = 5
AUTHORITY_STAGE_NAME = "Authority & Insurance Review"


def post_init_hook(env):
    job = env["hr.job"].browse(OWNER_OPERATOR_JOB_ID).exists()
    if not job:
        _logger.warning(
            "owner-operator: hr.job %s not found — nothing wired",
            OWNER_OPERATOR_JOB_ID)
        return

    env["ir.config_parameter"].sudo().set_param(
        "prema_owner_operator.job_id", job.id)

    stage = env["hr.recruitment.stage"].search(
        [("name", "=", AUTHORITY_STAGE_NAME)], limit=1)
    if not stage:
        _logger.warning(
            "owner-operator: stage %r missing — the pipeline is incomplete",
            AUTHORITY_STAGE_NAME)
    elif job not in stage.job_ids:
        stage.write({"job_ids": [(4, job.id)]})
        _logger.info("owner-operator: linked %r to job %s",
                     AUTHORITY_STAGE_NAME, job.id)

    _fix_alias(job)

    # Only the owner-operator pipeline is asserted here; everything else is
    # reported, never changed.
    expected = ["Equipment Verification", "Initial Qualification",
                AUTHORITY_STAGE_NAME, "Contract Proposal",
                "Contract Signed", "Approved — Ready for Dispatch"]
    have = set(env["hr.recruitment.stage"].search(
        [("job_ids", "in", job.id)]).mapped("name"))
    missing = [name for name in expected if name not in have]
    if missing:
        _logger.warning(
            "owner-operator: job %s is missing stages: %s", job.id, missing)


def _fix_alias(job):
    """Point the job alias at the address applicants actually write to.

    The public address is owneroperators@premafirm.com (a mail-server alias
    that forwards to catchall@premafirm.com). The job answered on
    @logistics.premafirm.com, so Odoo could never match the recipient and an
    emailed application never reached hr.applicant.

    mail.alias.domain has no company column in Odoo 18, so aligning the
    domain with the job's company is not a constraint here — but the alias
    must still not collide with one that already answers for another record.
    """
    alias = job.alias_id
    if not alias:
        _logger.warning("owner-operator: job %s has no alias", job.id)
        return
    domain = env["mail.alias.domain"].search(
        [("name", "=", "premafirm.com")], limit=1)
    if not domain:
        _logger.warning("owner-operator: alias domain premafirm.com missing")
        return
    if alias.alias_domain_id.id == domain.id:
        return
    clash = env["mail.alias"].search([
        ("alias_name", "=", alias.alias_name),
        ("alias_domain_id", "=", domain.id),
        ("id", "!=", alias.id),
    ], limit=1)
    if clash:
        _logger.warning(
            "owner-operator: %s@%s already answered by %s %s — alias left "
            "as-is for manual review",
            alias.alias_name, domain.name, clash._name, clash.id)
        return
    alias.write({"alias_domain_id": domain.id})
    _logger.info("owner-operator: alias moved to %s@%s",
                 alias.alias_name, domain.name)
