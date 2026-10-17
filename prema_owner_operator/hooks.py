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

    _check_alias(job)

    # Replay any saved "Other Information" chatter notes into the new
    # structured fields. Repeat-safe and no email is sent.
    updated = env["prema.owner.operator.application"]._backfill_from_other_information()
    if updated:
        _logger.info(
            "owner-operator: backfilled structured answers for %s application(s)",
            updated)

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


def _check_alias(job):
    """The onboarding address STAYS owneroperators@logistics.premafirm.com.

    Decision 2026-10-17: the onboarding mailbox keeps its logistics domain.
    The mail server already forwards that address to catchall@premafirm.com,
    which the "IMAP – catchall" fetchmail server polls, so Odoo sees the
    message with the original recipient header and resolves the job alias.
    This hook therefore only VERIFIES the alias — it never moves the domain.
    """
    alias = job.alias_id
    if not alias:
        _logger.warning("owner-operator: job %s has no alias", job.id)
        return
    expected = "owneroperators"
    if alias.alias_name != expected:
        _logger.warning(
            "owner-operator: job %s alias is %r, expected %r",
            job.id, alias.alias_name, expected)
    _logger.info(
        "owner-operator: onboarding address is %s@%s (mail-server alias must "
        "forward to catchall@premafirm.com and be fetchmail-polled)",
        alias.alias_name,
        alias.alias_domain_id.name if alias.alias_domain_id else "?")
