/** @odoo-module **/
// Top-bar envelope with live unread badge (design §3).
// Systray item → works identically in community and enterprise navbar.
// Server truth wins: reconcile on mount, on focus, and on every bus event.

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const LS_ANIM = "prema_inbox.animations";
const LS_SOUND = "prema_inbox.sound";
const LS_EVENT = "prema_inbox.event."; // + event_id → multi-tab dedupe

export class InboxBadge extends Component {
    static template = "prema_dispatch_inbox.InboxBadge";
    static props = {};

    setup() {
        this.rpc = useService("rpc");
        this.busService = useService("bus_service");
        this.notification = useService("notification");
        this.action = useService("action");
        this.channel = `prema_inbox:${this.env.userId}`;
        this.state = useState({
            total: 0, spam: 0, pulse: false,
            popover: false,
            animations: localStorage.getItem(LS_ANIM) !== "off",
            sound: localStorage.getItem(LS_SOUND) === "on",
        });
        this._onFocus = () => this.reconcile();
        onMounted(async () => {
            await this.reconcile();
            await this.busService.addChannel(this.channel);
            this.busService.subscribe(this.channel, (payload) => this._onEvent(payload));
            window.addEventListener("focus", this._onFocus);
            // bus worker reconnect → counts may have drifted while offline
            this.busService.addEventListener("reconnect", this._onFocus);
        });
        onWillUnmount(() => {
            this.busService.deleteChannel(this.channel);
            window.removeEventListener("focus", this._onFocus);
        });
    }

    async reconcile() {
        try {
            // Odoo refuses to call private model methods over call_kw —
            // the controller route is the public surface for this
            const res = await this.rpc("/prema_inbox/unread_counts", {});
            if (!res || !res.counts) {
                return;
            }
            this.state.total = res.counts.total || 0;
            this.state.spam = res.counts.spam || 0;
        } catch (e) {
            console.error("inbox badge reconcile failed:", e);
        }
    }

    _onEvent(payload) {
        if (!payload || payload.type === "read_change") {
            return; // read changes: reconcile quietly
        }
        this.reconcile();
        if (payload.is_spam) {
            return; // spam keeps its own count, never disturbs
        }
        if (this.state.animations) {
            this.state.pulse = true;
            setTimeout(() => (this.state.pulse = false), 1300);
        }
        // Multi-tab dedupe: whoever claims the event_id first plays it.
        if (!payload.event_id) {
            return;
        }
        const key = LS_EVENT + payload.event_id;
        try {
            if (localStorage.getItem(key)) {
                return; // another tab already announced this one
            }
            localStorage.setItem(key, "1");
        } catch (_) {
            // storage unavailable — just play here (best effort)
        }
        if (payload.muted) {
            return; // muted: count kept, no toast/sound
        }
        if (this.state.sound) {
            this._beep();
        }
        this.notification.add(
            `New message · ${payload.from_email || ""} — ${payload.subject || ""}`,
            { type: "info", sticky: false, title: "Dispatch Inbox" });
    }

    _beep() {
        try {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) {
                return;
            }
            const ctx = new Ctx();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.frequency.value = 880;
            gain.gain.setValueAtTime(0.06, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.35);
            osc.start();
            osc.stop(ctx.currentTime + 0.35);
        } catch (_) {
            // audio blocked — never crash the badge
        }
    }

    togglePopover() {
        this.state.popover = !this.state.popover;
        if (!this.state.popover) {
            this.reconcile();
        }
    }

    toggleAnimations() {
        this.state.animations = !this.state.animations;
        localStorage.setItem(LS_ANIM, this.state.animations ? "on" : "off");
    }

    toggleSound() {
        this.state.sound = !this.state.sound;
        localStorage.setItem(LS_SOUND, this.state.sound ? "on" : "off");
    }

    openInbox() {
        this.state.popover = false;
        this.action.doAction("prema_dispatch_inbox.action_prema_inbox_main");
    }
}

registry.category("systray").add(
    "prema.inbox.badge",
    { Component: InboxBadge },
    { sequence: 15 },
);
