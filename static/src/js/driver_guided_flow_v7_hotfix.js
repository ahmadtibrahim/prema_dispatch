/* Prema Driver v7 bridge fixes.
 * Loaded after driver_guided_flow_v7.js.
 *
 * 1) The legacy /pickup/confirm RPC intentionally persists actual pallet
 *    count/items before returning pickup_gate_blocked. In the guided flow,
 *    that response means "step 1 saved; later steps are still required",
 *    not a failed save. This wrapper reflects that contract in the UI.
 * 2) Come Back Later accepts an optional 30m / 1h / clock-time reminder.
 */
"use strict";

(function () {
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    function notify(message) {
        try { if (typeof toast === "function") toast(message); else console.info(message); }
        catch (_) { console.info(message); }
    }

    // Global function declaration in driver_app.js; replacing the window
    // property updates the function used by the v7 guide without changing the
    // final pickup-completion gate or the backend's source-of-truth checks.
    window.savePickupActuals = async function savePickupActualsV7(nextStep) {
        const flow = typeof S !== "undefined" ? S.pickupIntake : null;
        const stop = typeof currentPickupStop === "function" ? currentPickupStop() : S?.stop;
        if (!flow || !stop) return false;
        try {
            const result = await rpc("/dispatch/driver/pickup/confirm", {
                stop_id: stop.id,
                values: {
                    job_id: stop.job_id,
                    actual_received_pallet_count: Number(flow.actual || 0),
                    variance_notes: flow.varianceNotes || "",
                    route_sheet_received: !!flow.routeSheetReceived,
                    load_plan_id: stop.job_summary?.load_plan_id || S.loadPlan?.id || false,
                    version: S.loadPlan?.version || false,
                },
            });
            const gatePending = result?.code === "pickup_gate_blocked";
            if (result?.success === false && !gatePending) {
                notify(result?.error || "Could not save pallet count");
                return false;
            }
            if (typeof pickupSetDraftActual === "function") {
                pickupSetDraftActual(stop.id, Number(flow.actual || result?.actual_received_pallet_count || 0));
            }
            await reloadDay();
            S.stop = (S.stops || []).find(entry => entry.id === stop.id) || S.stop;
            if (typeof renderStopList === "function") renderStopList();
            if (typeof renderStopDetail === "function") renderStopDetail();
            if (typeof ensurePickupLoadPlan === "function") {
                try { await ensurePickupLoadPlan(true); } catch (_) {}
            }
            if (typeof renderLoadPlanChip === "function") renderLoadPlanChip();
            notify(gatePending ? "Pallet count saved — continue the pickup steps." : "Pallet count saved");
            if (nextStep && typeof pickupSetStep === "function") pickupSetStep(nextStep);
            else if (typeof renderPickupIntake === "function") renderPickupIntake();
            return true;
        } catch (error) {
            notify(error?.message || "Could not save pallet count");
            return false;
        }
    };

    function deferReasonCode(text) {
        const value = (text || "").toLowerCase();
        if (value.includes("appointment")) return "appointment_later";
        if (value.includes("dock")) return "dock_unavailable";
        if (value.includes("wait")) return "long_wait";
        if (value.includes("dispatch")) return "dispatcher_instructed";
        if (value.includes("open") || value.includes("closed")) return "customer_closed";
        return "other";
    }

    function returnAt(raw) {
        const value = (raw || "").trim().toLowerCase();
        if (!value || ["later", "none", "manual"].includes(value)) return "";
        const now = new Date();
        if (["30", "30m", "30 min", "30 minutes"].includes(value)) return new Date(now.getTime() + 30 * 60000).toISOString();
        if (["60", "1h", "1 hr", "1 hour", "hour"].includes(value)) return new Date(now.getTime() + 60 * 60000).toISOString();
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
        const reminder = window.prompt(
            "When should this stop come back up?\nEnter 30m, 1h, a time like 7:00am, or Later",
            "Later"
        );
        if (reminder === null) return;
        const code = deferReasonCode(reason);
        try {
            const result = await rpc("/dispatch/driver/stop/status", {
                stop_id: stop.id,
                action: "defer",
                data: {
                    reason: code,
                    reason_other: code === "other" ? reason : "",
                    return_at: returnAt(reminder),
                },
            });
            if (!result?.success) return notify(result?.error || "Could not save this stop for later");
            await reloadDay();
            showScreen("sSchedule");
            if (typeof showViewTab === "function") showViewTab("stops");
            notify(result.message || "Stop saved for later — continue to the next stop.");
        } catch (error) {
            notify(error?.message || "Could not save this stop for later");
        }
    }

    function rewriteDeferButtons(root) {
        root?.querySelectorAll?.('[data-v7="defer"]').forEach(button => {
            button.dataset.v7 = "defer-timed";
            button.textContent = "↪ Come Back Later";
        });
    }

    function boot() {
        const app = document.getElementById("app");
        if (!app?.classList.contains("da-app")) return;
        rewriteDeferButtons(app);
        document.addEventListener("click", event => {
            const button = event.target.closest('[data-v7="defer-timed"]');
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
