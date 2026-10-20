# -*- coding: utf-8 -*-
"""Resend webhook receiver: tracks opens, bounces, spam complaints and delays.

Resend POSTs these events (Svix-signed) to:

    https://logistics.premafirm.com/prema-mail-tracking/resend

Signature verification follows the Svix spec:
    signed   = f"{svix-id}.{svix-timestamp}.{raw_body}"
    key      = base64decode(signing_secret minus the "whsec_" prefix)
    expected = base64(HMAC-SHA256(key, signed))
The signing secret is stored in ir.config_parameter
`prema_mail_tracking.resend_signing_secret` at webhook-registration time.
A bearer of `prema_mail_tracking.webhook_token` (query param `token`) is also
accepted — handy for the self-test / dry-run path (`?token=...&dry_run=1`
performs the matching without writing anything).

Matching strategy (Resend does not echo Odoo's Message-ID on SMTP sends, so the
webhook has no per-mail ID to join on): recipient email -> res.partner, then the
most recent mail_message to that partner in the last 60 days whose subject
matches, falling back to the partner's most recent CRM lead.

Every event that carries a final provider Message-ID also captures a
deterministic outbound link: the payload echoes the ``X-Odoo-Objects`` header
(model-res_id) Odoo stamps on each outbound mail, paired with the final
provider Message-ID (the SES id the recipient's reply will reference).
That pair is stored on prema.resend.message.map and lets premafirm_ai_engine
thread inbound replies without any subject/sender heuristics.

Opens are handled differently from the other events. Resend fires
`email.opened` on every open, so the same mail can arrive here dozens of times.
The payload carries no `open` sub-object (no user agent / IP) but it does carry
Resend's own `email_id`, which is stable per sent mail — so opens are collapsed
onto a `prema.resend.open` row keyed by that id: the first open posts one
chatter note, later opens only bump `open_count`.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import psycopg2
from markupsafe import Markup

from odoo import fields, http
from odoo.http import request
from odoo.tools import email_normalize, html_escape

_logger = logging.getLogger(__name__)

SIGNING_SECRET_ICP = 'prema_mail_tracking.resend_signing_secret'
WEBHOOK_TOKEN_ICP = 'prema_mail_tracking.webhook_token'
SIGNATURE_TOLERANCE = 300  # seconds — reject replays/stale deliveries
LOOKBACK_DAYS = 60
EMAIL_RE = re.compile(r'[^\s<>,;]+@[^\s>,;]+')


class ResendWebhook(http.Controller):

    # ------------------------------------------------------------------
    # Routing / auth
    # ------------------------------------------------------------------

    @http.route('/prema-mail-tracking/resend', type='http', auth='public',
                methods=['POST'], csrf=False)
    def resend_event(self, **kw):
        svix_id = request.httprequest.headers.get('svix-id', '')
        svix_timestamp = request.httprequest.headers.get('svix-timestamp', '')
        svix_signature = request.httprequest.headers.get('svix-signature', '')
        body = request.httprequest.get_data(as_text=False)
        token = request.params.get('token', '')
        dry_run = str(request.params.get('dry_run', '')).lower() in ('1', 'true', 'yes')

        if not (self._verify_signature(svix_id, svix_timestamp, svix_signature, body)
                or self._verify_token(token)):
            _logger.warning('prema_mail_tracking: rejected webhook with bad credentials (svix-id=%r)', svix_id)
            return self._json_response({'ok': False, 'error': 'bad signature or token'}, status=401)

        try:
            payload = json.loads(body.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self._json_response({'ok': False, 'error': 'invalid JSON'}, status=400)

        event_type = payload.get('type', '')
        handler = EVENT_HANDLERS.get(event_type)
        if not handler:
            # Unknown event type: acknowledge so Resend does not retry.
            return self._json_response({'ok': True, 'ignored': event_type})

        data = payload.get('data') or {}
        dedup_key = svix_id or hashlib.sha1(body).hexdigest()

        if request.env['prema.resend.event'].sudo().search([('svix_id', '=', dedup_key)], limit=1):
            return self._json_response({'ok': True, 'duplicate': True})

        result = {'processed': [], 'skipped': [], 'dry_run': dry_run}

        if not dry_run:
            # Store the event first, inside a savepoint: if processing below fails,
            # the event row rolls back too and Resend will retry the delivery.
            try:
                with request.env.cr.savepoint():
                    request.env['prema.resend.event'].sudo().create({
                        'svix_id': dedup_key,
                        'event_type': event_type,
                        'email_from': data.get('from'),
                        'email_to': json.dumps(data.get('to') or []),
                        'subject': data.get('subject'),
                        'payload': body.decode('utf-8', 'replace'),
                    })
            except psycopg2.IntegrityError:
                # unique(svix_id) — lost a race with a concurrent delivery
                return self._json_response({'ok': True, 'duplicate': True})
            except Exception:
                _logger.exception('prema_mail_tracking: could not store event %s', event_type)
                return self._json_response({'ok': False, 'error': 'storage failed'}, status=500)

        try:
            if not dry_run:
                # Best-effort: a mapping failure must never take down the
                # primary handler (bounce notes, blacklists, open counters).
                try:
                    with request.env.cr.savepoint():
                        self._capture_message_map(data)
                except Exception:
                    _logger.exception('prema_mail_tracking: message-map capture '
                                      'failed for %s', event_type)
            handler(self, data, result, dry_run=dry_run)
        except Exception:
            _logger.exception('prema_mail_tracking: processing %s failed', event_type)
            if not dry_run:
                request.env.cr.rollback()
            return self._json_response({'ok': False, 'error': 'processing failed'}, status=500)

        if not dry_run:
            request.env['prema.resend.event'].sudo().search(
                [('svix_id', '=', dedup_key)]).write({'result': json.dumps(result)})
        _logger.info('prema_mail_tracking: %s -> %s', event_type, json.dumps(result))
        return self._json_response({'ok': True, **result})

    def _capture_message_map(self, data):
        """Record the final provider Message-ID of a sent mail against the
        Odoo record it was sent from (X-Odoo-Objects header), so inbound
        replies can be threaded deterministically (no subject/sender
        heuristics). Runs for every event type that carries the fields."""
        message_id = data.get('message_id')
        if not message_id:
            return
        model_name = res_id = None
        for hdr in data.get('headers') or []:
            if str(hdr.get('name', '')).lower() == 'x-odoo-objects':
                value = str(hdr.get('value') or '')
                model_name, _, res_id = value.rpartition('-')
                try:
                    res_id = int(res_id)
                except (TypeError, ValueError):
                    model_name, res_id = None, None
                break
        if not (model_name and res_id):
            return
        vals = {
            'resend_email_id': data.get('email_id') or False,
            'message_id': message_id,
            'model_name': model_name,
            'res_id': res_id,
            'subject': data.get('subject') or '',
            'email_from': data.get('from') or '',
            'email_to': json.dumps(data.get('to') or []),
        }
        Map = request.env['prema.resend.message.map'].sudo()
        existing = Map.search([('message_id', '=', message_id)], limit=1)
        (existing.write(vals) if existing else Map.create(vals))

    def _verify_signature(self, svix_id, svix_timestamp, svix_signature, body):
        if not (svix_id and svix_timestamp and svix_signature):
            return False
        try:
            if abs(time.time() - int(svix_timestamp)) > SIGNATURE_TOLERANCE:
                return False
        except ValueError:
            return False
        secret = request.env['ir.config_parameter'].sudo().get_param(SIGNING_SECRET_ICP, '')
        if not secret:
            return False
        if secret.startswith('whsec_'):
            secret = secret[len('whsec_'):]
        try:
            key = base64.b64decode(secret)
        except Exception:
            return False
        signed = ('%s.%s.' % (svix_id, svix_timestamp)).encode() + body
        expected = base64.b64encode(
            hmac.new(key, signed, hashlib.sha256).digest()).decode()
        for item in svix_signature.split():
            _version, _, sig = item.partition(',')
            if sig and hmac.compare_digest(sig, expected):
                return True
        return False

    def _verify_token(self, token):
        if not token:
            return False
        expected = request.env['ir.config_parameter'].sudo().get_param(WEBHOOK_TOKEN_ICP, '')
        return bool(expected) and hmac.compare_digest(token, expected)

    def _json_response(self, payload, status=200):
        return request.make_response(
            json.dumps(payload),
            headers=[('Content-Type', 'application/json')],
            status=status,
        )

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_resend_dt(value):
        """Resend timestamps to naive UTC (Odoo stores naive UTC datetimes).

        Formats seen: '2026-02-22T23:41:11.894Z', with +00:00, with microsecond
        '+00' suffix, or space-separated '2026-08-20 17:23:44.134000+00'.
        """
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return False
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    def _find_partner(self, raw_email):
        email = email_normalize(raw_email or '')
        if not email:
            match = EMAIL_RE.search(raw_email or '')
            email = email_normalize(match.group(0)) if match else False
        if not email:
            return None
        return request.env['res.partner'].sudo().search(
            [('email_normalized', '=', email)], limit=1)

    def _normalize_subject(self, subject):
        s = (subject or '').strip().lower()
        while s.startswith(('re:', 'fwd:')):
            s = s[3:].strip()
        return s

    def _find_message(self, partner, data):
        """Most recent mail_message sent to this partner whose subject matches.

        mail.message has no email_to column (recipients live in partner_ids), so
        for mails whose message carries no partner we fall back on the surviving
        mail_mail rows (auto_delete mails are unlinked after send).
        """
        since = fields.Datetime.to_string(fields.Datetime.now() - timedelta(days=LOOKBACK_DAYS))
        messages = request.env['mail.message'].sudo().search([
            ('create_date', '>=', since),
            ('partner_ids', 'in', partner.ids),
        ], order='id desc', limit=50)
        if not messages:
            mails = request.env['mail.mail'].sudo().search([
                ('create_date', '>=', since),
                ('email_to', 'ilike', '%' + (partner.email_normalized or '') + '%'),
            ], order='id desc', limit=20)
            messages = mails.mail_message_id
        if not messages:
            return None
        subject = self._normalize_subject(data.get('subject', ''))
        for message in messages:
            if subject and self._normalize_subject(message.subject) == subject:
                return message
        return messages[0]

    def _resolve_target(self, partner, data):
        """(model, res_id) the tracking note belongs on, or (None, None)."""
        message = self._find_message(partner, data)
        if message and message.model and message.res_id:
            model = request.env['ir.model'].sudo().search(
                [('model', '=', message.model)], limit=1)
            if model:
                return message.model, message.res_id
        lead = request.env['crm.lead'].sudo().search(
            [('partner_id', '=', partner.id)], order='id desc', limit=1)
        if lead:
            return 'crm.lead', lead.id
        return None, None

    def _post_note(self, model_name, res_id, body):
        """Internal chatter note, authored by OdooBot so it never emails the customer."""
        try:
            record = request.env[model_name].sudo().browse(res_id)
            if not record.exists() or not hasattr(record, 'message_post'):
                return False
            odoo_bot = request.env['res.users'].sudo().search(
                [('login', '=', '__system__')], limit=1).partner_id
            record.message_post(
                # Callers build trusted HTML and escape every subject,
                # address and reason; Markup keeps Odoo 18 from escaping the
                # intended <p>/<b>/<i> formatting a second time.
                body=Markup(body),
                message_type='comment',
                subtype_xmlid='mail.mt_note',
                author_id=odoo_bot.id,
            )
            return True
        except Exception:
            _logger.exception('prema_mail_tracking: message_post failed on %s %s',
                              model_name, res_id)
            return False

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle_bounced(self, data, result, dry_run=False):
        bounce = data.get('bounce') or {}
        is_permanent = str(bounce.get('type', '')).lower() == 'permanent'
        reason = bounce.get('message') or '; '.join(
            [c for c in (bounce.get('diagnosticCode') or []) if isinstance(c, str)]) \
            or bounce.get('subType') or 'hard bounce'
        for raw_email in (data.get('to') or []):
            partner = self._find_partner(raw_email)
            if not partner:
                result['skipped'].append(raw_email)
                continue
            model_name, res_id = self._resolve_target(partner, data)
            email = html_escape(partner.email_normalized or raw_email)
            subject = html_escape(data.get('subject') or '')
            note = ('<p>⚠️ <b>Email bounced</b> — "%s" to %s was not delivered '
                    '(Resend: %s).</p>') % (subject, email, html_escape(str(reason)))
            if is_permanent:
                note += ('<p>The address was marked as bounced (count %s). '
                         'Consider verifying it with the customer.</p>') % (partner.message_bounce + 1)
            if dry_run:
                result['processed'].append({
                    'email': partner.email_normalized, 'model': model_name, 'res_id': res_id,
                    'action': 'bounce_note' + ('_flag' if is_permanent else ''),
                })
                continue
            if model_name:
                self._post_note(model_name, res_id, note)
            if is_permanent:
                partner.message_bounce += 1
            result['processed'].append(partner.email_normalized)

    def _handle_complained(self, data, result, dry_run=False):
        for raw_email in (data.get('to') or []):
            partner = self._find_partner(raw_email)
            email = partner.email_normalized if partner else email_normalize(raw_email or '')
            if not email:
                result['skipped'].append(raw_email)
                continue
            model_name = res_id = None
            if partner:
                model_name, res_id = self._resolve_target(partner, data)
            note = ('<p>⚠️ <b>Spam complaint</b> — %s marked "%s" as spam. '
                    'The address was added to the mail blacklist; Odoo will not email '
                    'them again.</p>') % (html_escape(email), html_escape(data.get('subject') or ''))
            if dry_run:
                result['processed'].append({
                    'email': email, 'model': model_name, 'res_id': res_id,
                    'action': 'complaint_note_blacklist',
                })
                continue
            if model_name:
                self._post_note(model_name, res_id, note)
            request.env['mail.blacklist'].sudo()._add(email)
            result['processed'].append(email)

    def _handle_delayed(self, data, result, dry_run=False):
        for raw_email in (data.get('to') or []):
            partner = self._find_partner(raw_email)
            if not partner:
                result['skipped'].append(raw_email)
                continue
            model_name, res_id = self._resolve_target(partner, data)
            note = ('<p>ℹ️ <b>Delivery delayed</b> — "%s" to %s is delayed. '
                    'Resend keeps retrying; no action needed.</p>') % (
                html_escape(data.get('subject') or ''),
                html_escape(partner.email_normalized or raw_email))
            if dry_run:
                result['processed'].append({
                    'email': partner.email_normalized, 'model': model_name, 'res_id': res_id,
                    'action': 'delay_note',
                })
                continue
            if model_name:
                self._post_note(model_name, res_id, note)
            result['processed'].append(partner.email_normalized)


    def _handle_opened(self, data, result, dry_run=False):
        # `email_id` (Resend's own per-mail id) is the natural dedup key, but
        # SMTP-sent mail may not carry one — fall back to subject + recipient,
        # which can only merge two distinct emails to the same recipient with
        # the same subject line (an acceptable, rare collision).
        dedup_key = data.get('email_id') or None
        if not dedup_key:
            emails = [email_normalize(e or '') or e for e in (data.get('to') or [])]
            raw_key = '%s\x00%s' % ((data.get('subject') or '').strip().lower(), emails[0])
            dedup_key = 'subj:%s' % hashlib.sha1(raw_key.encode('utf-8')).hexdigest()
            _logger.info(
                'prema_mail_tracking: email.opened without email_id; '
                'dedup by subject+recipient (%s)', emails[0])
        first_recipient = (data.get('to') or [''])[0]
        partner = None
        for raw_email in (data.get('to') or []):
            partner = self._find_partner(raw_email)
            if partner:
                break
        email = (partner.email_normalized if partner
                 else (email_normalize(first_recipient) or first_recipient))
        model_name = res_id = None
        if partner:
            model_name, res_id = self._resolve_target(partner, data)

        if dry_run:
            result['processed'].append({
                'email': email, 'model': model_name, 'res_id': res_id,
                'action': 'open_note_on_first_open',
            })
            return

        open_model = request.env['prema.resend.open'].sudo()
        existing = open_model.search([('email_id', '=', dedup_key)], limit=1)
        # The email.opened payload has no open-timestamp field — use receipt time.
        ts = fields.Datetime.now()
        is_first = not existing
        if is_first:
            try:
                with request.env.cr.savepoint():
                    open_model.create({
                        'email_id': dedup_key,
                        'partner_id': partner.id if partner else False,
                        'email_to': email,
                        'subject': data.get('subject') or '',
                        'model_name': model_name,
                        'res_id': res_id,
                        'open_count': 1,
                        'first_open': ts,
                        'last_open': ts,
                    })
            except psycopg2.IntegrityError:
                # lost the race with a concurrent open for the same mail
                is_first = False
                existing = open_model.search([('email_id', '=', dedup_key)], limit=1)
        else:
            existing.write({
                'open_count': existing.open_count + 1,
                'last_open': ts,
            })

        if is_first:
            note = ('<p>👁️ <b>Email opened</b> — "%s" was opened by %s.</p>'
                    '<p><i>Open tracking is approximate: mail clients and spam '
                    'filters can trigger opens the recipient never made, and '
                    'image-blocking clients never register one.</i></p>') % (
                html_escape(data.get('subject') or ''), html_escape(email or 'the recipient'))
            if model_name:
                self._post_note(model_name, res_id, note)
            result['processed'].append({
                'email': email, 'model': model_name, 'res_id': res_id,
                'action': 'open_note',
            })
        else:
            result['processed'].append({
                'email': email, 'model': model_name, 'res_id': res_id,
                'action': 'open_count_%s' % (existing.open_count + 1 if existing else '?'),
            })


    def _handle_sent(self, data, result, dry_run=False):
        # email.sent / email.delivered: no chatter note — their value is the
        # message-id mapping captured for every event above (deterministic
        # reply threading downstream in premafirm_ai_engine).
        result['processed'].append({'action': 'message_map_captured'})

    def _handle_delivered(self, data, result, dry_run=False):
        result['processed'].append({'action': 'message_map_captured'})


EVENT_HANDLERS = {
    'email.bounced': ResendWebhook._handle_bounced,
    'email.complained': ResendWebhook._handle_complained,
    'email.delivery_delayed': ResendWebhook._handle_delayed,
    'email.opened': ResendWebhook._handle_opened,
    'email.sent': ResendWebhook._handle_sent,
    'email.delivered': ResendWebhook._handle_delivered,
}
