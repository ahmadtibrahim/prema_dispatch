/** @odoo-module **/
// Prema Dispatch Inbox — single OWL client action (design §4).
// C1 folders · C2 conversation list · C3 conversation · C4 AI assistant.
// Below 900px the columns stack: list → conversation → AI panel.

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
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

export class InboxApp extends Component {
    static template = "prema_dispatch_inbox.InboxApp";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.busService = useService("bus_service");
        this.notification = useService("notification");
        this.state = useState({
            loading: true,
            folders: [],
            folder: "inbox",
            conversations: [],
            selectedId: null,
            detail: null,
            search: "",
            loadError: null,
            composer: { mode: null, body: "", sending: false },
            ai: { busy: false, panelOpen: true },
            linkCandidates: null,
            assignCandidates: null,
            mobileScreen: "list", // list | conversation | ai (mobile stack)
        });
        // Odoo 18: env.userId does NOT exist (Odoo 16 legacy) — the uid
        // lives on the @web/core/user module export (there is no "user"
        // service either). A wrong/undefined uid silently subscribes to
        // "prema_inbox:undefined" while the server notifies
        // "prema_inbox:{uid}" — the relay matches zero websockets and the
        // inbox never updates live.
        this.channel = `prema_inbox:${user.userId}`;
        // Class-name identifiers do NOT resolve in OWL 2 template scope
        // (proved in prod + UAT: "InboxApp" is undefined at render → TypeError
        // → OwlError → technical modal). Templates must use `this.` instead.
        this.CATEGORY_LABELS = InboxApp.CATEGORY_LABELS;
        this.CATEGORY_OPTIONS = InboxApp.CATEGORY_OPTIONS;
        this.STATE_OPTIONS = InboxApp.STATE_OPTIONS;
        this._timer = null;
        this._searchTimer = null;
        this._convsLoading = false;   // overlap guard: one list load at a time
        this._convsQueued = false;    // coalesce: refresh once the current one ends
        this._reconcileRunning = false;
        this._reconcileQueued = false;
        // Arrow closure: bus_service.subscribe wraps the callback in a plain
        // function call (callback(payload, {id})), so a prototype method
        // loses `this` (TypeError on this._onEvent). Same reference is used
        // for unsubscribe — the service keys its wrapper map on the callback.
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
            // NB: do NOT clear loadError here — it is the conversation-load
            // error state; a folder refresh succeeding while the list load
            // fails would otherwise hide the error panel (race on _onEvent).
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
        // switches) never stacks parallel requests — at most one in flight
        // plus one queued refresh.
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
            this.state.detail = detail;
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
        // Coalesce concurrent reconciles (60s timer + bus events + actions
        // can fire within the same tick) — one run at a time.
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
        // Debounce keystrokes — one RPC after typing pauses, never one per
        // key. (t-model keeps state.search in sync; this fires the query.)
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this.loadConversations(), 250);
    }

    retryLoad() {
        this.state.loadError = null;
        this.refreshFolders();
        this.loadConversations();
    }

    async markUnread(id) {
        // Personal read state — the conversation's incoming messages become
        // unread for me again (folder "Unread" picks it up).
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
    // conversation actions (C3)
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

    async setState(state) {
        await this.orm.call(
            "prema.inbox.conversation", "write",
            [[this.state.selectedId], { workflow_state: state }]);
        this.reconcile();
    }

    async assignUser() {
        const users = await this.orm.call(
            "res.users", "search_read",
            [[["share", "=", false]], ["id", "login", "name"], 0, 50]);
        const self = users.find((u) => u.id === user.userId);
        if (!self) {
            users.unshift({ id: user.userId, login: "me", name: "Me" });
        }
        this.state.assignCandidates = users;
    }

    async doAssign(userId) {
        await this.orm.call(
            "prema.inbox.conversation", "write",
            [[this.state.selectedId], { assignee_id: userId }]);
        this.state.assignCandidates = null;
        this.reconcile();
    }

    async toggleMute() {
        await this.orm.call(
            "prema.inbox.conversation", "action_toggle_mute", [this.state.selectedId]);
        this.reconcile();
    }

    async startComposer(mode) {
        this.state.composer = { mode, body: "", sending: false };
        if (mode === "reply") {
            // prefill with the last message quoted (plain)
            const msgs = this.state.detail?.messages || [];
            const last = [...msgs].reverse().find((m) => m.direction === "incoming");
            this.state.composer.body = last ? `\n\nOn ${last.date}, ${last.author_name} wrote:\n${last.body_plain || ""}` : "";
        }
    }

    async saveDraft() {
        await this._compose(false);
    }

    async sendNow() {
        await this._compose(true);
    }

    async _compose(sendNow) {
        const { mode, body } = this.state.composer;
        this.state.composer.sending = true;
        try {
            const kind = mode === "note" ? "note"
                : mode === "compose" ? "compose" : "reply";
            const subject = mode === "reply"
                ? `Re: ${this.state.detail?.conversation?.name || "No subject"}`
                : body.split("\n")[0].slice(0, 100) || "No subject";
            const res = await this.orm.call(
                "prema.inbox.message", "compose_and_send",
                [this.state.selectedId, subject, body, kind, sendNow]);
            this.state.composer = { mode: null, body: "", sending: false };
            const stateLabel = {
                sent: "Message sent via the configured mail server.",
                pending: "Message queued for delivery.",
                failed: "Delivery failed — see the message status.",
                intercepted: "Outbound intercepted (never sent).",
            }[res?.outbound_state];
            this.notification.add(
                sendNow
                    ? (stateLabel || "Outbound recorded.")
                    : "Draft saved to the Drafts folder.",
                { type: "info" });
            this.reconcile();
        } catch (e) {
            console.error("compose failed:", e);
            this.state.composer.sending = false;
            this.notification.add("Could not save the message.", { type: "danger" });
        }
    }

    // ------------------------------------------------------------------
    // links
    // ------------------------------------------------------------------
    async searchLinks(model) {
        const res = await this.orm.call(
            "prema.inbox.conversation", "inbox_link_candidates",
            [model, this.state.selectedId, ""]);
        this.state.linkCandidates = { model, records: res };
    }

    async linkRecord(model, recordId) {
        await this.orm.call(
            "prema.inbox.conversation", "action_link_record",
            [this.state.selectedId, model, recordId]);
        this.state.linkCandidates = null;
        this.reconcile();
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
            if (action === "extract" || action === "summarize" || action === "draft_reply") {
                this.reconcile();
            }
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
            if (res && !res.available && res.reason) {
                this.notification.add(
                    `Pricing engine: ${res.reason} — no quote invented.`,
                    { type: "warning" });
            }
            this.reconcile();
            return res;
        } catch (e) {
            console.error("pricing failed:", e);
            this.state.ai.busy = false;
            return null;
        }
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------
    folderLabel(key) {
        return this.state.folders.find((f) => f.key === key)?.label || "";
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
        // OWL 2 template scope has no `JSON` global (only Math/Date/Object/
        // RegExp/Array/... are whitelisted in the QWeb compiler) — any
        // object value must be stringified from JS, not in the template.
        if (value === null || value === undefined) {
            return "";
        }
        return typeof value === "object" ? JSON.stringify(value) : value;
    }

    // ------------------------------------------------------------------
    // mobile stack
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
}

InboxApp.CATEGORY_LABELS = CATEGORY_LABELS;
InboxApp.STATE_LABELS = STATE_LABELS;
InboxApp.PRIORITY_LABELS = PRIORITY_LABELS;
InboxApp.CATEGORY_OPTIONS = Object.entries(CATEGORY_LABELS).map(
    ([value, label]) => ({ value, label }));
InboxApp.STATE_OPTIONS = Object.entries(STATE_LABELS).map(
    ([value, label]) => ({ value, label }));

registry.category("actions").add("prema_inbox_main", InboxApp);
