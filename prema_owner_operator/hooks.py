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
NEW_STAGE_NAME = "New"
PIPELINE = [
    NEW_STAGE_NAME,
    AUTHORITY_STAGE_NAME,
    "Contract Proposal",
    "Contract Signed",
    "Approved — Ready for Dispatch",
]
REMOVED_FROM_JOB5 = [
    "Equipment Verification",
    "Initial Qualification",
    "Final Review",
    "First Interview",
    "Second Interview",
]
STAGE_SLUGS = {
    "New": "new",
    "Authority & Insurance Review": "authority",
    "Contract Proposal": "contract",
    "Contract Signed": "signed",
    "Approved — Ready for Dispatch": "approved",
}


def _canonical_stage(env, job, name, candidates):
    """One stable stage per pipeline name, even when stock stages collide.

    Odoo ships stock stages with the same names as some of ours. Picking the
    first by name can leave the module's stage linked alongside a stock one,
    producing duplicate columns. The canonical id is stored in an ICP so later
    runs (and the reviewer UI) keep the same record.
    """
    ICP = env["ir.config_parameter"].sudo()
    slug = STAGE_SLUGS[name]
    stored = ICP.get_param("prema_owner_operator.stage_%s" % slug)
    if stored:
        stage = env["hr.recruitment.stage"].sudo().browse(
            int(stored)).exists()
        if stage and stage in candidates:
            return stage
    used = env["hr.applicant"].sudo().search([
        ("job_id", "=", job.id), ("stage_id", "in", candidates.ids)])
    if used:
        counts = {}
        for stage in used.mapped("stage_id"):
            counts[stage] = counts.get(stage, 0) + 1
        return max(counts, key=lambda s: (counts[s], s.id))
    # No applicant history: keep the most recently created matching record
    # (the module's stage is normally newer than the stock one).
    return candidates.sorted("id")[-1]


def ensure_pipeline(env):
    """Apply the job-5 pipeline without emails or silent advancement.

    Stage records are preserved. Only job 5 is unlinked from the removed
    stages; other jobs keep their links. Existing applicants on a removed
    stage are moved deliberately to New, except Final Review applicants, who
    return to Authority & Insurance Review. The stage-mail hook is disabled
    throughout, so migration sends no email.
    """
    job = env["hr.job"].browse(OWNER_OPERATOR_JOB_ID).exists()
    if not job:
        _logger.warning("owner-operator: hr.job %s not found", OWNER_OPERATOR_JOB_ID)
        return 0
    Stage = env["hr.recruitment.stage"].sudo()
    ICP = env["ir.config_parameter"].sudo()
    applicants = env["hr.applicant"].sudo().search([("job_id", "=", job.id)])
    stages = {}
    for sequence, name in enumerate(PIPELINE):
        candidates = Stage.search([("name", "=", name)])
        if candidates:
            stage = _canonical_stage(env, job, name, candidates)
        else:
            stage = Stage.create({"name": name})
        ICP.set_param(
            "prema_owner_operator.stage_%s" % STAGE_SLUGS[name], stage.id)
        # A stock stage can share our name. Keep ONE canonical stage linked
        # and remap any applicant sitting on a duplicate, preserving history.
        for duplicate in candidates - stage:
            if job in duplicate.job_ids:
                duplicate.write({"job_ids": [(3, job.id)]})
            on_duplicate = env["hr.applicant"].sudo().search([
                ("job_id", "=", job.id),
                ("stage_id", "=", duplicate.id),
            ])
            if on_duplicate:
                on_duplicate.with_context(
                    owner_operator_skip_stage_hook=True).write(
                        {"stage_id": stage.id})
        stage.write({
            "sequence": sequence,
            "job_ids": [(4, job.id)],
        })
        stages[name] = stage
    for name in REMOVED_FROM_JOB5:
        for stage in Stage.search([("name", "=", name),
                                   ("job_ids", "in", job.id)]):
            stage.write({"job_ids": [(3, job.id)]})
    new_stage = stages[NEW_STAGE_NAME]
    authority_stage = stages[AUTHORITY_STAGE_NAME]
    moved = 0
    applicants = env["hr.applicant"].sudo().search([("job_id", "=", job.id)])
    for applicant in applicants:
        if applicant.stage_id.name in stages:
            continue
        # An applicant sitting on the removed Final Review stage was already
        # in insurer review; return them to Authority, not to New. Every other
        # removed stage lands in New (never forward).
        target = authority_stage \
            if applicant.stage_id.name == "Final Review" else new_stage
        applicant.with_context(
            owner_operator_skip_stage_hook=True).write(
                {"stage_id": target.id})
        app = applicant.owner_operator_application_id
        if app:
            app._sync_state(target)
        moved += 1
    if moved:
        _logger.info(
            "owner-operator: moved %s applicant(s) to New without sending mail",
            moved)
    return moved


def ensure_verification_alias(env):
    """Route verification@premafirm.com to the review queue model."""
    domain = env["mail.alias.domain"].sudo().search(
        [("name", "=", "premafirm.com")], limit=1)
    if not domain:
        domain = env["mail.alias.domain"].sudo().create(
            {"name": "premafirm.com"})
    model = env["ir.model"].sudo().search(
        [("model", "=", "prema.owner.operator.verification")], limit=1)
    if not model:
        return False
    vals = {
        "alias_name": "verification",
        "alias_domain_id": domain.id,
        "alias_model_id": model.id,
        "alias_contact": "everyone",
        "alias_defaults": "{}",
    }
    alias = env["mail.alias"].sudo().search([
        ("alias_name", "=", "verification"),
        ("alias_domain_id", "=", domain.id),
    ], limit=1)
    if alias:
        alias.write(vals)
    else:
        alias = env["mail.alias"].sudo().create(vals)
    _logger.info("owner-operator: verification alias -> %s", model.model)
    return alias


def post_init_hook(env):
    job = env["hr.job"].browse(OWNER_OPERATOR_JOB_ID).exists()
    if not job:
        _logger.warning(
            "owner-operator: hr.job %s not found — nothing wired",
            OWNER_OPERATOR_JOB_ID)
        return

    env["ir.config_parameter"].sudo().set_param(
        "prema_owner_operator.job_id", job.id)

    ensure_pipeline(env)
    ensure_verification_alias(env)

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
    expected = list(PIPELINE)
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
