/* Prema Driver v8 — focused Load Plan enhancement for guided Pickup Step 3.
 *
 * This layer does NOT replace the v7 workflow and does not observe/rewrite the
 * whole Driver App.  It watches only the guided-sheet body/kicker so that the
 * existing v7 render-storm protections remain intact.  Backend load-plan RPCs
 * remain the source of truth.
 */
"use strict";

(function () {
    if (!location.pathname.startsWith("/dispatch/driver")) return;

    let selectedItemId = null;
    let recommendation = null;
    let rendering = false;
    let queued = false;

    const q = (sel) => document.querySelector(sel);
    const escapeHtml = (value) => String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");

    function tell(message) {
        try {
            if (typeof toast === "function") toast(message);
            else console.info(message);
        } catch (_) { console.info(message); }
    }

    function isPickupPositionStep() {
        const kicker = q("#v7GuideKicker");
        return !!kicker && /Pickup\s*·\s*Step\s*3\s*of\s*6/i.test(kicker.textContent || "");
    }

    function displayCode(code) {
        const raw = String(code || "").toUpperCase();
        if (raw === "PW1") return "L-07";
        const m = raw.match(/^([LR])(\d+)$/);
        if (m) return `${m[1]}-${String(Number(m[2])).padStart(2, "0")}`;
        return raw || "—";
    }

    function allPhysicalItems() {
        if (typeof S === "undefined" || !S.loadPlan) return [];
        const all = [
            ...(S.loadPlan.unassigned_items || []),
            ...(S.loadPlan.positions || []).map(p => p.item).filter(Boolean),
        ];
        const seen = new Set();
        return all.filter(item => item && !seen.has(item.id) && seen.add(item.id));
    }

    function positionFor(itemId) {
        return (S.loadPlan?.positions || []).find(p => p.item?.id === Number(itemId)) || null;
    }

    function destinationFor(item) {
        const stops = item?.stops || [];
        if (!stops.length) return "Destination not assigned";
        return stops.map(s => s.customer || `Stop ${s.sequence || ""}`).join(" + ");
    }

    function currentPickupItems() {
        const jobId = S?.stop?.job_id;
        return allPhysicalItems().filter(item => !jobId || item.job_id === jobId);
    }

    function slotByCode(code) {
        return (S.loadPlan?.positions || []).find(p => String(p.position_code || "").toUpperCase() === code) || null;
    }

    function slotHtml(pos) {
        if (!pos) return '<div class="da-v8-slot da-v8-slot-missing">—</div>';
        const item = pos.item;
        const isSelected = item && Number(item.id) === Number(selectedItemId);
        const classes = ["da-v8-slot", item ? "occupied" : "vacant", pos.blocked ? "blocked" : "", isSelected ? "selected" : ""].filter(Boolean).join(" ");
        return `<button type="button" class="${classes}" data-v8="position" data-position="${pos.id}" ${pos.blocked ? "disabled" : ""}>
            <span class="da-v8-slot-code">${escapeHtml(displayCode(pos.position_code))}</span>
            ${item ? `<strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(destinationFor(item))}</small>` : '<small>Open</small>'}
        </button>`;
    }

    function truckLayoutHtml() {
        const rows = [];
        // Front is shown at the top, rear/door at the bottom.  The stored
        // template codes remain L1/R1...PW1; only driver-facing labels change.
        const extra = slotByCode("PW1");
        if (extra) rows.push(`<div class="da-v8-extra-row">${slotHtml(extra)}</div>`);
        for (let n = 6; n >= 1; n--) {
            rows.push(`<div class="da-v8-layout-row">${slotHtml(slotByCode(`L${n}`))}${slotHtml(slotByCode(`R${n}`))}</div>`);
        }
        return `<section class="da-v8-truck">
            <div class="da-v8-truck-front">FRONT / CAB</div>
            ${rows.join("")}
            <div class="da-v8-truck-rear">REAR DOOR / LIFTGATE</div>
        </section>`;
    }

    function palletListHtml() {
        const items = allPhysicalItems();
        if (!items.length) return '<div class="da-v7-empty">No physical pallets are available to position.</div>';
        const currentJobId = S?.stop?.job_id;
        return `<div class="da-v8-pallet-list">${items.map(item => {
            const pos = positionFor(item.id);
            const currentPickup = currentJobId && item.job_id === currentJobId;
            return `<button type="button" class="da-v8-pallet-choice ${Number(item.id) === Number(selectedItemId) ? "selected" : ""}" data-v8="select-item" data-item="${item.id}">
                <span><strong>${escapeHtml(item.name)}</strong>${currentPickup ? '<em>Current pickup</em>' : '<em>Onboard / planned</em>'}</span>
                <span class="da-v8-pallet-dest">${escapeHtml(destinationFor(item))}</span>
                <b>${escapeHtml(pos ? displayCode(pos.position_code) : "Position needed")}</b>
            </button>`;
        }).join("")}</div>`;
    }

    function recommendationHtml() {
        if (!recommendation) return "";
        const moves = recommendation.moves || [];
        const placements = recommendation.new_placements || [];
        const future = recommendation.reserved_future_position_codes || [];
        const warnings = recommendation.warnings || [];
        const noChanges = !moves.length && !placements.length;
        return `<section class="da-v8-recommendation">
            <div class="da-v8-reco-head"><strong>Optimized Load Plan</strong><span>Preview only</span></div>
            <p>${escapeHtml(recommendation.summary || "Review the suggested pallet positions.")}</p>
            ${moves.map(m => `<div class="da-v8-reco-line"><b>${escapeHtml(m.item_name)}</b><span>${escapeHtml(m.from_position_label || displayCode(m.from_position_code))} → ${escapeHtml(m.to_position_label || displayCode(m.to_position_code))}</span><small>${escapeHtml(m.reason || "Improves unload order.")}</small></div>`).join("")}
            ${placements.map(p => `<div class="da-v8-reco-line"><b>${escapeHtml(p.item_name)}</b><span>→ ${escapeHtml(p.position_label || displayCode(p.position_code))}</span><small>${escapeHtml(p.destination || "")}</small></div>`).join("")}
            ${future.length ? `<div class="da-v8-future"><b>Later pickup planning:</b> keep ${escapeHtml(future.join(", "))} open where practical.</div>` : ""}
            ${warnings.map(w => `<div class="da-v7-warning">${escapeHtml(w)}</div>`).join("")}
            <div class="da-v8-reco-actions">
                <button type="button" class="da-v7-btn da-v7-btn-secondary" data-v8="dismiss-recommendation">Keep Manual</button>
                ${noChanges ? "" : '<button type="button" class="da-v7-btn da-v7-btn-primary" data-v8="apply-recommendation">Apply Plan</button>'}
            </div>
        </section>`;
    }

    function plannerHtml() {
        const current = currentPickupItems();
        if (!selectedItemId || !allPhysicalItems().some(i => i.id === Number(selectedItemId))) {
            selectedItemId = (current.find(i => !positionFor(i.id)) || current[0] || allPhysicalItems()[0])?.id || null;
        }
        const selected = allPhysicalItems().find(i => i.id === Number(selectedItemId));
        return `<div id="v8LoadPlanner" class="da-v8-planner">
            <div class="da-v7-note">Choose a pallet, then tap its physical truck position. Earlier deliveries should stay easier to reach from the rear door.</div>
            <div class="da-v8-toolbar">
                <div>${selected ? `Selected: <b>${escapeHtml(selected.name)}</b>` : "Select a pallet"}</div>
                <button type="button" class="da-v7-btn da-v7-btn-secondary" data-v8="optimize">⚡ Optimize Load Plan</button>
            </div>
            ${recommendationHtml()}
            ${truckLayoutHtml()}
            <h3 class="da-v8-section-title">Pallets</h3>
            ${palletListHtml()}
            <div class="da-v8-help">Manual changes save immediately. Optimize gives a preview first and never moves freight until you tap <b>Apply Plan</b>.</div>
        </div>`;
    }

    function enhanceStep3() {
        if (rendering || !isPickupPositionStep() || typeof S === "undefined" || !S.loadPlan) return;
        const body = q("#v7GuideBody");
        if (!body) return;
        rendering = true;
        try {
            const desired = plannerHtml();
            if (body.innerHTML !== desired) body.innerHTML = desired;
        } finally {
            rendering = false;
        }
    }

    function queueEnhance() {
        if (queued) return;
        queued = true;
        requestAnimationFrame(() => requestAnimationFrame(() => {
            queued = false;
            enhanceStep3();
        }));
    }

    async function loadPlanRpc(route, params) {
        if (typeof S === "undefined" || !S.loadPlan) return null;
        try {
            const result = await rpc(route, {
                load_plan_id: S.loadPlan.id,
                version: S.loadPlan.version,
                ...params,
            });
            if (!result || result.success === false) {
                tell(result?.error || "Load Plan action failed.");
                return null;
            }
            return result;
        } catch (error) {
            tell(error?.message || "Load Plan action failed.");
            return null;
        }
    }

    async function placeSelected(positionId) {
        if (!selectedItemId) return tell("Select a pallet first.");
        const target = (S.loadPlan.positions || []).find(p => p.id === Number(positionId));
        if (!target || target.blocked) return;
        const selectedPos = positionFor(selectedItemId);

        let result = null;
        if (target.item && Number(target.item.id) !== Number(selectedItemId)) {
            if (!selectedPos) return tell(`${displayCode(target.position_code)} is occupied. Choose an open position or select that pallet first.`);
            result = await loadPlanRpc("/dispatch/driver/loadplan/swap", {
                item_id_a: Number(selectedItemId),
                item_id_b: Number(target.item.id),
            });
        } else if (selectedPos) {
            if (selectedPos.id === target.id) return;
            result = await loadPlanRpc("/dispatch/driver/loadplan/move", {
                item_id: Number(selectedItemId), position_id: Number(target.id),
            });
        } else {
            result = await loadPlanRpc("/dispatch/driver/loadplan/assign", {
                item_id: Number(selectedItemId), position_id: Number(target.id),
            });
        }

        if (result) {
            S.loadPlan = result;
            recommendation = null;
            if (typeof renderLoadPlanChip === "function") renderLoadPlanChip();
            queueEnhance();
        }
    }

    async function optimize() {
        const result = await loadPlanRpc("/dispatch/driver/loadplan/recommend", {});
        if (!result?.recommendation) return;
        recommendation = result.recommendation;
        queueEnhance();
    }

    async function applyRecommendation() {
        if (!recommendation) return;
        const result = await loadPlanRpc("/dispatch/driver/loadplan/accept_recommendation", {recommendation});
        if (!result) return;
        S.loadPlan = result;
        recommendation = null;
        if (typeof renderLoadPlanChip === "function") renderLoadPlanChip();
        tell("Optimized pallet layout applied.");
        queueEnhance();
    }

    function handleClick(event) {
        const target = event.target.closest("[data-v8]");
        if (!target) return;
        event.preventDefault();
        event.stopPropagation();
        const action = target.dataset.v8;
        if (action === "select-item") {
            selectedItemId = Number(target.dataset.item);
            recommendation = null;
            return queueEnhance();
        }
        if (action === "position") return placeSelected(target.dataset.position);
        if (action === "optimize") return optimize();
        if (action === "apply-recommendation") return applyRecommendation();
        if (action === "dismiss-recommendation") {
            recommendation = null;
            return queueEnhance();
        }
    }

    function boot() {
        const overlay = q("#oGuidedV7");
        const kicker = q("#v7GuideKicker");
        const body = q("#v7GuideBody");
        if (!overlay || !kicker || !body) return;

        document.addEventListener("click", handleClick, true);

        // Narrow observers only.  The kicker observer notices step changes;
        // the body observer notices a legitimate v7 re-render caused by fresh
        // server state.  Our replacement contains #v8LoadPlanner, so its own
        // write settles after one pass and cannot create a mutation storm.
        new MutationObserver(queueEnhance).observe(kicker, {childList: true, subtree: true, characterData: true});
        new MutationObserver(() => {
            if (rendering || !isPickupPositionStep()) return;
            if (!q("#v8LoadPlanner")) queueEnhance();
        }).observe(body, {childList: true});
        queueEnhance();
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, {once: true});
    else boot();
})();
