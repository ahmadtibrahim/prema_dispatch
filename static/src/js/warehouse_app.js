"use strict";
// Minimal Warehouse Loader page. Deliberately smaller than the Driver App —
// no navigation, no stop completion, no chat, no invoices/rates (the
// backend already strips those — see get_load_plan_for_warehouse()).
let WH = { plan: null, selectedCode: null };

async function whRpc(url, params) {
    const r = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include",
        body: JSON.stringify({ jsonrpc: "2.0", method: "call", id: (Math.random() * 1e9) | 0, params }),
    });
    const d = await r.json();
    if (d.error) throw new Error(d.error.data?.message || d.error.message || "RPC error");
    return d.result;
}

function whEsc(s) { return (s || "").toString().replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }

document.addEventListener("DOMContentLoaded", () => {
    const dateInput = document.getElementById("whDate");
    if (dateInput) dateInput.value = new Date().toISOString().slice(0, 10);
});

async function whOpenPlan() {
    const vehicleId = parseInt(document.getElementById("whVehicle").value, 10);
    const date = document.getElementById("whDate").value;
    const body = document.getElementById("whBody");
    body.innerHTML = '<div class="wh-empty">Loading…</div>';
    try {
        const data = await whRpc("/dispatch/warehouse/loadplan/get", { vehicle_id: vehicleId, operating_date: date });
        if (!data || data.success === false) {
            body.innerHTML = `<div class="wh-empty">${whEsc(data?.error || "Could not load plan")}</div>`;
            return;
        }
        WH.plan = data; WH.vehicleId = vehicleId; WH.date = date; WH.selectedCode = null;
        whRender();
    } catch (e) {
        body.innerHTML = `<div class="wh-empty">${whEsc(e.message || "Error")}</div>`;
    }
}
window.whOpenPlan = whOpenPlan;

function whPosClass(pos) {
    if (pos.blocked) return "wh-pos wh-pos-blocked";
    if (WH.selectedCode === pos.position_code) return "wh-pos wh-pos-selected";
    if (!pos.item) return "wh-pos wh-pos-vacant";
    if (pos.item.shared_skid) return "wh-pos wh-pos-shared";
    if (["loaded", "in_transit", "delivered"].includes(pos.item.status)) return "wh-pos wh-pos-loaded";
    return "wh-pos wh-pos-occupied";
}

function whRenderPos(pos) {
    const stops = pos.item?.stops?.length ? `Stop ${whEsc(pos.item.stops.map(s => s.sequence).join("/"))}` : "";
    return `<div class="${whPosClass(pos)}" onclick="whTapPosition('${pos.position_code}')">
        <div class="wh-pos-code">${whEsc(pos.position_code)}</div>
        ${pos.item ? `<div class="wh-pos-item">${whEsc(pos.item.name)}</div>${stops ? `<div class="wh-pos-stops">${stops}</div>` : ""}`
                   : `<div class="wh-pos-vacant">VACANT</div>`}
    </div>`;
}

function whRender() {
    const body = document.getElementById("whBody");
    const lp = WH.plan;
    const driverPos = lp.positions.filter(p => p.side === "driver");
    const passPos = lp.positions.filter(p => p.side === "passenger");
    const selected = lp.positions.find(p => p.position_code === WH.selectedCode);

    let h = `<div class="wh-summary">
        Assigned ${lp.counts.assigned} · Loaded ${lp.counts.loaded} · Vacant ${lp.counts.vacant}
        ${lp.is_locked ? ' · <span class="wh-lock">🔒 LOCKED</span>' : ""}
    </div>`;
    if (!lp.layout_template.is_verified) {
        h += `<div class="wh-unverified">⚠ UNVERIFIED VEHICLE LAYOUT — dimensions/capacity not yet physically verified. Planning aid only.</div>`;
    }
    h += `<div class="wh-label">FRONT / CAB</div><div class="wh-grid">`;
    for (let i = 0; i < Math.max(driverPos.length, passPos.length); i++) {
        h += `<div class="wh-row">`;
        if (driverPos[i]) h += whRenderPos(driverPos[i]);
        if (passPos[i]) h += whRenderPos(passPos[i]);
        h += `</div>`;
    }
    h += `</div><div class="wh-label">REAR DOOR / LIFTGATE</div>`;

    if (selected && selected.item) {
        h += `<div class="wh-detail">
            <b>${whEsc(selected.item.name)}</b> — ${whEsc(selected.item.status)}
            <div class="wh-detail-actions">
                <button class="wh-btn wh-btn-primary" onclick="whMarkLoaded(${selected.item.id})">Mark Loaded</button>
                <button class="wh-btn wh-btn-warn" onclick="whReportException(${selected.item.id})">⚠ Report Damage</button>
            </div>
        </div>`;
    }
    body.innerHTML = h;
}

function whTapPosition(code) {
    WH.selectedCode = (WH.selectedCode === code) ? null : code;
    whRender();
}
window.whTapPosition = whTapPosition;

async function whCall(route, params) {
    try {
        const r = await whRpc(route, { load_plan_id: WH.plan.id, version: WH.plan.version, ...params });
        if (!r || r.success === false) { alert(r?.error || "Action failed"); return null; }
        return r;
    } catch (e) { alert("Error: " + (e.message || "failed")); return null; }
}

async function whMarkLoaded(itemId) {
    const r = await whCall("/dispatch/warehouse/loadplan/mark_loaded", { item_id: itemId });
    if (r) { WH.plan = r; whRender(); }
}
window.whMarkLoaded = whMarkLoaded;

async function whReportException(itemId) {
    const notes = prompt("Describe the damage/issue:");
    if (!notes) return;
    const r = await whCall("/dispatch/warehouse/loadplan/exception", { item_id: itemId, exception_type: "damaged", notes });
    if (r) { WH.plan = r; whRender(); }
}
window.whReportException = whReportException;
