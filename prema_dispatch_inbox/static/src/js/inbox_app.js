/** @odoo-module **/
// Prema Dispatch Inbox — single OWL client action (design §4).
// C1 folders · C2 conversation list · C3 conversation · C4 AI assistant.
// Below 900px the columns stack: list → conversation → AI panel; between
// 900px and 1199px the AI panel becomes a slide-over drawer.
//
// Every visible control in this app maps to ONE backend RPC (the wiring
// matrix in the manual / audit report):
//   folders / list / detail  → inbox_folders / inbox_conversations /
//                              inbox_conversation_detail
//   category / state selects → write (t-model bound, then persisted)
//   assign dropdown          → inbox_assign_candidates / action_assign
//   mute                     → action_toggle_mute
//   link picker (Dropdown)   → inbox_link_candidates / action_link_record /
//                              action_unlink_record
//   composer (all modes)     → prema.inbox.conversation.compose_and_send
//                              (ONE method — reply, reply-all, forward,
//                              new email, internal note, draft resume)
//   draft discard / retry    → discard_draft / retry_send
//   AI panel                 → inbox_ai_action / inbox_calculate_price
//   attachments              → /prema_inbox/attachment/<id>/<name> route
//   badge                    → /prema_inbox/unread_counts (inbox_badge.js)

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

const CATEGORY_LABELS = {
    quote_request: "Quote Request",
    load_opportunity: "Load Opportunity",
    active_shipment: "Active Shipment",
    waiting_reply: "Waiting for Reply",
    invoice_question: "Invoice Question",
    carrier_alert: "Carrier Alert",
    needs_review: "Needs Review",
    other: "Other",
};
const STATE_LABELS = {
    open: "Open", waiting: "Waiting",
    completed: "Completed", archived: "Archived",
};
const PRIORITY_LABELS = {
    normal: "Normal", urgent: "Urgent", emergency: "Emergency",
};
const LINK_MODELS = {
    booking: "logistics.booking",
    job: "prema.dispatch.job",
    invoice: "account.move",
    opportunity: "crm.lead",
};
const LINK_LABELS = {
    booking: "Booking", job: "Job",
    invoice: "Invoice", opportunity: "Opportunity",
};
const OUTBOUND_LABELS = {
    sent: "Sent",
    pending: "Queued for delivery",
    failed: "Send failed",
    intercepted: "Intercepted (never sent)",
    draft: "Draft",
};

export class InboxApp extends Component {
    static template = "prema_dispatch_inbox.InboxApp";
    static props = { ...standardActionServiceProps };
    static components = { Dropdown };

    setup() {
        this.orm = useService("orm");
        this.busService = useService("bus_service");
        this.notification = useService("notification");
        this.action = useService("action");
        this.state = useState({
            loading: true,
            folders: [],
            folder: "inbox",
            conversations: [],
            selectedId: null,
            detail: null,
            search: "",
            loadError: null,
            composer: {
                mode: null, body: "", to: "", cc: "", subject: "",
                attachments: [], draftId: null, sending: false,
            },
            ai: { busy: false, panelOpen: true, conflictsOpen: false,
                  editingKey: null, editValue: "",
                  adjInput: "", adjReason: "" },
            linkCandidates: null,   // {model, records, manual}
            linkSearch: "",
            assignCandidates: null,
            plainMsg: null,     // per-message "view plain text" opt-out
            mobileScreen: "list",   // list | conversation | ai (mobile stack)
        });
        // Odoo 18: env.userId does NOT exist (Odoo 16 legacy) — the uid
        // lives on the @web/core/user module export. A wrong/undefined uid
        // silently subscribes to "prema_inbox:undefined" while the server
        // notifies "prema_inbox:{uid}" — the relay matches zero websockets.
        this.channel = `prema_inbox:${user.userId}`;
        // Class-name identifiers do NOT resolve in OWL 2 template scope —
        // templates must use `this.` instead.
        this.CATEGORY_LABELS = InboxApp.CATEGORY_LABELS;
        this.CATEGORY_OPTIONS = InboxApp.CATEGORY_OPTIONS;
        this.STATE_OPTIONS = InboxApp.STATE_OPTIONS;
        this.OUTBOUND_LABELS = InboxApp.OUTBOUND_LABELS;
        this.userId = user.userId;
        this._timer = null;
        this._searchTimer = null;
        this._linkTimer = null;
        this._convsLoading = false;   // overlap guard: one list load at a time
        this._convsQueued = false;    // coalesce: refresh once the current one ends
        this._reconcileRunning = false;
        this._reconcileQueued = false;
        // Seed the picker on EVERY open: state.linkCandidates starts null
        // and is only set by searchLinks (whose tab buttons live INSIDE the
        // t-if on linkCandidates) — without this the Link picker opened as
        // an empty panel and could never be used (stuck state).
        this.linkDropdown = useDropdownState({
            onOpen: () => this.searchLinks(
                this.state.linkCandidates?.model || "booking"),
        });
        this.assignDropdown = useDropdownState();
        // Arrow closure: bus_service.subscribe wraps the callback in a plain
        // function call, so a prototype method would lose `this`.
        this._premaEventCb = (payload) => {
            this._onEvent(payload);
        };
        onMounted(async () => {
            await this.refreshFolders();
            await this.loadConversations();
            this._timer = setInterval(() => this.reconcile(), 60000);
            try {
                // A bus failure (websocket down) must never block the basic
                // inbox rendering or the reconcile timer — live updates are
                // a bonus, the mailbox itself is not.
                await this.busService.addChannel(this.channel);
                // Odoo 18 delivery model: bus._sendone(channel, notif_type,
                // payload) arrives on the client's notificationBus keyed by
                // notif_type — the channel name only routes it to the right
                // websocket. So: addChannel("prema_inbox:{uid}") gates who
                // receives, subscribe("prema_inbox") is the listener key.
                this.busService.subscribe("prema_inbox", this._premaEventCb);
            } catch (e) {
                console.error("bus subscribe failed — live updates disabled:", e);
            }
        });
        onWillUnmount(() => {
            clearInterval(this._timer);
            clearTimeout(this._searchTimer);
            clearTimeout(this._linkTimer);
            this._timer = null;
            try {
                this.busService.deleteChannel(this.channel);
                this.busService.unsubscribe("prema_inbox", this._premaEventCb);
            } catch (e) {
                // channel may never have been added — nothing to clean
            }
        });
    }

    // ------------------------------------------------------------------
    // data loading
    // ------------------------------------------------------------------
    async refreshFolders() {
        try {
            this.state.folders = await this.orm.call(
                "prema.inbox.conversation", "inbox_folders", []);
        } catch (e) {
            console.error("folders failed:", e);
            if (!this.state.folders.length) {
                this.state.loadError =
                    "Could not load the inbox — check your connection and retry.";
            }
        }
    }

    async loadConversations() {
        // Overlap guard + coalesce: a burst of triggers (events, folder
        // switches) never stacks parallel requests.
        if (this._convsLoading) {
            this._convsQueued = true;
            return;
        }
        this._convsLoading = true;
        this.state.loading = true;
        try {
            this.state.conversations = await this.orm.call(
                "prema.inbox.conversation", "inbox_conversations",
                [this.state.folder, this.state.search || null]);
            this.state.loadError = null;
        } catch (e) {
            console.error("conversations failed:", e);
            this.state.loadError =
                "Could not load conversations — check your connection and retry.";
        } finally {
            this.state.loading = false;
            this._convsLoading = false;
            if (this._convsQueued) {
                this._convsQueued = false;
                this.loadConversations();
            }
        }
    }

    async openConversation(id) {
        this.state.selectedId = id;
        this.state.detail = null;
        if (window.innerWidth < 900) {
            this.state.mobileScreen = "conversation";
        }
        try {
            const detail = await this.orm.call(
                "prema.inbox.conversation", "inbox_conversation_detail", [id]);
            if (this.state.selectedId !== id) {
                return; // user moved on — ignore the stale response
            }
            if (!detail || !detail.conversation) {
                // The conversation vanished (deleted/merged) — never crash.
                this.state.selectedId = null;
                this.state.detail = null;
                this.notification.add("Conversation no longer exists.", {
                    type: "warning",
                });
                this.loadConversations();
                return;
            }
            this.state.detail = detail;
            this.loadAssignCandidates();
            const msgs = detail.messages
                .filter((m) => m.direction === "incoming" && !m.is_read)
                .map((m) => m.id);
            if (msgs.length) {
                await this.orm.call(
                    "prema.inbox.message", "mark_read", [msgs]);
                await this.reconcile();
            }
        } catch (e) {
            console.error("detail failed:", e);
        }
    }

    async reconcile() {
        if (this._reconcileRunning) {
            this._reconcileQueued = true;
            return;
        }
        this._reconcileRunning = true;
        try {
            await this.refreshFolders();
            if (this.state.selectedId) {
                const detail = await this.orm.call(
                    "prema.inbox.conversation", "inbox_conversation_detail",
                    [this.state.selectedId]).catch(() => null);
                if (detail && this.state.selectedId === detail.conversation?.id) {
                    this.state.detail = detail;
                }
            }
        } finally {
            this._reconcileRunning = false;
            if (this._reconcileQueued) {
                this._reconcileQueued = false;
                this.reconcile();
            }
        }
    }

    _onEvent(payload) {
        if (!payload) {
            return;
        }
        if (payload.type === "new_message") {
            this.refreshFolders();
            this.loadConversations();
        } else {
            this.reconcile();
        }
    }

    // ------------------------------------------------------------------
    // folder / list actions
    // ------------------------------------------------------------------
    selectFolder(key) {
        this.state.folder = key;
        this.state.selectedId = null;
        this.state.detail = null;
        this.loadConversations();
    }

    onSearch() {
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.loadConversations(), 250);
    }

    retryLoad() {
        this.state.loadError = null;
        this.refreshFolders();
        this.loadConversations();
    }

    async markUnread(id) {
        const detail = await this.orm.call(
            "prema.inbox.conversation", "inbox_conversation_detail", [id]);
        const incoming = detail.messages
            .filter((m) => m.direction === "incoming").map((m) => m.id);
        if (incoming.length) {
            await this.orm.call("prema.inbox.message", "mark_unread", [incoming]);
        }
        this.reconcile();
    }

    // ------------------------------------------------------------------
    // thread header: category / state / mute / assignment
    // ------------------------------------------------------------------
    async setCategory(category) {
        if (!this.state.detail) {
            return;
        }
        await this.orm.call(
            "prema.inbox.conversation", "write",
            [[this.state.selectedId], { category }]);
        this.reconcile();
    }

    async setState(workflowState) {
        if (!this.state.detail) {
            return;
        }
        await this.orm.call(
            "prema.inbox.conversation", "write",
            [[this.state.selectedId], { workflow_state: workflowState }]);
        this.reconcile();
    }

    async toggleMute() {
        await this.orm.call(
            "prema.inbox.conversation", "action_toggle_mute",
            [this.state.selectedId]);
        this.reconcile();
    }

    async loadAssignCandidates() {
        // Group-filtered candidates — never the whole directory.
        try {
            this.state.assignCandidates = await this.orm.call(
                "prema.inbox.conversation", "inbox_assign_candidates", []);
        } catch (e) {
            console.error("assign candidates failed:", e);
            this.state.assignCandidates = [];
        }
    }

    async doAssign(userId) {
        try {
            await this.orm.call(
                "prema.inbox.conversation", "action_assign",
                [this.state.selectedId, userId]);
        } catch (e) {
            this.notification.add(this._rpcError(e, "Could not assign."), {
                type: "danger",
            });
        }
        this.assignDropdown.close();
        // reconcile() refreshes folders + detail (the thread header) but NOT
        // the conversation list — the assignee chip on the row would go
        // stale until the next folder switch / bus event. Reload it now.
        this.loadConversations();
        this.reconcile();
    }

    async doUnassign() {
        try {
            await this.orm.call(
                "prema.inbox.conversation", "action_assign",
                [this.state.selectedId, false]);
        } catch (e) {
            this.notification.add(this._rpcError(e, "Could not unassign."), {
                type: "danger",
            });
        }
        this.assignDropdown.close();
        this.loadConversations();
        this.reconcile();
    }

    // ------------------------------------------------------------------
    // composer (C3) — ONE RPC: prema.inbox.conversation.compose_and_send
    // ------------------------------------------------------------------
    startComposer(mode, opts = {}) {
        const detail = this.state.detail;
        const conv = detail?.conversation;
        const defaults = detail?.reply_defaults || { to: [], subject: "" };
        const toEmails = (arr) => (arr || []).map((p) => p.email).join(", ");
        let composer = {
            mode, body: "", to: "", cc: "", subject: "",
            attachments: [], draftId: null, sending: false,
        };
        if (mode === "reply" || mode === "reply_all") {
            // reply_all carries its OWN to/cc defaults (sender + external
            // To/cc) — reading reply_defaults.to here would silently drop
            // the external To recipients of the incoming email.
            const replyDefaults = mode === "reply_all"
                ? (detail?.reply_all_defaults || defaults) : defaults;
            composer.to = toEmails(replyDefaults.to);
            composer.cc = mode === "reply_all"
                ? toEmails(detail.reply_all_defaults?.cc) : "";
            composer.subject = replyDefaults.subject || "";
            const last = [...(detail?.messages || [])]
                .reverse().find((m) => m.direction === "incoming");
            composer.body = last
                ? `\n\nOn ${this.fmtDate(last.date)}, ${last.author_name} wrote:\n${last.body_plain || ""}`
                : "";
        } else if (mode === "forward") {
            composer.subject = conv ? `Fwd: ${conv.name}` : "";
            const last = [...(detail?.messages || [])]
                .reverse().find((m) => m.direction === "incoming");
            composer.body = last
                ? `\n\nOn ${this.fmtDate(last.date)}, ${last.author_name} wrote:\n${last.body_plain || ""}`
                : "";
        } else if (mode === "compose") {
            composer.to = conv?.partner_email || "";
        }
        if (opts.body !== undefined) {
            composer.body = opts.body;
        }
        if (opts.subject !== undefined) {
            composer.subject = opts.subject;   // F-2: quote reply subject
        }
        this.state.composer = composer;
        if (mode === "reply" || mode === "reply_all") {
            if (!composer.to.trim()) {
                // D-3: no automatic reply recipient resolved (sender
                // internal or no external email) — the dispatcher must add
                // one manually; the server refuses an empty send.
                this.notification.add(
                    "No automatic reply recipient was found (sender is "
                    + "internal or has no external email) — verify the To field.",
                    { type: "warning" });
            }
        }
        this._scrollComposer();
    }

    async addNote() {
        if (this.state.composer.sending) {
            return;
        }
        if (!(this.state.composer.body || "").trim()) {
            this.notification.add("The note is empty.", { type: "warning" });
            return;
        }
        await this._compose(true, "note");
    }

    async saveDraft() {
        await this._compose(false);
    }

    async sendNow() {
        await this._compose(true);
    }

    async _compose(sendNow, forceKind = null) {
        const c = this.state.composer;
        const kind = forceKind || c.mode;
        if (!kind) {
            return;
        }
        // Client-side validation mirrors the server: a Send without any
        // recipient is refused here, before the RPC, with the same message.
        // Replies get the D-3 message — the sender had no resolvable
        // external address; the recipient must be added manually.
        if (sendNow && kind !== "note" && !this._parseRecipients(c.to).length) {
            this.notification.add(
                kind === "reply" || kind === "reply_all"
                    ? "No reply recipient — add the customer's email address manually before sending."
                    : "No recipient — add the customer's email address before sending.",
                { type: "danger" });
            return;
        }
        c.sending = true;
        try {
            const res = await this.orm.call(
                "prema.inbox.conversation", "compose_and_send",
                [this.state.selectedId, c.subject, c.body, kind, sendNow],
                {
                    to_partner_ids: this._parseRecipients(c.to),
                    cc_partner_ids: kind === "compose" || kind === "reply_all"
                        ? this._parseRecipients(c.cc) : [],
                    attachment_ids: c.attachments.map((a) => a.id),
                    draft_id: c.draftId,
                });
            if (res?.conversation_id
                    && kind === "compose"
                    && res.conversation_id !== this.state.selectedId) {
                // New email → its OWN conversation: select it.
                this.state.selectedId = res.conversation_id;
                this.state.detail = null;
                await this.openConversation(res.conversation_id);
                this.loadConversations();
            }
            const wasDraft = Boolean(c.draftId);
            if (!sendNow || kind === "note") {
                // Draft saved — keep the composer open so the user keeps
                // editing; remember the id so the next Save edits the SAME
                // message (a resumed draft never becomes a second row).
                c.draftId = res?.id || c.draftId;
                c.sending = false;
                this.notification.add(
                    kind === "note"
                        ? "Internal note added to the thread."
                        : (wasDraft ? "Draft updated." : "Draft saved to the Drafts folder."),
                    { type: "info" });
            } else {
                const label = OUTBOUND_LABELS[res?.outbound_state] || "Recorded";
                if (res?.outbound_state === "failed") {
                    this.notification.add(
                        `${label} — ${res.send_error || "see the message status."}`,
                        { type: "danger" });
                } else {
                    this.notification.add(
                        res?.outbound_state === "intercepted"
                            ? "Outbound intercepted (never sent) — UAT mode."
                            : `Message ${label.toLowerCase()}.`,
                        { type: "info" });
                }
                this.state.composer = {
                    mode: null, body: "", to: "", cc: "", subject: "",
                    attachments: [], draftId: null, sending: false,
                };
            }
            await this.reconcile();
        } catch (e) {
            console.error("compose failed:", e);
            c.sending = false;
            this.notification.add(
                this._rpcError(
                    e,
                    sendNow ? "Could not send the message." : "Could not save the message."),
                { type: "danger" });
        }
    }

    resumeDraft(draft) {
        const kind = ["reply", "reply_all", "forward", "compose"]
            .includes(draft.kind) ? draft.kind : "compose";
        this.state.composer = {
            mode: kind,
            body: draft.body_plain || draft.body || "",
            to: draft.to.map((p) => p.email).join(", "),
            cc: draft.cc.map((p) => p.email).join(", "),
            subject: draft.subject || "",
            attachments: draft.attachments || [],
            draftId: draft.id,
            sending: false,
        };
        this._scrollComposer();
    }

    async discardDraft(draftId) {
        if (!window.confirm("Discard this draft? It cannot be recovered.")) {
            return;
        }
        try {
            await this.orm.call(
                "prema.inbox.conversation", "discard_draft",
                [this.state.selectedId, draftId]);
            if (this.state.composer.draftId === draftId) {
                this.state.composer = {
                    mode: null, body: "", to: "", cc: "", subject: "",
                    attachments: [], draftId: null, sending: false,
                };
            }
            this.notification.add("Draft discarded.", { type: "info" });
            await this.reconcile();
        } catch (e) {
            this.notification.add(this._rpcError(e, "Could not discard the draft."), {
                type: "danger",
            });
        }
    }

    async retrySend(messageId) {
        try {
            const res = await this.orm.call(
                "prema.inbox.conversation", "retry_send",
                [this.state.selectedId, messageId]);
            const label = OUTBOUND_LABELS[res?.outbound_state] || "Recorded";
            if (res?.outbound_state === "failed") {
                this.notification.add(`${label} — ${res.send_error || ""}`, {
                    type: "danger",
                });
            } else {
                this.notification.add(`Retry: ${label.toLowerCase()}.`, { type: "info" });
            }
            await this.reconcile();
        } catch (e) {
            this.notification.add(this._rpcError(e, "Retry failed."), {
                type: "danger",
            });
        }
    }

    _parseRecipients(text) {
        // Accept partner ids (numeric) or raw email strings; the server
        // resolves both through the canonical partner resolver.
        return (text || "").split(/[,;]/)
            .map((s) => s.trim())
            .filter(Boolean)
            .map((token) => (/^\d+$/.test(token) ? parseInt(token, 10) : token));
    }

    async onFileSelected(ev) {
        const files = [...(ev.target.files || [])];
        ev.target.value = "";
        for (const file of files) {
            try {
                const b64 = await this._fileToBase64(file);
                const created = await this.orm.create("ir.attachment", {
                    name: file.name,
                    datas: b64,
                    mimetype: file.type || "",
                });
                this.state.composer.attachments.push({
                    id: created[0],
                    name: file.name,
                    size: file.size,
                });
            } catch (e) {
                console.error("attachment upload failed:", e);
                this.notification.add(
                    `Could not upload attachment ${file.name}.`, { type: "danger" });
            }
        }
    }

    removeAttachment(attId) {
        this.state.composer.attachments = this.state.composer.attachments
            .filter((a) => a.id !== attId);
    }

    _fileToBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => {
                const dataUrl = reader.result || "";
                resolve(dataUrl.split(",")[1] || "");
            };
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    // ------------------------------------------------------------------
    // business links (C3) — native Dropdown picker
    // ------------------------------------------------------------------
    async searchLinks(model) {
        const manual = this.state.linkCandidates?.manual || false;
        let records = [];
        try {
            records = await this.orm.call(
                "prema.inbox.conversation", "inbox_link_candidates",
                [model, this.state.selectedId, this.state.linkSearch || "", manual]);
        } catch (e) {
            console.error("link candidates failed:", e);
        }
        this.state.linkCandidates = { model, records, manual };
        // opening is owned by the Dropdown's target toggle (and the
        // onOpen seeding above) — calling open() here would recurse
    }

    onLinkSearch() {
        clearTimeout(this._linkTimer);
        this._linkTimer = setTimeout(
            () => this.searchLinks(this.state.linkCandidates?.model || "booking"), 250);
    }

    async toggleManualLinkSearch(checked) {
        this.state.linkCandidates.manual = checked;
        await this.searchLinks(this.state.linkCandidates?.model || "booking");
    }

    async linkRecord(model, recordId) {
        try {
            await this.orm.call(
                "prema.inbox.conversation", "action_link_record",
                [this.state.selectedId, model, recordId, this.state.linkSearch || ""]);
        } catch (e) {
            this.notification.add(this._rpcError(e, "Could not link the record."), {
                type: "danger",
            });
            return;
        }
        this.linkDropdown.close();
        this.state.linkCandidates = null;
        this.state.linkSearch = "";
        await this.reconcile();
    }

    async unlinkRecord(model) {
        await this.orm.call(
            "prema.inbox.conversation", "action_unlink_record",
            [this.state.selectedId, model]);
        await this.reconcile();
    }

    openLinkedRecord(model, recordId) {
        const resModel = LINK_MODELS[model];
        if (!resModel || !recordId) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: resModel,
            res_id: recordId,
            views: [[false, "form"]],
        });
    }

    // ------------------------------------------------------------------
    // D-4: partner resolution — deterministic, dispatcher-confirmed
    // ------------------------------------------------------------------
    openPartner() {
        const conv = this.state.detail?.conversation;
        if (!conv?.partner_id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "res.partner",
            res_id: conv.partner_id,
            views: [[false, "form"]],
        });
    }

    async confirmPartner(partnerId) {
        try {
            await this.orm.call(
                "prema.inbox.conversation", "action_confirm_partner",
                [this.state.selectedId, partnerId]);
            this.notification.add("Customer confirmed.", { type: "info" });
        } catch (e) {
            this.notification.add(
                this._rpcError(e, "Could not confirm the customer."),
                { type: "danger" });
        }
        await this.reconcile();
        this.loadConversations();
    }

    async dismissProvisional() {
        // "Leave unassigned" — clears the flag WITHOUT associating anything
        // (a wrong customer is a high-severity error; never guess).
        try {
            await this.orm.call(
                "prema.inbox.conversation", "action_confirm_partner",
                [this.state.selectedId, false]);
        } catch (e) {
            /* keep the banner — the user decides next */
        }
        await this.reconcile();
        this.loadConversations();
    }

    linkTypeLabel(model) {
        return LINK_LABELS[model] || model;
    }

    // D-5: model-specific detail line under each link candidate — the
    // dispatcher recognizes the right record at a glance.
    linkDetail(r, model) {
        const parts = [];
        if (r.number && r.number !== r.name) {
            parts.push(r.number);
        }
        if (model === "booking") {
            if (r.pickup && r.delivery) {
                parts.push(`${r.pickup} → ${r.delivery}`);
            } else if (r.pickup || r.delivery) {
                parts.push(r.pickup || r.delivery);
            }
        } else if (model === "job") {
            if (r.route) {
                parts.push(r.route);
            }
        } else if (model === "invoice") {
            if (r.total !== null && r.total !== undefined) {
                parts.push(this.fmtMoney(r.total));
            }
            if (r.payment_state) {
                parts.push(r.payment_state);
            }
        } else if (model === "opportunity") {
            if (r.stage) {
                parts.push(r.stage);
            }
            if (r.salesperson) {
                parts.push(r.salesperson);
            }
            if (r.activity) {
                parts.push(`activity: ${r.activity}`);
            }
        }
        if (r.date) {
            parts.push(this.fmtDate(r.date));
        }
        return parts.join(" · ");
    }

    // ------------------------------------------------------------------
    // AI panel (C4)
    // ------------------------------------------------------------------
    async aiAction(action) {
        this.state.ai.busy = true;
        try {
            const res = await this.orm.call(
                "prema.inbox.conversation", "inbox_ai_action",
                [this.state.selectedId, action]);
            if (action === "draft_reply" && res?.text) {
                // The reply lands in the composer as an EDITABLE DRAFT —
                // AI never sends anything.
                this.startComposer("reply", { body: res.text });
                if (window.innerWidth < 900) {
                    this.state.mobileScreen = "conversation";
                }
                this.notification.add(
                    "Draft reply placed in the composer — review and edit before sending.",
                    { type: "info" });
            }
            await this.reconcile();
            this.state.ai.busy = false;
            return res;
        } catch (e) {
            console.error("ai action failed:", e);
            this.state.ai.busy = false;
            this.notification.add(
                "AI action failed — see server log. Inbox continues in manual mode.",
                { type: "danger" });
            return null;
        }
    }

    async calculatePrice() {
        this.state.ai.busy = true;
        try {
            const res = await this.orm.call(
                "prema.inbox.conversation", "inbox_calculate_price",
                [this.state.selectedId]);
            this.state.ai.busy = false;
            if (res && !res.available) {
                this.notification.add(
                    res.reason_text || `${res.reason} — no quote invented.`,
                    { type: "warning" });
            }
            await this.reconcile();
            return res;
        } catch (e) {
            console.error("pricing failed:", e);
            this.state.ai.busy = false;
            return null;
        }
    }

    // ------------------------------------------------------------------
    // D-10 / F-1 / F-2 — pricing state, dispatcher adjustment, quote reply
    // ------------------------------------------------------------------
    pricingState() {
        // {state, label} — server-derived from the immutable snapshot.
        return this.state.detail?.pricing?.state ||
            { state: "NOT_PRICED", label: "Not priced" };
    }

    pricingStateClass(state) {
        return {
            READY: "o_inbox_ps_ready",
            NEEDS_INFORMATION: "o_inbox_ps_warn",
            PARTIAL_ESTIMATE: "o_inbox_ps_partial",
            ENGINE_UNAVAILABLE: "o_inbox_ps_error",
            NOT_PRICED: "o_inbox_ps_muted",
        }[state] || "o_inbox_ps_muted";
    }

    quoteState() {
        return this.state.detail?.pricing?.quote || {};
    }

    quoteBreakdown() {
        return this.state.detail?.pricing?.breakdown || [];
    }

    async saveAdjustment() {
        const quote = this.quoteState();
        if (!quote.engine_calculated_price) {
            this.notification.add(
                "Run 'Review & calculate quote' first — there is no engine "
                + "price to adjust.",
                { type: "warning" });
            return;
        }
        const raw = (this.state.ai.adjInput ?? "").toString().trim();
        const amount = raw === "" ? null : Number(raw);
        if (amount !== null && !Number.isFinite(amount)) {
            this.notification.add("Adjustment must be a number.", { type: "warning" });
            return;
        }
        const res = await this.orm.call(
            "prema.inbox.conversation", "action_set_quoted_price",
            [this.state.selectedId, amount ?? 0, this.state.ai.adjReason || ""]);
        this.state.ai.adjInput = "";
        if (res?.error) {
            this.notification.add(res.error, { type: "warning" });
            return;
        }
        this.notification.add(
            amount === 0
                ? "Quote reset to the engine price."
                : `Final quoted price updated to ${this.fmtMoney(res.final_quoted_price, res.currency)}.`,
            { type: "info" });
        await this.reconcile();
    }

    async clearAdjustment() {
        this.state.ai.adjInput = "";
        this.state.ai.adjReason = "";
        await this.orm.call(
            "prema.inbox.conversation", "action_set_quoted_price",
            [this.state.selectedId, 0, ""]);
        await this.reconcile();
    }

    async quoteReply() {
        // F-2: deterministic template → composer (dispatcher edits + sends).
        // NEVER auto-sends.
        const res = await this.orm.call(
            "prema.inbox.conversation", "action_quote_reply",
            [this.state.selectedId]);
        if (res?.error) {
            this.notification.add(res.error, { type: "warning" });
            return;
        }
        this.startComposer("reply", { subject: res.subject, body: res.body });
        if (window.innerWidth < 900) {
            this.state.mobileScreen = "conversation";
        }
        this.notification.add(
            "Quote reply placed in the composer — review and send.",
            { type: "info" });
        await this.reconcile();
    }

    // ------------------------------------------------------------------
    // D-9 — editable shipment extraction (inline, provenance 'manual')
    // ------------------------------------------------------------------
    editExtraction(key) {
        const f = this.state.detail?.ai?.extraction?.fields || {};
        const current = key === "pickup" || key === "delivery"
            ? (f[key]?.postal_code || "")
            : (f[key] ?? "");
        this.state.ai.editingKey = key;
        this.state.ai.editValue = String(current ?? "");
    }

    cancelEditExtraction() {
        this.state.ai.editingKey = null;
        this.state.ai.editValue = "";
    }

    async saveEditExtraction() {
        const key = this.state.ai.editingKey;
        const raw = (this.state.ai.editValue || "").trim();
        const f = this.state.detail?.ai?.extraction?.fields || {};
        let updates = {};
        if (key === "pickup" || key === "delivery") {
            // stop edit → postal code (the fix-critical value for pricing)
            const stop = { ...(f[key] || {}) };
            stop.postal_code = raw || null;
            updates[key] = stop;
        } else if (key === "pallets" || key === "weight_lbs"
                   || key === "temperature_c") {
            updates[key] = raw === "" ? null : Number(raw);
        } else {
            updates[key] = raw;
        }
        const res = await this.orm.call(
            "prema.inbox.conversation", "action_update_extraction",
            [this.state.selectedId, updates]);
        this.cancelEditExtraction();
        if (res?.error) {
            this.notification.add(res.error, { type: "warning" });
            return;
        }
        this.notification.add(
            `Extraction updated (${Object.keys(updates).join(", ")}) — `
            + "recalculate the quote to refresh pricing.",
            { type: "info" });
        await this.reconcile();
    }

    toggleAiPanel() {
        this.state.ai.panelOpen = !this.state.ai.panelOpen;
    }

    toggleConflicts() {
        this.state.ai.conflictsOpen = !this.state.ai.conflictsOpen;
    }

    isMine(uid) {
        return uid === this.userId;
    }

    linkedRows() {
        const c = this.state.detail?.conversation;
        if (!c) {
            return [];
        }
        const rows = [];
        for (const [model, id, name] of [
            ["booking", c.booking_id, c.booking_name],
            ["job", c.job_id, c.job_name],
            ["invoice", c.invoice_id, c.invoice_name],
            ["opportunity", c.opportunity_id, c.opportunity_name],
        ]) {
            if (id) {
                rows.push({ model, id, name, label: LINK_LABELS[model] });
            }
        }
        return rows;
    }

    toLine(m) {
        return (m.to || []).map((p) => p.email).join(", ");
    }

    // Formatted extraction — never raw JSON in the panel. Pickup/Delivery
    // rows carry their own "Postal/FSA: missing" marker.
    extractionRows() {
        const ex = this.state.detail?.ai?.extraction;
        if (!ex) {
            return [];
        }
        const f = ex.fields || {};
        const rows = [];
        const addStop = (label, key) => {
            const stop = f[key] || {};
            const city = [stop.city, stop.province].filter(Boolean).join(", ");
            const postal = (stop.postal_code || "").trim();
            const fsa = postal.split(" ")[0] || "";
            rows.push({
                label, key,
                value: city || stop.address || "",
                fsa,
                missing: !fsa,
            });
        };
        addStop("Pickup", "pickup");
        addStop("Delivery", "delivery");
        const scalar = (label, key, fmt = (v) => v) => {
            if (f[key] !== undefined && f[key] !== null && f[key] !== "") {
                rows.push({ label, key, value: fmt(f[key]) });
            }
        };
        scalar("Pallets", "pallets", (v) => `${v} pallet${v === 1 ? "" : "s"}`);
        scalar("Weight", "weight_lbs", (v) => `${Number(v).toLocaleString()} lbs`);
        scalar("Equipment", "equipment");
        scalar("Temperature", "temperature_c", (v) => `${v} °C`);
        scalar("Accessorials", "accessorials", (v) =>
            Array.isArray(v) ? v.join(", ") : v);
        scalar("Reference numbers", "reference_numbers", (v) =>
            Array.isArray(v) ? v.join(", ") : v);
        return rows;
    }

    extractionMissing() {
        return this.state.detail?.ai?.extraction?.missing || [];
    }

    extractionConflicts() {
        return this.state.detail?.ai?.extraction?.conflicting || [];
    }

    toggleFormatted(messageId) {
        // D-2: incoming HTML (sanitized at ingest) is the DEFAULT view;
        // this toggles OUT to the plain-text rendering.
        this.state.plainMsg =
            this.state.plainMsg === messageId ? null : messageId;
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------
    folderLabel(key) {
        return this.state.folders.find((f) => f.key === key)?.label || "";
    }

    attIcon(mimetype) {
        const m = (mimetype || "").toLowerCase();
        if (m.includes("pdf")) {
            return "fa-file-pdf-o";
        }
        if (m.startsWith("image/")) {
            return "fa-file-image-o";
        }
        if (m.startsWith("text/")) {
            return "fa-file-text-o";
        }
        return "fa-file-o";
    }

    attHref(att) {
        return `/prema_inbox/attachment/${att.id}/${encodeURIComponent(att.name)}`;
    }

    attDownloadHref(att) {
        return `${this.attHref(att)}?download=1`;
    }

    fmtDate(iso) {
        if (!iso) {
            return "";
        }
        const d = new Date(iso);
        return d.toLocaleString([], {
            month: "short", day: "numeric",
            hour: "2-digit", minute: "2-digit",
        });
    }

    fmtMoney(amount, currency) {
        if (amount === null || amount === undefined) {
            return "—";
        }
        return new Intl.NumberFormat("en-CA", {
            style: "currency",
            currency: currency || "CAD",
        }).format(amount);
    }

    fmtValue(value) {
        // OWL 2 template scope has no `JSON` global — any object value must
        // be stringified from JS, not in the template.
        if (value === null || value === undefined) {
            return "";
        }
        return typeof value === "object" ? JSON.stringify(value) : value;
    }

    _rpcError(e, fallback) {
        // The server's ValidationError text is the honest message — never
        // mask it behind the generic "Could not save the message."
        return e?.data?.message || e?.message || fallback;
    }

    // ------------------------------------------------------------------
    // mobile stack + layout
    // ------------------------------------------------------------------
    backTo(kind) {
        if (kind === "list") {
            this.state.mobileScreen = "list";
        } else if (kind === "conversation") {
            this.state.mobileScreen = "conversation";
        } else {
            this.state.mobileScreen = "ai";
        }
    }

    _scrollComposer() {
        // Bring the composer into view on small screens.
        requestAnimationFrame(() => {
            const el = this.el?.querySelector(".o_inbox_composer");
            el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        });
    }
}

InboxApp.CATEGORY_LABELS = CATEGORY_LABELS;
InboxApp.STATE_LABELS = STATE_LABELS;
InboxApp.PRIORITY_LABELS = PRIORITY_LABELS;
InboxApp.LINK_LABELS = LINK_LABELS;
InboxApp.OUTBOUND_LABELS = OUTBOUND_LABELS;
InboxApp.CATEGORY_OPTIONS = Object.entries(CATEGORY_LABELS).map(
    ([value, label]) => ({ value, label }));
InboxApp.STATE_OPTIONS = Object.entries(STATE_LABELS).map(
    ([value, label]) => ({ value, label }));

registry.category("actions").add("prema_inbox_main", InboxApp);
