/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { DispatchLiveMap } from "./live_map";

/**
 * Driver Updates is deliberately layered onto the existing Live Map instead
 * of changing its fleet/GPS logic. The map remains authoritative and usable
 * even if this optional operational-inbox RPC fails.
 */
patch(DispatchLiveMap.prototype, {
    setup() {
        super.setup(...arguments);
        this.notification = useService("notification");
        Object.assign(this.state, {
            sidebarTab: "fleet",
            driverUpdates: [],
            driverUpdateOpenCount: 0,
            driverUpdatesLoading: false,
            driverUpdatesError: null,
            replyOpenId: null,
            replyDraft: "",
            // §9 chronological operational feed (activity tab).
            activityFeed: [],
            activityUrgentCount: 0,
            activityLoading: false,
            activityError: null,
        });
    },

    async _init() {
        await super._init(...arguments);
        await this._refreshDriverUpdates();
        await this._refreshActivityFeed();
    },

    async _refreshData() {
        await super._refreshData(...arguments);
        await this._refreshDriverUpdates();
        await this._refreshActivityFeed();
    },

    async _refreshDriverUpdates() {
        this.state.driverUpdatesLoading = true;
        try {
            const data = await this.orm.call(
                "prema.dispatch.driver.update",
                "get_live_updates",
                []
            );
            this.state.driverUpdates = data?.updates || [];
            this.state.driverUpdateOpenCount = Number(data?.open_count || 0);
            this.state.driverUpdatesError = null;
        } catch (error) {
            // Never take down the Live Map because the operational inbox had
            // a problem. Dispatch still retains Fleet/GPS/stop visibility.
            this.state.driverUpdatesError = error?.message || "Could not load Driver Updates.";
            console.warn("Driver Updates refresh failed", error);
        } finally {
            this.state.driverUpdatesLoading = false;
        }
    },

    showSidebarTab(tab) {
        this.state.sidebarTab = (tab === "updates" || tab === "activity") ? tab : "fleet";
        this.state.replyOpenId = null;
        this.state.replyDraft = "";
    },

    /**
     * §9 Activity feed: every chronological operational event, recent
     * first. Observability-only — this refresh failing must never take
     * down the map, exactly like the alerts panel.
     */
    async _refreshActivityFeed() {
        this.state.activityLoading = true;
        try {
            const data = await this.orm.call(
                "prema.dispatch.driver.update",
                "get_feed",
                [60]
            );
            this.state.activityFeed = data?.updates || [];
            this.state.activityUrgentCount = this.state.activityFeed.filter(
                (u) => u.severity === "urgent" && !u.is_alert
            ).length;
            this.state.activityError = null;
        } catch (error) {
            this.state.activityError = error?.message || "Could not load the activity feed.";
            console.warn("Activity feed refresh failed", error);
        } finally {
            this.state.activityLoading = false;
        }
    },

    feedSeverityIcon(severity) {
        return severity === "urgent" ? "🚨" : severity === "warning" ? "⚠" : "●";
    },

    async acknowledgeDriverUpdate(updateId) {
        try {
            const result = await this.orm.call(
                "prema.dispatch.driver.update",
                "acknowledge_update",
                [updateId]
            );
            if (!result?.success) {
                this.notification.add(result?.error || "Could not acknowledge this update.", {type: "danger"});
                return;
            }
            this.notification.add("Driver update acknowledged.", {type: "success"});
            await this._refreshDriverUpdates();
        } catch (error) {
            this.notification.add(error?.message || "Could not acknowledge this update.", {type: "danger"});
        }
    },

    async dismissDriverUpdate(updateId) {
        try {
            const result = await this.orm.call(
                "prema.dispatch.driver.update",
                "dismiss_update",
                [updateId]
            );
            if (!result?.success) {
                this.notification.add(result?.error || "Could not dismiss this update.", {type: "danger"});
                return;
            }
            if (this.state.replyOpenId === updateId) {
                this.state.replyOpenId = null;
                this.state.replyDraft = "";
            }
            await this._refreshDriverUpdates();
        } catch (error) {
            this.notification.add(error?.message || "Could not dismiss this update.", {type: "danger"});
        }
    },

    toggleDriverUpdateReply(updateId) {
        if (this.state.replyOpenId === updateId) {
            this.state.replyOpenId = null;
            this.state.replyDraft = "";
            return;
        }
        this.state.replyOpenId = updateId;
        this.state.replyDraft = "";
    },

    onDriverUpdateReplyInput(ev) {
        this.state.replyDraft = ev.target.value || "";
    },

    async sendDriverUpdateReply(updateId) {
        const body = (this.state.replyDraft || "").trim();
        if (!body) {
            this.notification.add("Write a reply first.", {type: "warning"});
            return;
        }
        try {
            const result = await this.orm.call(
                "prema.dispatch.driver.update",
                "reply_update",
                [updateId, body]
            );
            if (!result?.success) {
                this.notification.add(result?.error || "Could not send this reply.", {type: "danger"});
                return;
            }
            this.state.replyOpenId = null;
            this.state.replyDraft = "";
            this.notification.add("Reply sent to the driver's existing Dispatch Chat.", {type: "success"});
            await this._refreshDriverUpdates();
        } catch (error) {
            this.notification.add(error?.message || "Could not send this reply.", {type: "danger"});
        }
    },
});
