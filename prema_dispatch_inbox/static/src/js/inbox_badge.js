/** @odoo-module **/
// Top-bar envelope with live unread badge (design §1/§3).
// Anchored before the navbar breadcrumbs (beside the app title) via a
// NavBar template extension + components patch — community and enterprise
// navbar both resolve it. Server truth wins: reconcile on mount, on
// focus, and on every bus event.

import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { patch } from "@web/core/utils/patch";
import { NavBar } from "@web/webclient/navbar/navbar";
import { useService } from "@web/core/utils/hooks";
import { user } from "@web/core/user";
// Odoo 18 removed the "rpc" service — raw JSON-RPC must be imported
// directly (same pattern as @web/core/user.js / orm_service.js).
import { rpc } from "@web/core/network/rpc";

const LS_ANIM = "prema_inbox.animations";
const LS_SOUND = "prema_inbox.sound";
const LS_EVENT = "prema_inbox.event."; // + event_id → multi-tab dedupe

export class InboxBadge extends Component {
    static template = "prema_dispatch_inbox.InboxBadge";
    static props = {};

    setup() {
        this.busService = useService("bus_service");
        this.notification = useService("notification");
        this.action = useService("action");
        // Odoo 18: uid lives on the @web/core/user module export (no "user"
        // service; env.userId is undefined). See inbox_app.js.
        this.channel = `prema_inbox:${user.userId}`;
        this.state = useState({
            total: 0, spam: 0, pulse: false,
            popover: false,
            animations: localStorage.getItem(LS_ANIM) !== "off",
            sound: localStorage.getItem(LS_SOUND) === "on",
        });
        this._onFocus = () => this.reconcile();
        // Arrow closure — see inbox_app.js: bus_service calls the callback
        // as a plain function, so a prototype method would lose `this`.
        this._premaEventCb = (payload) => {
            this._onEvent(payload);
        };
        onMounted(async () => {
            await this.reconcile();
            try {
                // A dead bus (websocket down, evented port unreachable) must
                // never reject this lifecycle hook: an unhandled OWL error
                // opens Odoo's technical modal and freezes the webclient.
                await this.busService.addChannel(this.channel);
                // Odoo 18 delivery model: bus._sendone(channel, notif_type,
                // payload) arrives on the client's notificationBus keyed by
                // notif_type — the channel name only routes it to the right
                // websocket. So: addChannel("prema_inbox:{uid}") gates who
                // receives, subscribe("prema_inbox") is the listener key.
                this.busService.subscribe("prema_inbox", this._premaEventCb);
                // bus worker reconnect → counts may have drifted while offline
                this.busService.addEventListener("reconnect", this._onFocus);
            } catch (e) {
                console.error("inbox badge bus subscribe failed — live updates disabled:", e);
            }
            window.addEventListener("focus", this._onFocus);
        });
        onWillUnmount(() => {
            this.busService.deleteChannel(this.channel);
            this.busService.unsubscribe("prema_inbox", this._premaEventCb);
            window.removeEventListener("focus", this._onFocus);
            this.busService.removeEventListener("reconnect", this._onFocus);
        });
    }

    async reconcile() {
        try {
            // Odoo refuses to call private model methods over call_kw —
            // the controller route is the public surface for this
            const res = await rpc("/prema_inbox/unread_counts", {});
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

// Approved placement (design §1): the envelope sits BESIDE the app title,
// immediately before the breadcrumbs — not in the systray. OWL resolves
// <InboxBadge/> in the navbar template through the rendering component's
// constructor.components; web_enterprise.EnterpriseNavBar extends NavBar
// and inherits this map (no own `components` static), so one patch covers
// both navbar flavours.
patch(NavBar, {
    components: { ...NavBar.components, InboxBadge },
});
