# FILE: prema_ai_auditor/services/crm_operator.py
"""
CRM OPERATOR MODE — safe, CRM-scoped tool/service layer for Prema AI Chat.

SAFETY CONTRACT (do not violate):
* NEVER .sudo() CRM records here. Every function receives the caller's
  ``env`` — Odoo ACLs and record rules of the logged-in user apply
  naturally. A user can only read/write what they are allowed to.
* NEVER accept arbitrary model names, ORM commands or Python code.
  Every operation is an explicit, whitelisted function.
* NEVER hardcode database IDs. Stages resolve by stable XMLID
  (premafirm_ai_engine.crm_stage_*) with a canonical-name fallback that
  excludes folded (archived) stages.
* NEVER hard-delete CRM leads.
* Every WRITE is audited twice: a prema.ai.crm.action log row + an
  INTERNAL chatter note (mail.mt_note). Internal notes never generate
  customer email.
* Bulk / high-impact writes must not execute without explicit
  confirmation (handled by the dispatcher: ``confirm=True``).
"""
import inspect
import logging
from datetime import date, datetime, timedelta

from markupsafe import Markup

from odoo import _
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)


def _f(record, name, default=None):
    """Defensive field read.

    Prema AI Auditor is installed alongside premafirm_ai_engine in
    production (which defines fields like ``x_last_outreach_at`` and
    ``needs_reply``), but Odoo's tagged-test registries load only the
    tested module's dependency graph, where those fields do not exist.
    ``record[name]`` raises KeyError instead of AttributeError on a
    missing field, so this never crashes either way.
    """
    try:
        return record[name]
    except KeyError:
        return default

# ── canonical stage aliases (natural language → canonical name) ─────
_STAGE_ALIASES = {
    'new': 'NEW / UNCONTACTED', 'uncontacted': 'NEW / UNCONTACTED',
    'new / uncontacted': 'NEW / UNCONTACTED',
    'outreach': 'OUTREACH SENT', 'outreach sent': 'OUTREACH SENT',
    'engaged': 'ENGAGED / REPLIED', 'replied': 'ENGAGED / REPLIED',
    'engaged / replied': 'ENGAGED / REPLIED',
    'qualified': 'QUALIFIED / DATA COLLECTED',
    'data collection': 'QUALIFIED / DATA COLLECTED',
    'qualified / data collected': 'QUALIFIED / DATA COLLECTED',
    'quote requested': 'QUOTE REQUESTED',
    'quote sent': 'QUOTE SENT',
    'negotiation': 'NEGOTIATION',
    'onboarding': 'ONBOARDING', 'on board': 'ONBOARDING',
    'won': 'WON / ACTIVE CUSTOMER', 'active customer': 'WON / ACTIVE CUSTOMER',
    'won / active customer': 'WON / ACTIVE CUSTOMER',
    'lost': 'LOST', 'cancelled': 'LOST', 'cancel': 'LOST',
    'paused': 'PAUSED / ON HOLD', 'on hold': 'PAUSED / ON HOLD', 'hold': 'PAUSED / ON HOLD',
    'paused / on hold': 'PAUSED / ON HOLD',
}

# canonical name → engine xmlid (fallback: name search with fold=False)
_STAGE_XMLIDS = {
    'NEW / UNCONTACTED': 'premafirm_ai_engine.crm_stage_new_uncontacted',
    'OUTREACH SENT': 'premafirm_ai_engine.crm_stage_outreach_sent',
    'ENGAGED / REPLIED': 'premafirm_ai_engine.crm_stage_engaged_replied',
    'QUALIFIED / DATA COLLECTED': 'premafirm_ai_engine.crm_stage_qualified',
    'QUOTE REQUESTED': 'premafirm_ai_engine.crm_stage_quote_requested',
    'QUOTE SENT': 'premafirm_ai_engine.crm_stage_quote_sent',
    'NEGOTIATION': 'premafirm_ai_engine.crm_stage_negotiation',
    'ONBOARDING': 'premafirm_ai_engine.crm_stage_onboarding',
    'WON / ACTIVE CUSTOMER': 'premafirm_ai_engine.crm_stage_won_active',
    'LOST': 'premafirm_ai_engine.crm_stage_lost',
    'PAUSED / ON HOLD': 'premafirm_ai_engine.crm_stage_paused',
}

# Fields a user may pass to update_lead — validated against the whitelist.
_LEAD_UPDATE_FIELDS = {
    'name': 'name',
    'expected_revenue': 'expected_revenue',
    'probability': 'probability',
    'email_from': 'email_from',
    'phone': 'phone',
    'mobile': 'mobile',
    'contact_name': 'contact_name',
    'description': 'description',
}

# activity-type words → mail.activity.type name
_ACTIVITY_WORDS = {
    'call': 'Call', 'callback': 'Call', 'call back': 'Call',
    'follow': 'Follow-Up', 'follow up': 'Follow-Up', 'follow-up': 'Follow-Up',
    'email': 'Email', 'meeting': 'Meeting',
    'quote follow': 'Quote Follow-Up', 'quote follow-up': 'Quote Follow-Up',
    'respond': 'Respond to Customer', 'respond to customer': 'Respond to Customer',
    'onboarding check': 'Onboarding Check', 'check-in': 'Onboarding Check',
    'gather': 'Gather Requirements', 'requirements': 'Gather Requirements',
    'negotiation': 'Negotiation Follow-Up', 'negotiation follow': 'Negotiation Follow-Up',
    'reminder': 'To-Do', 'todo': 'To-Do', 'to-do': 'To-Do', 'note': 'To-Do',
}


# ═══════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════

def resolve_stage(env, key):
    """Resolve a canonical crm.stage from natural language / name.

    Never matches folded (archived) legacy stages. Returns
    (stage_recordset, canonical_name) or (False, None).
    """
    if not key:
        return env['crm.stage'], None
    norm = str(key).strip().lower()
    canonical = _STAGE_ALIASES.get(norm)
    if not canonical:
        canonical = _STAGE_ALIASES.get(norm.replace('-', ' '))
    if not canonical:
        return env['crm.stage'], None
    xmlid = _STAGE_XMLIDS.get(canonical)
    stage = env.ref(xmlid, raise_if_not_found=False) if xmlid else env['crm.stage']
    if not stage:
        stage = env['crm.stage'].search(
            [('name', '=', canonical), ('fold', '=', False)], limit=1)
    if not stage:
        raise UserError(_("Canonical stage '%s' not found in the pipeline "
                          "(is the pipeline restructure installed?)")
                        % canonical)
    return stage, canonical


def _check(env, model, access):
    """Raise AccessError with a clear message when the current user lacks
    the permission — the caller's env is never sudoed."""
    try:
        env[model].check_access_rights(access)
    except AccessError:
        raise AccessError(_("Prema AI CRM: you do not have '%s' access to "
                            "%s — permission denied.") % (access, model))


def _get_lead(env, lead_id):
    """Fetch one lead with existence + read-access validation."""
    if not lead_id:
        raise UserError(_("No lead specified."))
    lead = env['crm.lead'].browse(int(lead_id))
    if not lead.exists():
        raise UserError(_("Lead %s does not exist.") % lead_id)
    _check(env, 'crm.lead', 'read')
    try:
        lead.check_access('read')
    except AccessError:
        raise AccessError(_("Prema AI CRM: you do not have access to lead "
                            "%s (record rule).") % lead_id)
    return lead


def _audit(env, session, lead, action, old_value, new_value, request_text,
           note_body=None):
    """Audit one AI-performed change: log row + internal chatter note.

    The chatter note uses mail.mt_note (internal) — it is NEVER emailed
    to the customer.
    """
    uid = env.uid
    user_name = env.user.name
    log = env['prema.ai.crm.action'].create({
        'session_id': session.id if session else False,
        'user_id': uid,
        'lead_id': lead.id,
        'action': action,
        'old_value': old_value or '',
        'new_value': new_value or '',
        'request_text': (request_text or '')[:4000],
        'outcome': 'done',
    })
    try:
        if note_body:
            lead.message_post(
                body=note_body,
                subtype_xmlid='mail.mt_note',
                author_id=uid,
            )
    except Exception as exc:
        _logger.warning('CRM operator: chatter note failed for lead %s: %s',
                        lead.id, exc)
    return log


def _parse_deadline(value):
    """Parse a deadline into a date. Accepts YYYY-MM-DD, 'today',
    'tomorrow', 'in N days' — either alone or embedded in natural
    language ('Call him tomorrow morning'). Invalid input raises."""
    import re
    if not value:
        return date.today()
    v = str(value).strip().lower()
    m = re.search(r'\d{4}-\d{2}-\d{2}', v)
    if m:
        return datetime.strptime(m.group(0), '%Y-%m-%d').date()
    if 'tomorrow' in v:
        return date.today() + timedelta(days=1)
    if 'today' in v:
        return date.today()
    m = re.search(r'in\s+(\d+)\s+days?', v)
    if m:
        return date.today() + timedelta(days=int(m.group(1)))
    raise UserError(_("Cannot parse date '%s' — use YYYY-MM-DD, "
                      "'today', 'tomorrow' or 'in N days'.") % value)


def _resolve_activity_type(env, key):
    """Resolve a mail.activity.type by the user's word(s)."""
    if not key:
        return env.ref('mail.mail_activity_data_todo',
                       raise_if_not_found=False) or env['mail.activity.type']
    norm = str(key).strip().lower()
    # exact match first, then word map
    act = env['mail.activity.type'].search(
        [('name', '=ilike', norm)], limit=1)
    if not act:
        for word, type_name in _ACTIVITY_WORDS.items():
            if word in norm:
                act = env['mail.activity.type'].search(
                    [('name', '=', type_name)], limit=1)
                break
    if not act:
        raise UserError(_("Unknown activity type '%s' — use Call, Follow-Up, "
                          "Email, Meeting, To-Do, Quote Follow-Up, Respond to "
                          "Customer or Onboarding Check.") % key)
    return act


def _resolve_salesperson(env, name):
    """Resolve a res.users by name or login. Returns recordset or False."""
    if not name:
        return env['res.users']
    user = env['res.users'].sudo().search(
        ['|', ('name', '=ilike', name), ('login', '=ilike', name)], limit=1)
    return user


def _resolve_tag(env, name):
    """Resolve a crm.tag by name, creating it if needed (tags are safe
    configuration data; creation respects the caller's create access)."""
    if not name:
        raise UserError(_("No tag name specified."))
    tag = env['crm.tag'].search([('name', '=ilike', name)], limit=1)
    if not tag:
        env['crm.tag'].check_access_rights('create')
        tag = env['crm.tag'].create({'name': name})
    return tag


def _lead_to_dict(lead, include_meta=False):
    """Structured, JSON-safe lead payload for the AI."""
    data = {
        'id': lead.id,
        'name': lead.name,
        'company': lead.partner_name or (lead.partner_id.name if lead.partner_id else ''),
        'contact': lead.contact_name,
        'email': lead.email_from,
        'phone': lead.phone or '',
        'mobile': lead.mobile or '',
        'stage': lead.stage_id.name if lead.stage_id else None,
        'salesperson': lead.user_id.name if lead.user_id else None,
        'expected_revenue': lead.expected_revenue,
        'probability': lead.probability,
        'type': lead.type,
        'active': lead.active,
        'needs_reply': bool(_f(lead, 'needs_reply')),
        'reply_received': bool(_f(lead, 'reply_received')),
        'last_meaningful_reply_at': str(_f(lead, 'last_meaningful_reply_at')) if _f(lead, 'last_meaningful_reply_at') else None,
        'last_outreach_at': str(_f(lead, 'x_last_outreach_at')) if _f(lead, 'x_last_outreach_at') else None,
        'next_followup_at': str(_f(lead, 'next_followup_at')) if _f(lead, 'next_followup_at') else None,
        'date_open': str(lead.date_open) if lead.date_open else None,
        'write_date': str(lead.write_date) if lead.write_date else None,
        'tags': [t.name for t in lead.tag_ids],
    }
    if include_meta:
        data['partner_id'] = lead.partner_id.id if lead.partner_id else None
        data['user_id'] = lead.user_id.id if lead.user_id else None
    return data


# ═══════════════════════════════════════════════════════════════════
# READ TOOLS
# ═══════════════════════════════════════════════════════════════════

def op_search_leads(env, query, limit=25):
    """Search leads/opportunities by name, company or email."""
    _check(env, 'crm.lead', 'read')
    leads = env['crm.lead'].search(
        ['|', '|', ('name', 'ilike', query),
         ('partner_name', 'ilike', query), ('email_from', 'ilike', query)],
        limit=limit)
    return {'count': len(leads), 'leads': [_lead_to_dict(l) for l in leads]}


def op_get_lead(env, lead_id):
    """Full header of one lead."""
    lead = _get_lead(env, lead_id)
    return {'lead': _lead_to_dict(lead, include_meta=True)}


def op_get_lead_timeline(env, lead_id, limit=20):
    """Chatter timeline: messages (emails + internal notes)."""
    lead = _get_lead(env, lead_id)
    msgs = lead.message_ids.sorted(key=lambda m: m.date, reverse=True)[:limit]
    return {'lead_id': lead.id,
            'entries': [{
                'date': str(m.date) if m.date else None,
                'author': m.author_id.name if m.author_id else None,
                'type': m.message_type,
                'subtype': m.subtype_id.name if m.subtype_id else None,
                'body': (m.body or '')[:400],
                'email_from': m.email_from,
            } for m in msgs]}


def op_get_lead_emails(env, lead_id, limit=20):
    """Email messages of one lead (for conversation summaries)."""
    lead = _get_lead(env, lead_id)
    msgs = lead.message_ids.filtered(
        lambda m: m.message_type == 'email' and m.body is not False)
    msgs = msgs.sorted(key=lambda m: m.date, reverse=True)[:limit]
    return {'lead_id': lead.id, 'emails': [{
        'date': str(m.date) if m.date else None,
        'author': m.author_id.name if m.author_id else None,
        'from': m.email_from,
        'subject': m.subject,
        'body': (m.body or '')[:1000],
    } for m in msgs]}


def op_get_lead_activities(env, lead_id):
    """Open activities of one lead."""
    lead = _get_lead(env, lead_id)
    acts = lead.activity_ids
    return {'lead_id': lead.id, 'activities': [{
        'id': a.id,
        'type': a.activity_type_id.name if a.activity_type_id else None,
        'summary': a.summary,
        'date_deadline': str(a.date_deadline) if a.date_deadline else None,
        'assigned': a.user_id.name if a.user_id else None,
        'state': a.state,
    } for a in acts]}


def op_get_due_followups(env, user_id=False, days=7):
    """Leads with open activities due within N days (optionally mine)."""
    _check(env, 'crm.lead', 'read')
    today = date.today()
    horizon = today + timedelta(days=int(days))
    domain = [('activity_ids.date_deadline', '>=', str(today)),
              ('activity_ids.date_deadline', '<=', str(horizon)),
              ('activity_ids.active', '=', True)]
    if user_id:
        domain += [('activity_ids.user_id', '=', int(user_id))]
    leads = env['crm.lead'].search(domain)
    out = []
    for lead in leads:
        for act in lead.activity_ids.filtered(
                lambda a: a.active and a.date_deadline
                and today <= a.date_deadline <= horizon):
            out.append({
                'lead_id': lead.id,
                'lead': lead.name,
                'company': lead.partner_name or (lead.partner_id.name if lead.partner_id else ''),
                'activity_id': act.id,
                'activity': act.activity_type_id.name if act.activity_type_id else None,
                'summary': act.summary,
                'due': str(act.date_deadline),
                'assigned': act.user_id.name if act.user_id else None,
            })
    out.sort(key=lambda r: r['due'])
    return {'count': len(out), 'followups': out}


def op_get_overdue_activities(env, user_id=False):
    """Leads with open activities past their deadline."""
    _check(env, 'crm.lead', 'read')
    today = date.today()
    domain = [('activity_ids.date_deadline', '<', str(today)),
              ('activity_ids.active', '=', True)]
    if user_id:
        domain += [('activity_ids.user_id', '=', int(user_id))]
    leads = env['crm.lead'].search(domain)
    out = []
    for lead in leads:
        for act in lead.activity_ids.filtered(
                lambda a: a.active and a.date_deadline
                and a.date_deadline < today):
            overdue = (today - act.date_deadline).days
            out.append({
                'lead_id': lead.id,
                'lead': lead.name,
                'company': lead.partner_name or (lead.partner_id.name if lead.partner_id else ''),
                'activity_id': act.id,
                'activity': act.activity_type_id.name if act.activity_type_id else None,
                'summary': act.summary,
                'due': str(act.date_deadline),
                'overdue_days': overdue,
                'assigned': act.user_id.name if act.user_id else None,
            })
    out.sort(key=lambda r: r['overdue_days'], reverse=True)
    return {'count': len(out), 'overdue': out}


def op_get_pipeline_summary(env):
    """Per-stage lead counts for the canonical pipeline."""
    _check(env, 'crm.lead', 'read')
    stages = env['crm.stage'].search(
        [('fold', '=', False)], order='sequence')
    summary = []
    for stage in stages:
        summary.append({
            'stage': stage.name,
            'count': env['crm.lead'].search_count([
                ('stage_id', '=', stage.id), ('active', '=', True)]),
            'expected_revenue': sum(
                env['crm.lead'].search([
                    ('stage_id', '=', stage.id), ('active', '=', True)]
                ).mapped('expected_revenue')),
        })
    return {'pipeline': summary}


def op_get_email_performance(env, days=365, top=10):
    """Cold-email performance from the mail history on crm.lead.

    This install has NO open/click tracking (no mail_tracking_event rows;
    bulk-email campaigns were never run), so the only engagement signals
    are replies — Re: thread fingerprint on the message subject +
    ``x_response_status='replied'`` on the lead — and stage progression
    past outreach. Groups subject-bearing messages per lead, normalizes
    subject lines (strips Re:/Fw:/Fwd: prefixes), and ranks the threads
    by replies. Read-only.
    """
    _check(env, 'crm.lead', 'read')
    _check(env, 'mail.message', 'read')
    cutoff = str((datetime.now() - timedelta(days=days)).date())
    msgs = env['mail.message'].sudo().search_read(
        [('model', '=', 'crm.lead'), ('subject', '!=', False),
         ('subject', '!=', ''), ('create_date', '>=', cutoff)],
        ['subject', 'res_id', 'create_date'])
    if not msgs:
        return {'total_threads': 0, 'total_leads': 0, 'total_replies': 0,
                'note': ('No open/click tracking exists on this CRM — '
                         'replies + stage movement are the only engagement '
                         'signals.'),
                'top': []}

    def _norm(subject):
        s = subject.strip()
        while True:
            low = s.lower()
            if low.startswith('re:'):
                s = s[3:].strip()
            elif low.startswith('fw:') or low.startswith('fwd:'):
                s = s[s.index(':') + 1:].strip()
            else:
                break
        return ' '.join(s.lower().split())

    threads = {}
    for m in msgs:
        key = _norm(m['subject'])
        if not key:
            continue
        t = threads.setdefault(key, {
            'display': m['subject'], 'leads': set(),
            'replies': set(), 'sent_dates': [],
        })
        t['leads'].add(m['res_id'])
        t['sent_dates'].append(m['create_date'])
        if m['subject'].strip().lower().startswith('re:'):
            t['replies'].add(m['res_id'])

    # pass 2: merge truncated-subject fragments — long subjects are stored
    # truncated with an ellipsis ('…'), so 'Weekly … Mon…' and 'Weekly …
    # Montreal' are the SAME campaign. A key ending in '…' merges into a
    # longer key with the same prefix (min length guards false merges).
    _keys = list(threads)
    for k in _keys:
        if not k.endswith('…') or len(k) < 20:
            continue
        base = k[:-1]
        target = next((k2 for k2 in _keys
                       if k2 != k and len(k2) > len(base)
                       and k2.startswith(base)), None)
        if not target:
            continue
        t = threads.pop(k)
        dest = threads[target]
        dest['leads'] |= t['leads']
        dest['replies'] |= t['replies']
        dest['sent_dates'] += t['sent_dates']
        if len(dest['display']) < len(t['display']):
            dest['display'] = t['display']

    def _date_str(d):
        return d.strftime('%Y-%m-%d') if d else ''

    all_ids = sorted({lid for t in threads.values() for lid in t['leads']})
    lead_fields = ['name', 'stage_id', 'active']
    if 'x_response_status' in env['crm.lead']._fields:
        lead_fields.append('x_response_status')
    lead_info = {l['id']: l for l in env['crm.lead'].sudo().browse(
        all_ids).read(lead_fields)}
    stage_names = {}
    for l in lead_info.values():
        sid = l.get('stage_id') and l['stage_id'][0]
        if sid and sid not in stage_names:
            stage_names[sid] = env['crm.stage'].browse(sid).name

    _OUTREACH_STAGES = {'NEW / UNCONTACTED', 'OUTREACH SENT'}
    rows = []
    for t in threads.values():
        replied = set(t['replies'])
        engaged = 0
        active = 0
        for lid in t['leads']:
            info = lead_info.get(lid)
            if not info:
                continue
            if info.get('x_response_status') == 'replied':
                replied.add(lid)
            if info.get('active'):
                active += 1
            sid = info.get('stage_id') and info['stage_id'][0]
            if sid and stage_names.get(sid) not in _OUTREACH_STAGES:
                engaged += 1
        sent = len(t['leads'])
        rows.append({
            'subject': t['display'],
            'sent': sent,
            'replied': len(replied),
            'reply_rate': round(100.0 * len(replied) / sent, 1),
            'engaged': engaged,
            'active': active,
            'first_sent': _date_str(t['sent_dates'][0]),
            'last_sent': _date_str(t['sent_dates'][-1]),
            'companies': [lead_info[lid]['name'] for lid in sorted(replied)
                          if lead_info.get(lid) and lead_info[lid].get('name')],
        })

    rows.sort(key=lambda r: (r['replied'], r['reply_rate']), reverse=True)
    return {
        'total_threads': len(rows),
        'total_leads': sum(r['sent'] for r in rows),
        'total_replies': sum(r['replied'] for r in rows),
        'note': ('No open/click tracking exists on this CRM — replies + '
                 'stage movement are the only engagement signals.'),
        'top': rows[:top],
    }


def op_get_stage_leads(env, stage_name, limit=50):
    """Leads currently in a canonical stage."""
    _check(env, 'crm.lead', 'read')
    stage, canonical = resolve_stage(env, stage_name)
    if not stage:
        raise UserError(_("Unknown stage '%s' — I know: %s.")
                        % (stage_name, ', '.join(sorted(_STAGE_ALIASES))))
    leads = env['crm.lead'].search(
        [('stage_id', '=', stage.id), ('active', '=', True)], limit=limit)
    return {'stage': canonical, 'count': len(leads),
            'leads': [_lead_to_dict(l) for l in leads]}


def op_search_contacts(env, query, limit=25):
    """Search partners (companies + people)."""
    _check(env, 'res.partner', 'read')
    partners = env['res.partner'].sudo().search(
        ['|', ('name', 'ilike', query), ('email', 'ilike', query)],
        limit=limit)
    return {'count': len(partners), 'contacts': [{
        'id': p.id,
        'name': p.name,
        'email': p.email,
        'phone': p.phone or '',
        'mobile': p.mobile or '',
        'company': p.parent_id.name if p.parent_id else '',
        'is_company': p.is_company,
    } for p in partners]}


def op_summarize_lead(env, lead_id):
    """Compact conversational summary of one lead."""
    lead = _get_lead(env, lead_id)
    msgs = lead.message_ids.sorted(key=lambda m: m.date, reverse=True)
    emails = msgs.filtered(lambda m: m.message_type == 'email')
    last_msg = msgs[:1]
    latest = None
    if last_msg:
        m = last_msg[0]
        latest = {'date': str(m.date) if m.date else None,
                  'author': m.author_id.name if m.author_id else None,
                  'type': m.message_type,
                  'body': (m.body or '')[:300]}
    return {
        'lead': _lead_to_dict(lead, include_meta=True),
        'open_activities': len(lead.activity_ids),
        'message_count': len(msgs),
        'email_count': len(emails),
        'last_inbound_email': {
            'date': str(emails[0].date) if emails else None,
            'from': emails[0].email_from if emails else None,
            'subject': emails[0].subject if emails else None,
        } if emails else None,
        'latest_message': latest,
    }


# ═══════════════════════════════════════════════════════════════════
# WRITE TOOLS  (all audit + internal note; never sudoed)
# ═══════════════════════════════════════════════════════════════════

def op_move_lead_stage(env, lead_id, stage_name, session=False,
                       request_text='', note=''):
    """Move one lead to a canonical stage. Refuses WON / LOST (use
    mark_won / mark_lost) and folded legacy stages."""
    lead = _get_lead(env, lead_id)
    stage, canonical = resolve_stage(env, stage_name)
    if not stage:
        raise UserError(_("Unknown stage '%s'.") % stage_name)
    if canonical in ('WON / ACTIVE CUSTOMER', 'LOST'):
        raise UserError(_("Use 'Mark Won' / 'Mark Lost' for the %s stage — "
                          "it sets the closing fields too.") % canonical)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    old = lead.stage_id.name if lead.stage_id else None
    if old == canonical:
        return {'status': 'no_change', 'lead_id': lead.id,
                'message': 'already in %s' % canonical}
    lead.write({'stage_id': stage.id})
    body = (Markup("Prema AI moved this opportunity from <b>%s</b> to "
                   "<b>%s</b> at %s's request.")
            % (old or '?', canonical, env.user.name))
    if note:
        body += Markup('<br/>Context: %s') % note
    _audit(env, session, lead, 'move_lead_stage', old, canonical,
           request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name,
            'from': old, 'to': canonical}


def op_create_activity(env, lead_id, activity_word='', summary='',
                       note='', date_deadline=False, user_name=False,
                       session=False, request_text=''):
    """Create a mail.activity linked to a crm.lead."""
    lead = _get_lead(env, lead_id)
    activity_type = _resolve_activity_type(env, activity_word or summary)
    _check(env, 'mail.activity', 'create')
    deadline = _parse_deadline(date_deadline)
    assignee = _resolve_salesperson(env, user_name) if user_name else lead.user_id
    if not assignee:
        assignee = env.user
    act = lead.activity_schedule(
        activity_type_id=activity_type.id,
        summary=summary or activity_type.name,
        note=note or None,
        date_deadline=deadline,
        user_id=assignee.id or env.uid,
    )
    body = (Markup("Prema AI created a <b>%s</b> activity (%s, due %s, "
                   "assigned %s) at %s's request.")
            % (activity_type.name, summary or 'no summary', deadline,
               assignee.name, env.user.name))
    _audit(env, session, lead, 'create_activity',
           '', '%s | due %s | %s' % (activity_type.name, deadline,
                                     summary or ''), request_text,
           note_body=body)
    return {'status': 'done', 'activity_id': act.id, 'lead_id': lead.id,
            'lead': lead.name, 'type': activity_type.name,
            'due': str(deadline), 'assigned': assignee.name}


def op_complete_activity(env, activity_id, feedback='', session=False,
                         request_text=''):
    """Complete an open activity (optionally with feedback)."""
    if not activity_id:
        raise UserError(_("No activity specified."))
    act = env['mail.activity'].browse(int(activity_id))
    if not act.exists():
        raise UserError(_("Activity %s does not exist.") % activity_id)
    _check(env, 'mail.activity', 'write')
    act.check_access('write')
    lead = act.res_id and env['crm.lead'].browse(act.res_id)
    act.action_feedback(feedback=feedback or None)
    body = (Markup("Prema AI completed the <b>%s</b> activity%s at %s's "
                   "request.")
            % (act.activity_type_id.name if act.activity_type_id else '?',
               (' (%s)' % feedback) if feedback else '', env.user.name))
    _audit(env, session, lead, 'complete_activity',
           'open', 'done', request_text, note_body=body)
    return {'status': 'done', 'activity_id': act.id,
            'lead_id': lead.id if lead else None}


def op_update_lead(env, lead_id, vals, session=False, request_text=''):
    """Update whitelisted lead fields only."""
    lead = _get_lead(env, lead_id)
    safe = {}
    for k, v in (vals or {}).items():
        field = _LEAD_UPDATE_FIELDS.get(str(k).lower())
        if not field:
            raise UserError(_("Field '%s' is not allowed for AI updates. "
                              "Allowed: %s")
                            % (k, ', '.join(sorted(_LEAD_UPDATE_FIELDS))))
        safe[field] = v
    if not safe:
        raise UserError(_("No updatable fields given."))
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    old = {f: getattr(lead, f) for f in safe}
    lead.write(safe)
    changed = ', '.join('%s: %s → %s' % (f, old[f], safe[f]) for f in safe)
    body = (Markup("Prema AI updated this opportunity (%s) at %s's request.")
            % (changed, env.user.name))
    _audit(env, session, lead, 'update_lead', old, safe, request_text,
           note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'changed': changed}


def op_assign_salesperson(env, lead_id, user_name, session=False,
                          request_text=''):
    """Assign a lead to a salesperson."""
    lead = _get_lead(env, lead_id)
    user = _resolve_salesperson(env, user_name)
    if not user:
        raise UserError(_("User '%s' not found.") % user_name)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    old = lead.user_id.name if lead.user_id else None
    lead.write({'user_id': user.id})
    body = (Markup("Prema AI assigned this opportunity from %s to <b>%s</b> "
                   "at %s's request.")
            % (old or 'unassigned', user.name, env.user.name))
    _audit(env, session, lead, 'assign_salesperson', old or '',
           user.name, request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name,
            'from': old or 'unassigned', 'to': user.name}


def op_add_internal_note(env, lead_id, body, session=False,
                         request_text=''):
    """Add an internal chatter note (never emailed)."""
    lead = _get_lead(env, lead_id)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    note_body = (Markup('%s<br/><i>— added via Prema AI at %s\'s request</i>')
                 % (body, env.user.name))
    lead.message_post(body=note_body, subtype_xmlid='mail.mt_note')
    _audit(env, session, lead, 'add_internal_note', '', body[:4000],
           request_text, note_body=False)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name}


def op_add_tag(env, lead_id, tag_name, session=False, request_text=''):
    """Add a tag to a lead (creates the tag if missing)."""
    lead = _get_lead(env, lead_id)
    tag = _resolve_tag(env, tag_name)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    had = tag in lead.tag_ids
    if not had:
        lead.write({'tag_ids': [(4, tag.id)]})
    body = (Markup("Prema AI added tag <b>%s</b> to this opportunity at "
                   "%s's request.") % (tag.name, env.user.name))
    _audit(env, session, lead, 'add_tag',
           '' if had else 'no', tag.name, request_text, note_body=body)
    return {'status': 'done' if not had else 'no_change',
            'lead_id': lead.id, 'tag': tag.name}


def op_remove_tag(env, lead_id, tag_name, session=False, request_text=''):
    """Remove a tag from a lead."""
    lead = _get_lead(env, lead_id)
    tag = env['crm.tag'].search([('name', '=ilike', tag_name)], limit=1)
    if not tag or tag not in lead.tag_ids:
        return {'status': 'no_change', 'lead_id': lead.id,
                'message': 'lead does not have tag %r' % tag_name}
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    lead.write({'tag_ids': [(3, tag.id)]})
    body = (Markup("Prema AI removed tag <b>%s</b> from this opportunity at "
                   "%s's request.") % (tag.name, env.user.name))
    _audit(env, session, lead, 'remove_tag', tag.name, '',
           request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'tag': tag.name}


def op_mark_won(env, lead_id, session=False, request_text=''):
    """Mark one opportunity Won (native action_set_won)."""
    lead = _get_lead(env, lead_id)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    old = lead.stage_id.name if lead.stage_id else None
    lead.action_set_won()
    body = (Markup("Prema AI marked this opportunity as <b>WON / ACTIVE "
                   "CUSTOMER</b> (from %s) at %s's request.")
            % (old or '?', env.user.name))
    _audit(env, session, lead, 'mark_won', old or '',
           'WON / ACTIVE CUSTOMER', request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name,
            'from': old, 'to': 'WON / ACTIVE CUSTOMER'}


def op_mark_lost(env, lead_id, reason='', session=False, request_text=''):
    """Mark one opportunity Lost (native action_set_lost)."""
    lead = _get_lead(env, lead_id)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    old = lead.stage_id.name if lead.stage_id else None
    # action_set_lost writes its kwargs to the lead — only pass a real
    # crm.lost.reason record (the free-text reason stays in the audit)
    reason_rec = env['crm.lost.reason'].search(
        [('name', '=ilike', reason)], limit=1) if reason else False
    try:
        lead.action_set_lost(lost_reason=reason_rec.id if reason_rec else False)
    except (TypeError, UserError, ValueError):
        # native signature variants — fall back to a plain stage move
        stage, canonical = resolve_stage(env, 'lost')
        lead.write({'stage_id': stage.id, 'probability': 0})
    body = (Markup("Prema AI marked this opportunity as <b>LOST</b> (from "
                   "%s%s) at %s's request.")
            % (old or '?', ' — %s' % reason if reason else '',
               env.user.name))
    _audit(env, session, lead, 'mark_lost', old or '', 'LOST',
           request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name,
            'from': old, 'to': 'LOST'}


def op_pause_lead(env, lead_id, session=False, request_text=''):
    """Pause a lead: move to PAUSED / ON HOLD, remembering the previous
    stage in the audit log so resume can restore it."""
    lead = _get_lead(env, lead_id)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    old = lead.stage_id.name if lead.stage_id else None
    if old == 'PAUSED / ON HOLD':
        return {'status': 'no_change', 'lead_id': lead.id,
                'message': 'already paused'}
    stage, canonical = resolve_stage(env, 'paused')
    lead.write({'stage_id': stage.id})
    body = (Markup("Prema AI paused this opportunity (moved from <b>%s</b> "
                   "to <b>PAUSED / ON HOLD</b>) at %s's request.")
            % (old or '?', env.user.name))
    _audit(env, session, lead, 'pause_lead', old or '',
           'PAUSED / ON HOLD', request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name,
            'from': old, 'to': 'PAUSED / ON HOLD'}


def op_resume_lead(env, lead_id, session=False, request_text=''):
    """Resume a paused lead: restore the stage it had before pausing
    (from the audit trail), defaulting to OUTREACH SENT."""
    lead = _get_lead(env, lead_id)
    _check(env, 'crm.lead', 'write')
    lead.check_access('write')
    if lead.stage_id.name != 'PAUSED / ON HOLD':
        return {'status': 'no_change', 'lead_id': lead.id,
                'message': 'lead is not paused'}
    prev = env['prema.ai.crm.action'].search([
        ('lead_id', '=', lead.id),
        ('action', '=', 'pause_lead'),
    ], order='create_date desc, id desc', limit=1)
    target = resolve_stage(env, 'outreach sent')[0]
    if prev and prev.old_value:
        stage, canonical = resolve_stage(env, prev.old_value)
        if stage:
            target = stage
    lead.write({'stage_id': target.id})
    body = (Markup("Prema AI resumed this opportunity (moved from "
                   "<b>PAUSED / ON HOLD</b> to <b>%s</b>) at %s's request.")
            % (target.name, env.user.name))
    _audit(env, session, lead, 'resume_lead', 'PAUSED / ON HOLD',
           target.name, request_text, note_body=body)
    return {'status': 'done', 'lead_id': lead.id, 'lead': lead.name,
            'from': 'PAUSED / ON HOLD', 'to': target.name}


# ═══════════════════════════════════════════════════════════════════
# TODAY'S CALL LIST  (priority-ordered)
# ═══════════════════════════════════════════════════════════════════

def op_get_call_list(env, user_id=False, limit=20):
    """The prioritized call list for today.

    Priority buckets (higher first):
      1. overdue Call activities
      2. Call activities due today
      3. customer replied — we owe a response (needs_reply)
      4. promised callbacks due today (Call activity w/ 'callback' summary)
      5. quotation follow-ups due (Quote Follow-Up activity)
      6. onboarding check-ins due (Onboarding Check activity)
      7. high-priority stale opportunities (7+ days silent, early stage)
    Returns Company / Contact / Phone / Stage / Reason / Last Contact /
    Next Activity / Priority — never a bare dump of every lead.
    """
    _check(env, 'crm.lead', 'read')
    today = date.today()
    call_type = env['mail.activity.type'].search([('name', '=', 'Call')],
                                                 limit=1)
    quote_type = env['mail.activity.type'].search(
        [('name', '=', 'Quote Follow-Up')], limit=1)
    onb_type = env['mail.activity.type'].search(
        [('name', '=', 'Onboarding Check')], limit=1)

    rows = {}
    uid_leaf = [('user_id', '=', int(user_id))] if user_id else []

    def _add(lead, score, reason, next_activity):
        key = lead.id
        if key not in rows or score > rows[key]['score']:
            phone = lead.phone or lead.mobile or ''
            if not phone and lead.partner_id:
                phone = lead.partner_id.phone or lead.partner_id.mobile or ''
            rows[key] = {
                'score': score,
                'lead_id': lead.id,
                'company': lead.partner_name or (lead.partner_id.name if lead.partner_id else '') or lead.name,
                'contact': lead.contact_name,
                'phone': phone or '',
                'stage': lead.stage_id.name if lead.stage_id else None,
                'reason': reason,
                'last_contact': str(_f(lead, 'x_last_outreach_at')
                                    or _f(lead, 'last_meaningful_reply_at')
                                    or lead.write_date)[:10],
                'next_activity': next_activity,
            }

    # 1+2+4 — Call activities (overdue / due today / callbacks)
    if call_type:
        open_calls = env['mail.activity'].search(
            [('res_model', '=', 'crm.lead'),
             ('activity_type_id', '=', call_type.id),
             ('active', '=', True),
             ('date_deadline', '<=', str(today))] + uid_leaf)
        for act in open_calls:
            lead = env['crm.lead'].browse(act.res_id)
            if not lead.exists():
                continue
            overdue = (today - act.date_deadline).days
            reason = 'overdue call (%d days)' % overdue
            if 'callback' in (act.summary or '').lower():
                reason += ' — promised callback'
            score = 100 + overdue
            _add(lead, score, reason,
                 '%s due %s' % (act.summary or 'Call',
                                str(act.date_deadline)[:10]))

    # 3 — replied, we owe a response (engine-only field: skip the leaf
    # in registries where the field is absent)
    replied = env['crm.lead'].search(
        [('active', '=', True)] + uid_leaf
        + ([('needs_reply', '=', True)]
            if 'needs_reply' in env['crm.lead']._fields else []))
    for lead in replied:
        _add(lead, 70, 'customer replied — we owe a response',
             'Reply / Respond to Customer')

    # 5 — quote follow-ups due
    if quote_type:
        q_acts = env['mail.activity'].search(
            [('res_model', '=', 'crm.lead'),
             ('activity_type_id', '=', quote_type.id),
             ('active', '=', True),
             ('date_deadline', '<=', str(today))] + uid_leaf)
        for act in q_acts:
            lead = env['crm.lead'].browse(act.res_id)
            if lead.exists():
                _add(lead, 60, 'quote follow-up due',
                     '%s due %s' % (act.summary or 'Quote Follow-Up',
                                    str(act.date_deadline)[:10]))

    # 6 — onboarding check-ins due
    if onb_type:
        o_acts = env['mail.activity'].search(
            [('res_model', '=', 'crm.lead'),
             ('activity_type_id', '=', onb_type.id),
             ('active', '=', True),
             ('date_deadline', '<=', str(today))] + uid_leaf)
        for act in o_acts:
            lead = env['crm.lead'].browse(act.res_id)
            if lead.exists():
                _add(lead, 50, 'onboarding check-in due',
                     '%s due %s' % (act.summary or 'Onboarding Check',
                                    str(act.date_deadline)[:10]))

    # 7 — high-priority stale opportunities (early-stage, 7+ days silent)
    cutoff = datetime.now() - timedelta(days=7)
    stale = env['crm.lead'].search(
        [('active', '=', True),
         ('stage_id.name', 'in',
          ['NEW / UNCONTACTED', 'OUTREACH SENT', 'QUALIFIED / DATA COLLECTED']),
         ('write_date', '<', str(cutoff)),
         ('expected_revenue', '>=', 10000)] + uid_leaf,
        order='expected_revenue desc', limit=15)
    for lead in stale:
        silent = (datetime.now() - (lead.write_date or datetime.now())).days
        _add(lead, 30 + min(silent, 20),
             'high-priority stale (%d days silent)' % silent,
             'Follow-Up')

    ranked = sorted(rows.values(), key=lambda r: r['score'], reverse=True)
    for r in ranked:
        score = r.pop('score', 0)
        r['priority'] = 'HIGH' if score >= 70 else (
            'MEDIUM' if score >= 50 else 'LOW')
    return {'count': len(ranked), 'date': str(today),
            'call_list': ranked[:limit]}


# ═══════════════════════════════════════════════════════════════════
# DISPATCHER
# ═══════════════════════════════════════════════════════════════════

_READ_OPS = {
    'search_leads', 'get_lead', 'get_lead_timeline', 'get_lead_emails',
    'get_lead_activities', 'get_due_followups', 'get_overdue_activities',
    'get_call_list', 'get_pipeline_summary', 'get_stage_leads',
    'get_email_performance', 'search_contacts', 'summarize_lead',
}

_WRITE_OPS = {
    'move_lead_stage', 'create_activity', 'complete_activity',
    'update_lead', 'assign_salesperson', 'add_internal_note', 'add_tag',
    'remove_tag', 'mark_won', 'mark_lost', 'pause_lead', 'resume_lead',
}

def dispatch(env, intent, params, session=False):
    """Run one operator intent.

    Returns {'kind': 'read'|'write'|'preview', 'result': {...}}.
    'preview' means: changes are proposed but NOT applied — the caller
    must re-invoke with params['confirm']=True to execute.
    """
    if intent not in _READ_OPS and intent not in _WRITE_OPS:
        raise UserError(_("Unknown CRM operator intent '%s'.") % intent)

    params = dict(params)  # never mutate the caller's dict (confirm flow
    # re-invokes dispatch with the same params object)
    is_write = intent in _WRITE_OPS

    if intent in _READ_OPS:
        fn = globals()['op_' + intent]
        # the LLM router may emit extra keys (e.g. activity_word for a
        # call list) — pass only kwargs the op accepts
        p = dict(params)
        if 'lead_ids' in p and 'lead_id' not in p and len(p['lead_ids']) == 1:
            p['lead_id'] = p['lead_ids'][0]
        p.pop('lead_ids', None)
        accepted = {k: v for k, v in p.items()
                    if k in inspect.signature(fn).parameters}
        return {'kind': 'read', 'result': fn(env, **accepted)}

    # ── write path ────────────────────────────────────────────────────
    # resolve target leads first (bulk safety)
    lead_ids = params.pop('lead_ids', None) or params.pop('lead_id', None)
    if not lead_ids:
        raise UserError(_("No target lead for action '%s'.") % intent)
    if isinstance(lead_ids, int):
        lead_ids = [lead_ids]
    lead_ids = [int(x) for x in lead_ids]

    bulk = len(lead_ids) > 1
    confirm = bool(params.pop('confirm', False))

    # EVERY write previews first — nothing applies without an explicit
    # 'confirm' from the user (2026-08-31 decision). The preview payload is
    # stored on the session; the confirm flow re-invokes dispatch with
    # params['confirm']=True, which executes the write below.
    if not confirm:
        leads = env['crm.lead'].browse(lead_ids).exists()
        preview = {
            'intent': intent,
            'lead_count': len(leads),
            'leads': [{'id': l.id, 'name': l.name,
                       'stage': l.stage_id.name if l.stage_id else None}
                      for l in leads],
            'params': dict(params, lead_ids=lead_ids),
            'message': 'This would apply to %d lead(s) — reply '
                       '"confirm" to execute.' % len(leads),
        }
        return {'kind': 'preview', 'result': preview}

    results = []
    for lead_id in lead_ids:
        fn = globals()['op_' + intent]
        p = dict(params, lead_id=lead_id)
        if intent == 'add_internal_note' and 'note' in p and 'body' not in p:
            # router/fallback emit 'note'; the op takes 'body' — map BEFORE
            # the signature filter or the 'note' key is stripped first
            p['body'] = p.pop('note')
        p = {k: v for k, v in p.items()
             if k in inspect.signature(fn).parameters}
        results.append(fn(env, session=session, **p))
    if len(results) == 1:
        return {'kind': 'write', 'result': results[0]}
    return {'kind': 'write', 'result': {'status': 'done',
                                        'lead_count': len(results),
                                        'results': results}}
