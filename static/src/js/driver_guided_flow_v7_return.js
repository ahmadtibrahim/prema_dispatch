/* V7 deferred-stop helper: optional return time without changing freight state. */
"use strict";

(function () {
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    function toastSafe(message) {
        try { if (typeof toast === "function") toast(message); else console.info(message); }
        catch (_) { console.info(message); }
    }

    function reasonCode(text) {
        const value = (text || "").toLowerCase();
        if (value.includes("appointment")) return "appointment_later";
        if (value.includes("dock")) return "dock_unavailable";
        if (value.includes("wait")) return "long_wait";
        if (value.includes("dispatch")) return "dispatcher_instructed";
        if (value.includes("open") || value.includes("closed")) return "customer_closed";
        return "other";
    }

    function returnAtFromChoice(raw) {
        const value = (raw || "").trim().toLowerCase();
        if (!value || ["later", "none", "manual"].includes(value)) return "";
        const now = new Date();
        if (["30", "30m", "30 min", "30 minutes"].includes(value)) {
            return new Date(now.getTime() + 30 * 60000).toISOString();
        }
        if (["60", "1h", "1 hr", "1 hour", "hour"].includes(value)) {
            return new Date(now.getTime() + 60 * 60000).toISOString();
        }
        const match = value.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/);
        if (!match) return "";
        let hour = Number(match[1]);
        const minute = Number(match[2] || 0);
        const meridian = match[3];
        if (minute > 59) return "";
        if (meridian) {
            if (hour < 1 || hour > 12) return "";
            if (meridian === "pm" && hour < 12) hour += 12;
            if (meridian === "am" && hour === 12) hour = 0;
        } else if (hour > 23) return "";
        const target = new Date(now);
        target.setHours(hour, minute, 0, 0);
        if (target <= now) target.setDate(target.getDate() + 1);
        return target.toISOString();
    }

    async function deferWithReturnTime() {
        const stop = typeof S !== "undefined" ? S.stop : null;
        if (!stop?.id) return;
        const reason = window.prompt(
            "Come back later because:\nCustomer not open / Appointment later / Dock unavailable / Long wait / Dispatcher instructed / Other",
            "Customer not open"
        );
        if (reason === null) return;
        const choice = window.prompt(
            "When should this stop come back up?\nEnter 30m, 1h, a time like 7:00am, or Later",
            "Later"
        );
        if (choice === null) return;
        const code = reasonCode(reason);
        try {
            const result = await rpc("/dispatch/driver/stop/status", {
                stop_id: stop.id,
                action: "defer",
                data: {
                    reason: code,
                    reason_other: code === "other" ? reason : "",
                    return_at: returnAtFromChoice(choice),
                },
            });
            if (!result?.success) return toastSafe(result?.error || "Could not save this stop for later");
            await reloadDay();
            showScreen("sSchedule");
            if (typeof showViewTab === "function") showViewTab("stops");
            toastSafe(result.message || "Stop saved for later — continue to the next stop.");
        } catch (err) {
            toastSafe(err?.message || "Could not save this stop for later");
        }
    }

    function rewriteDeferButtons(root) {
        (root || document).querySelectorAll?.('[data-v7="defer"]').forEach(button => {
            button.dataset.v7 = "defer-with-return";
            button.textContent = "↪ Come Back Later";
        });
    }

    function boot() {
        const app = document.getElementById("app");
        if (!app?.classList.contains("da-app")) return;
        rewriteDeferButtons(app);
        document.addEventListener("click", event => {
            const button = event.target.closest('[data-v7="defer-with-return"]');
            if (!button) return;
            event.preventDefault();
            deferWithReturnTime();
        });
        const observer = new MutationObserver(records => {
            for (const record of records) {
                for (const node of record.addedNodes) {
                    if (node.nodeType === 1) rewriteDeferButtons(node);
                }
            }
        });
        observer.observe(app, {subtree: true, childList: true});
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
    else boot();
})();
