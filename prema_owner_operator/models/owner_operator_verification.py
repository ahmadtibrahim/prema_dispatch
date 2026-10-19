# -*- coding: utf-8 -*-
"""Inbound insurance-verification mailbox.

Every message addressed to verification@premafirm.com lands in THIS queue —
never in hr.applicant. A queue record is matched to an EXISTING application by
the application reference (OO-######) and/or the reply-thread Message-IDs.
Sender addresses and business names are evidence for the reviewer, never the
sole match key.

A match only attaches a certificate as RECEIVED. It never verifies coverage,
never moves the applicant, never approves the operator and never creates an
applicant. The applicant stays in Authority & Insurance Review; a reviewer
checks the insurer, policy dates and coverage and then moves it manually to
Contract Proposal.
"""
import base64
import logging
import re
from email.utils import parseaddr

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

REFERENCE_RE = re.compile(r"\bOO-\d{6}\b", re.IGNORECASE)
CERT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".webp")


class OwnerOperatorVerification(models.Model):
    _name = "prema.owner.operator.verification"
    _description = "Owner-Operator Verification Email"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "received_date desc, id desc"

    name = fields.Char(required=True, default="Verification email")
    company_id = fields.Many2one(
        "res.company", required=True, index=True,
        default=lambda self: self.env.company)
    state = fields.Selection([
        ("received", "Received"),
        ("matched", "Matched — certificate in review"),
        ("applicant_forwarded", "Applicant-forwarded copy"),
        ("no_certificate", "No certificate attachment"),
        ("unmatched", "Unmatched — review queue"),
        ("ambiguous", "Ambiguous reference — review queue"),
        ("conflict", "Conflicting references — review queue"),
        ("manual", "Manually attached by reviewer"),
        ("duplicate", "Duplicate (ignored)"),
    ], default="received", required=True, tracking=True, index=True)
    matched_application_id = fields.Many2one(
        "prema.owner.operator.application", string="Matched application",
        index=True)
    matched_applicant_id = fields.Many2one(
        related="matched_application_id.hr_applicant_id", store=True)
    match_reason = fields.Text(string="Match reason")

    sender_email = fields.Char(string="Sender")
    to_headers = fields.Text(string="To")
    cc_headers = fields.Text(string="Cc")
    subject = fields.Char()
    message_id = fields.Char(index=True)
    in_reply_to = fields.Char()
    references_header = fields.Text(string="References")
    received_date = fields.Datetime(string="Received")
    raw_headers = fields.Text(string="Original headers")
    body = fields.Html(string="Email body")

    attachment_count = fields.Integer(compute="_compute_attachment_count")
    certificate_attachment_id = fields.Many2one(
        "ir.attachment", string="Certificate to attach",
        help="Leave empty to use the first PDF/image attachment on this "
             "email's chatter.")
    notes = fields.Text()

    _sql_constraints = [
        ("message_id_unique", "UNIQUE(message_id)",
         "This verification email has already been processed."),
    ]

    @api.depends("message_ids.attachment_ids")
    def _compute_attachment_count(self):
        for rec in self:
            rec.attachment_count = len(rec.message_ids.attachment_ids)

    # ------------------------------------------------------------------
    # inbound
    # ------------------------------------------------------------------
    @api.model
    def message_new(self, msg_dict, custom_values=None):
        message_id = (msg_dict.get("message_id") or "").strip()
        if message_id:
            existing = self.search([("message_id", "=", message_id)], limit=1)
            if existing:
                return existing
        sender = msg_dict.get("email_from") or ""
        vals = {
            "name": msg_dict.get("subject") or _("Verification email"),
            "company_id": self.env.company.id,
            "state": "received",
            "sender_email": parseaddr(sender)[1].lower() or sender,
            "to_headers": msg_dict.get("to"),
            "cc_headers": msg_dict.get("cc"),
            "subject": msg_dict.get("subject"),
            "message_id": message_id or False,
            "in_reply_to": msg_dict.get("in_reply_to"),
            "references_header": msg_dict.get("references"),
            "received_date": msg_dict.get("date") or fields.Datetime.now(),
            "raw_headers": self._header_block(msg_dict),
            "body": msg_dict.get("body"),
        }
        if isinstance(custom_values, dict):
            vals.update(custom_values)
        record = self.create(vals)
        try:
            record._match_and_process(msg_dict)
        except Exception:  # noqa: BLE001 — never lose an inbound email
            _logger.exception(
                "owner-operator verification: matching failed for %s",
                message_id)
            record.write({
                "state": "unmatched",
                "match_reason": _(
                    "Matching raised an error; the email was kept for manual "
                    "triage."),
            })
        return record

    @api.model
    def _header_block(self, msg_dict):
        return "\n".join([
            "From: %s" % (msg_dict.get("email_from") or ""),
            "To: %s" % (msg_dict.get("to") or ""),
            "Cc: %s" % (msg_dict.get("cc") or ""),
            "Subject: %s" % (msg_dict.get("subject") or ""),
            "Message-ID: %s" % (msg_dict.get("message_id") or ""),
            "In-Reply-To: %s" % (msg_dict.get("in_reply_to") or ""),
            "References: %s" % (msg_dict.get("references") or ""),
            "Date: %s" % (msg_dict.get("date") or ""),
        ])

    # ------------------------------------------------------------------
    # matching
    # ------------------------------------------------------------------
    @api.model
    def _sender_address(self, msg_dict):
        raw = msg_dict.get("email_from") or ""
        return (parseaddr(raw)[1] or raw).strip().lower()

    @api.model
    def _references(self, msg_dict):
        raw = " ".join(filter(None, [
            msg_dict.get("in_reply_to") or "",
            msg_dict.get("references") or "",
        ]))
        return [ref.strip() for ref in re.findall(r"<[^>]+>", raw)]

    def _reference_applications(self, msg_dict):
        hay = " ".join(filter(None, [
            msg_dict.get("subject") or "",
            msg_dict.get("body") or "",
            msg_dict.get("to") or "",
            msg_dict.get("cc") or "",
        ]))
        refs = {ref.upper() for ref in REFERENCE_RE.findall(hay)}
        if not refs:
            return self.env["prema.owner.operator.application"]
        return self.env["prema.owner.operator.application"].sudo().search(
            [("name", "in", list(refs))])

    def _thread_applications(self, msg_dict):
        refs = self._references(msg_dict)
        if not refs:
            return self.env["prema.owner.operator.application"]
        Message = self.env["mail.message"].sudo()
        messages = Message.search([
            ("model", "=", "prema.owner.operator.application"),
            ("message_id", "in", refs),
        ])
        apps = self.env["prema.owner.operator.application"].sudo().browse(
            messages.mapped("res_id"))
        # The outbound COI request's own Message-ID is also a valid thread key.
        apps |= self.env["prema.owner.operator.application"].sudo().search([
            ("coi_request_message_id", "in", refs),
        ])
        return apps.exists()

    @api.model
    def _certificate_attachments(self, msg_dict):
        found = []
        for attachment in msg_dict.get("attachments") or []:
            filename = (attachment[0] or "").lower()
            if filename.endswith(CERT_EXTENSIONS):
                found.append(attachment)
        return found

    @api.model
    def _trusted_domains(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            "prema_owner_operator.trusted_insurer_domains") or ""
        return {
            part.strip().lower().lstrip("@")
            for part in raw.split(",") if part.strip()
        }

    def _match_and_process(self, msg_dict):
        self.ensure_one()
        sender = self._sender_address(msg_dict)
        by_ref = self._reference_applications(msg_dict)
        by_thread = self._thread_applications(msg_dict)
        certificates = self._certificate_attachments(msg_dict)

        if by_ref and by_thread and set(by_ref.ids) != set(by_thread.ids):
            self.write({
                "state": "conflict",
                "match_reason": _(
                    "The subject reference and the reply thread point to "
                    "different applications (%(ref)s vs %(thread)s).") % {
                    "ref": ", ".join(by_ref.mapped("name")),
                    "thread": ", ".join(by_thread.mapped("name")),
                },
            })
            self._schedule_queue_activity(_("Conflicting verification email"))
            return self

        refs = by_ref or by_thread
        if not refs:
            self.write({
                "state": "unmatched",
                "match_reason": _(
                    "No application reference and no recognised reply thread. "
                    "No applicant was created."),
            })
            self._schedule_queue_activity(_("Unmatched verification email"))
            return self
        if len(refs) > 1:
            self.write({
                "state": "ambiguous",
                "match_reason": _(
                    "The email points to more than one application: %s.")
                % ", ".join(refs.mapped("name")),
            })
            self._schedule_queue_activity(_("Ambiguous verification email"))
            return self

        app = refs
        self.write({
            "matched_application_id": app.id,
            "company_id": app.company_id.id or self.company_id.id,
            "match_reason": _("Matched by %s.") % (
                "application reference" if by_ref else "reply thread"),
        })

        if not certificates:
            self.write({"state": "no_certificate", "match_reason": _(
                "Matched to %(app)s but the email has no PDF/image "
                "certificate attachment. Nothing was attached and the "
                "application did not advance.") % {"app": app.name}})
            self._post_to_application(
                app, msg_dict,
                _("Verification email received without a certificate "
                  "attachment. No checklist change was made."))
            self._schedule_application_activity(
                app, _("Verification email has no certificate"))
            return self

        applicant_emails = {
            (app.email or "").strip().lower(),
            (app.partner_id.email or "").strip().lower(),
        } - {""}
        if sender in applicant_emails:
            self.write({"state": "applicant_forwarded", "match_reason": _(
                "Matched to %(app)s but the sender is the applicant. An "
                "applicant-forwarded copy is not insurer-direct receipt and "
                "did not advance the application.")
                % {"app": app.name}})
            self._post_to_application(
                app, msg_dict,
                _("Applicant-forwarded certificate kept for manual triage. "
                  "It was NOT recorded as insurer-direct receipt."))
            self._schedule_application_activity(
                app, _("Applicant-forwarded COI needs review"))
            return self

        domain = sender.split("@")[-1] if "@" in sender else sender
        if domain and domain in self._trusted_domains():
            self._attach_certificate(app, certificates, msg_dict)
            self.write({
                "state": "matched",
                "match_reason": _(
                    "Matched to %(app)s and the sender domain %(domain)s is "
                    "trusted. Certificate stored as Received. The applicant "
                    "remains in Authority & Insurance Review; a reviewer must "
                    "check the insurer, policy dates and coverage and move it "
                    "to Contract Proposal manually.")
                % {"app": app.name, "domain": domain},
            })
            self._post_to_application(
                app, msg_dict,
                _("Insurer-direct certificate received from %s. Stored as "
                  "Received. The applicant remains in Authority & Insurance "
                  "Review pending reviewer verification of the insurer, "
                  "policy dates and coverage.") % sender)
            self._schedule_application_activity(
                app, _("Verify insurer-direct COI"))
            return self

        self.write({
            "state": "matched",
            "match_reason": _(
                "Matched to %(app)s but the sender domain %(domain)s is not "
                "trusted. The certificate was kept in this queue for manual "
                "triage and the application did not advance.")
            % {"app": app.name, "domain": domain or "?"},
        })
        self._post_to_application(
            app, msg_dict,
            _("Verification email matched but its source is not trusted. "
              "The certificate is in the verification queue for manual "
              "triage; it was NOT recorded as insurer-direct receipt."))
        self._schedule_application_activity(
            app, _("Verification email needs source review"))
        return self

    # ------------------------------------------------------------------
    # attachment / stage actions
    # ------------------------------------------------------------------
    def _attachment_payload(self, attachment):
        content = attachment[1]
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not content:
            return False
        if isinstance(content, bytes):
            return content
        return False

    def _mimetype_for(self, filename):
        lower = (filename or "").lower()
        if lower.endswith(".pdf"):
            return "application/pdf"
        if lower.endswith(".png"):
            return "image/png"
        if lower.endswith(".webp"):
            return "image/webp"
        return "image/jpeg"

    def _attach_certificate(self, app, certificates, msg_dict):
        doc = app.document_ids.filtered(
            lambda d: d.document_type == "coi_insurer")[:1]
        if not doc:
            doc = self.env["prema.owner.operator.document"].sudo().create({
                "application_id": app.id,
                "document_type": "coi_insurer",
                "source": "insurer",
            })
        for attachment in certificates:
            filename = attachment[0] or "certificate"
            payload = self._attachment_payload(attachment)
            if not payload:
                continue
            att = self.env["ir.attachment"].sudo().create({
                "name": filename,
                "raw": payload,
                "mimetype": self._mimetype_for(filename),
                "res_model": "prema.owner.operator.document",
                "res_id": doc.id,
                "public": False,
            })
            doc.sudo().write({
                "attachment_id": att.id,
                "status": "received",
                "source": "insurer",
                "received_on": fields.Datetime.now(),
                "notes": _("Received by verification email %s")
                % (msg_dict.get("message_id") or ""),
            })
        return doc

    def _post_to_application(self, app, msg_dict, reason):
        body = (
            "<p><strong>Verification email</strong><br/>"
            "From: %s<br/>To: %s<br/>Subject: %s<br/>Message-ID: %s</p>"
            "<p>%s</p><blockquote>%s</blockquote>"
        ) % (
            msg_dict.get("email_from") or "",
            msg_dict.get("to") or "",
            msg_dict.get("subject") or "",
            msg_dict.get("message_id") or "",
            reason,
            msg_dict.get("body") or "",
        )
        message = app.message_post(
            body=body,
            subject=msg_dict.get("subject"),
            email_from=msg_dict.get("email_from"),
            message_type="email",
            # Internal note: the certificate must not be emailed to the
            # applicant or other followers by the notification engine.
            subtype_xmlid="mail.mt_note",
        )
        # Keep a private copy of every email attachment on the app message.
        for attachment in msg_dict.get("attachments") or []:
            payload = self._attachment_payload(attachment)
            if not payload:
                continue
            self.env["ir.attachment"].sudo().create({
                "name": attachment[0] or "attachment",
                "raw": payload,
                "mimetype": self._mimetype_for(attachment[0]),
                "res_model": "mail.message",
                "res_id": message.id,
                "public": False,
            })
        return message

    @api.model
    def _activity_user(self, app=None):
        icp = self.env["ir.config_parameter"].sudo()
        raw = icp.get_param("prema_owner_operator.verification_reviewer_id")
        if raw:
            user = self.env["res.users"].browse(int(raw)).exists()
            if user:
                return user
        if app and app.hr_applicant_id.user_id:
            return app.hr_applicant_id.user_id
        group = self.env.ref(
            "prema_owner_operator.group_owner_operator",
            raise_if_not_found=False)
        if group and group.users[:1]:
            return group.users[:1]
        return self.env.ref("base.user_admin", raise_if_not_found=False)

    def _schedule_application_activity(self, app, summary):
        user = self._activity_user(app)
        if not user:
            return False
        app.activity_schedule(
            "mail.mail_activity_data_todo",
            summary=summary,
            note=_("Verification email %s") % (self.name or self.id),
            user_id=user.id)
        return True

    def _schedule_queue_activity(self, summary):
        user = self._activity_user()
        if not user:
            return False
        self.activity_schedule(
            "mail.mail_activity_data_todo",
            summary=summary,
            note=_("Open the verification queue record and triage it."),
            user_id=user.id)
        return True

    # ------------------------------------------------------------------
    # reviewer actions
    # ------------------------------------------------------------------
    def _first_certificate_attachment(self):
        self.ensure_one()
        if self.certificate_attachment_id:
            return self.certificate_attachment_id
        for attachment in self.message_ids.attachment_ids:
            if (attachment.name or "").lower().endswith(CERT_EXTENSIONS):
                return attachment
        return self.env["ir.attachment"]

    def action_manual_attach(self):
        for record in self:
            app = record.matched_application_id
            if not app:
                raise UserError(_(
                    "Choose the application this certificate belongs to."))
            attachment = record._first_certificate_attachment()
            if not attachment:
                raise UserError(_(
                    "This email has no PDF/image certificate attachment."))
            content = attachment.raw
            if not content:
                raise UserError(_("The certificate attachment is empty."))
            certificates = [(attachment.name, content, {})]
            record._attach_certificate(app, certificates, {
                "message_id": record.message_id,
            })
            record.write({
                "state": "manual",
                "match_reason": _(
                    "Certificate manually attached to %(app)s by %(user)s. "
                    "The applicant remains in Authority & Insurance Review; "
                    "move it to Contract Proposal after checking the insurer, "
                    "policy dates and coverage.") % {
                        "app": app.name,
                        "user": self.env.user.name,
                    },
            })
            app.message_post(
                body=_("Certificate attached from verification queue by %s.")
                % self.env.user.name,
                subtype_xmlid="mail.mt_note")
        return True
