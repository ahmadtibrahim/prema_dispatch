/** @odoo-module **/
// Prema Dispatch Inbox — single OWL client action (design §4).
// C1 folders · C2 conversation list · C3 conversation · C4 AI assistant.
// Below 900px the columns stack: list → conversation → AI panel.

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
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
            composer: { mode: null, body: "", sending: false },
            ai: { busy: false, panelOpen: true },
            linkCandidates: null,
            assignCandidates: null,
            mobileScreen: "list", // list | conversation | ai (mobile stack)
        });
        this.channel = `prema_inbox:${this.env.userId}`;
        this._timer = null;
        onMounted(async () => {
            await this.refreshFolders();
            await this.loadConversations();
            await this.busService.addChannel(this.channel);
            this.busService.subscribe(this.channel, (payload) => this._onEvent(payload));
            this._timer = setInterval(() => this.reconcile(), 60000);
        });
        onWillUnmount(() => {
            clearInterval(this._timer);
            this.busService.deleteChannel(this.channel);
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
        }
    }

    async loadConversations() {
        this.state.loading = true;
        try {
            this.state.conversations = await this.orm.call(
                "prema.inbox.conversation", "inbox_conversations",
                [this.state.folder, this.state.search || null]);
        } catch (e) {
            console.error("conversations failed:", e);
        } finally {
            this.state.loading = false;
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
        await this.refreshFolders();
        if (this.state.selectedId) {
            const detail = await this.orm.call(
                "prema.inbox.conversation", "inbox_conversation_detail",
                [this.state.selectedId]).catch(() => null);
            if (detail) {
                this.state.detail = detail;
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
        const self = users.find((u) => u.id === this.env.userId);
        if (!self) {
            users.unshift({ id: this.env.userId, login: "me", name: "Me" });
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
            await this.orm.call(
                "prema.inbox.message", "compose_and_send",
                [this.state.selectedId, subject, body, kind, sendNow]);
            this.state.composer = { mode: null, body: "", sending: false };
            this.notification.add(
                sendNow ? "Outbound recorded (intercepted in UAT — never sent)."
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
