# -*- coding: utf-8 -*-
"""Owner-operator review workspace and chatter rendering."""
import os
import uuid

from lxml import etree

from odoo.tests import tagged

from odoo.addons.prema_owner_operator.tests.test_owner_operator import (
    OwnerOperatorCase)


def _load_correction():
    path = os.path.join(
        os.path.dirname(__file__), "..", "migrations",
        "18.0.1.3.3", "post-migrate.py")
    namespace = {}
    with open(path) as handle:
        exec(handle.read(), namespace)
    return namespace["correct_note_bodies"]


@tagged("post_install", "-at_install")
class TestReviewWorkspace(OwnerOperatorCase):

    def _application(self):
        return self.Application._create_from_public(
            self._public_vals(
                email="review-%s@test.invalid" % uuid.uuid4().hex[:8]))

    def test_review_next_action_is_populated(self):
        app = self._application()
        self.assertTrue(app.review_next_action)
        self.assertFalse(app.can_approve)

    def test_review_workspace_is_on_the_recruitment_record(self):
        root = etree.fromstring(
            self.env["hr.applicant"].get_view(False, "form")["arch"])
        review = root.xpath('//page[@name="oo_review"]')
        self.assertTrue(review)
        self.assertTrue(root.xpath(
            '//page[@name="oo_review"]//field[@name="oo_review_next_action"]'))
        self.assertTrue(root.xpath(
            '//page[@name="oo_review"]//button[@name="action_owner_operator_advance"]'))
        self.assertTrue(root.xpath(
            '//page[@name="oo_review"]//button[@name="action_owner_operator_approve"]'))
        self.assertTrue(root.xpath(
            '//page[@name="oo_review"]//field[@name="owner_operator_document_ids"]'))
        options = root.xpath(
            '//field[@name="can_preview"]/@options')
        self.assertTrue(any("coi_version" in value for value in options))
        strings = [p.get("string") for p in review[0].xpath(".//group")]
        for expected in ("Needs upload", "Received — needs review",
                         "Verified", "Required later (contract)",
                         "Applicant / forwarded COI copy",
                         "Insurer-direct COI (requested)"):
            self.assertIn(expected, strings)

    def test_duplicated_hr_fields_are_hidden_only_for_job_5(self):
        root = etree.fromstring(
            self.env["hr.applicant"].get_view(False, "form")["arch"])
        details = root.xpath('//page[@name="application_details"]')
        self.assertTrue(details)
        self.assertEqual(details[0].get("invisible"), "job_id == 5")
        skills = [p for p in root.xpath("//page")
                  if p.get("string") == "Skills"]
        self.assertTrue(skills)
        self.assertEqual(skills[0].get("invisible"), "job_id == 5")
        # Source/medium stay reachable in Advanced, not in the main flow.
        advanced = root.xpath('//page[@name="oo_advanced"]')
        self.assertTrue(advanced)
        for field in ("source_id", "medium_id", "department_id",
                      "company_id"):
            self.assertTrue(
                advanced[0].xpath('.//field[@name="%s"]' % field), field)

    def test_other_recruitment_jobs_keep_stock_fields(self):
        other = self.env["hr.job"].create({
            "name": "Other job %s" % uuid.uuid4().hex[:6],
            "company_id": self.env.company.id,
        })
        candidate = self.env["hr.candidate"].create({
            "partner_name": "Other Candidate",
            "email_from": "other-%s@test.invalid" % uuid.uuid4().hex[:8],
        })
        applicant = self.env["hr.applicant"].create({
            "candidate_id": candidate.id,
            "job_id": other.id,
            "partner_name": "Other Candidate",
            "email_from": candidate.email_from,
            "company_id": self.env.company.id,
        })
        self.assertFalse(applicant.owner_operator_application_id)
        root = etree.fromstring(
            self.env["hr.applicant"].get_view(False, "form")["arch"])
        # The hiding rules are record conditions, never global removal.
        note = root.xpath('//page[@name="application_note"]')
        self.assertTrue(note)
        self.assertEqual(note[0].get("invisible"), "job_id == 5")


@tagged("post_install", "-at_install")
class TestChatterCorrections(OwnerOperatorCase):

    def _message(self, lead, body, message_type="comment"):
        subtype = self.env.ref(
            "mail.mt_note" if message_type != "email" else "mail.mt_comment")
        return self.env["mail.message"].sudo().create({
            "model": "crm.lead",
            "res_id": lead.id,
            "body": body,
            "message_type": message_type,
            "subtype_id": subtype.id,
        })

    def test_double_escaped_note_is_corrected_once_and_sanitized(self):
        lead = self.env["crm.lead"].create({"name": "Chatter Test"})
        message = self._message(
            lead,
            '<p>&lt;p&gt;👁️ &lt;b&gt;Email opened&lt;/b&gt; — '
            '"&lt;script&gt;alert(1)&lt;/script&gt;"'
            '&lt;/p&gt;</p>')
        untouched = self._message(
            lead,
            '<p>&lt;p&gt;👁️ &lt;b&gt;Email opened&lt;/b&gt;</p>',
            message_type="email")
        correct = _load_correction()
        self.assertEqual(correct(self.env), 1)
        self.assertIn("<b>Email opened</b>", message.body)
        self.assertNotIn("&lt;b&gt;Email opened", message.body)
        self.assertNotIn("<script", message.body)
        self.assertIn("&lt;p&gt;", untouched.body)  # outgoing mail preserved
        self.assertEqual(correct(self.env), 0)

    def test_prema_ai_stage_note_is_corrected_and_names_are_escaped(self):
        lead = self.env["crm.lead"].create({"name": "Gobolt <Test>"})
        message = self._message(
            lead,
            '<p>Prema AI moved this opportunity from &lt;b&gt;OLD&lt;/b&gt; '
            'to &lt;b&gt;NEW&lt;/b&gt; at X\'s request.</p>')
        correct = _load_correction()
        self.assertEqual(correct(self.env), 1)
        self.assertIn("<b>OLD</b>", message.body)
        self.assertNotIn("&lt;b&gt;OLD", message.body)
        self.assertEqual(correct(self.env), 0)


@tagged("post_install", "-at_install")
class TestVerificationNoteMarkup(OwnerOperatorCase):

    def test_verification_copy_is_html_and_untrusted_values_are_escaped(self):
        app = self.Application._create_from_public(
            self._public_vals(
                email="chatter-%s@test.invalid" % uuid.uuid4().hex[:8]))
        verification = self.env[
            "prema.owner.operator.verification"].create({
                "name": "[%s] Safely rendered" % app.name,
                "state": "matched",
                "matched_application_id": app.id,
                "sender_email": "attacker@test.invalid",
                "subject": "<b>Subject</b>",
                "message_id": "<id@test.invalid>",
            })
        verification._post_to_application(app, {
            "email_from": '"<img src=x onerror=alert(1)>" <x@test.invalid>',
            "to": "verification@premafirm.com",
            "subject": "<b>Subject</b>",
            "message_id": "<id@test.invalid>",
            "body": "<p>Hello</p><script>alert(1)</script>",
        }, "Matched for manual review.")
        message = app.message_ids.filtered(
            lambda m: m.subtype_id == self.env.ref("mail.mt_note")
            and (m.subject or "").startswith("<b>Subject"))[:1]
        self.assertTrue(message)
        self.assertTrue(message.body.startswith(
            "<p><strong>Verification email</strong>"))
        self.assertNotIn("&lt;p&gt;", message.body)
        self.assertNotIn("<script", message.body)
        self.assertNotIn("<img", message.body)
        self.assertIn("&lt;img", message.body)
        self.assertIn("<p>Hello</p>", message.body)
        self.assertIn("&lt;b&gt;Subject&lt;/b&gt;", message.body)


@tagged("post_install", "-at_install")
class TestReviewNextAction(OwnerOperatorCase):

    def test_authority_review_action_names_the_coi(self):
        app = self.Application._create_from_public(
            self._public_vals(
                email="next-%s@test.invalid" % uuid.uuid4().hex[:8]))
        app.write({"state": "authority_review",
                   "verification_status": "review"})
        self.assertIn("Review the received COI", app.review_next_action)

    def test_signed_action_explains_unavailable_approval(self):
        app = self.Application._create_from_public(
            self._public_vals(
                email="signed-%s@test.invalid" % uuid.uuid4().hex[:8]))
        app.write({"state": "signed"})
        self.assertIn("Approval is unavailable", app.review_next_action)
