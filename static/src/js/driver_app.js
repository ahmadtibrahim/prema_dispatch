/* ══ Prema Driver App v5 — Production Ready ═══════════════════════ */
"use strict";

// ── State ─────────────────────────────────────────────────────────
const S = {
    weekOffset:0, weekData:null,
    selDate:null, dayData:null, stops:[],
    stop:null,
    finishFlow:null,
    channelId:null, chatOpen:false, chatPoll:null,
    gpsId:null, lat:null, lng:null,
    geoArmed:false, geoTimer:null,
    GEO_M:150, GEO_SEC:15,
    maps:{}, markers:{}, dirSvc:null,
    isSat:true, mapCollapsed:false,
    mapsReady:false, dataLoaded:false,
    refreshPoll:null,
    dragSrcIdx:null,
    navVoiceOn:true, navHeadingMode:false, navFullscreen:false, navTrafficOn:true,
    viewTab:"home",
    _suppressHistoryPush:false,
    uploadState:null,   // {stopId, evType, filename, phase, progress, message, _file} — see runEvidenceUpload()
    loadPlan:null, lpSelectedCode:null,
};

// Local calendar date (NOT toISOString, which converts to UTC — that rolls
// over to "tomorrow" hours before local midnight and made the app show no
// stops in the evening even though today's jobs existed).
const today = () => {
    const d = new Date();
    const p = n => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}`;
};
const isMaps = () => !!window.google?.maps;
const GMAPS_KEY = document.getElementById("app")?.dataset.gmapsKey || "";
const DISPATCH_PHONE = document.getElementById("app")?.dataset.dispatchPhone || "";
const DISPATCH_VOIP_URI = document.getElementById("app")?.dataset.dispatchVoipUri || "";
function streetViewUrl(lat,lng){
    if(!GMAPS_KEY||!lat||!lng)return "";
    return `https://maps.googleapis.com/maps/api/streetview?size=400x200&location=${lat},${lng}&fov=80&key=${GMAPS_KEY}`;
}
function letterLabel(idx){
    let n=idx,s="";
    do{ s=String.fromCharCode(65+(n%26))+s; n=Math.floor(n/26)-1; }while(n>=0);
    return s;
}
function stopCompany(stop){
    return stop.company_name||stop.business_name||stop.partner||stop.city||"";
}
function stopTypeLabel(type){
    return ({
        pickup:"Pickup",
        dropoff:"Drop-off",
        return:"Return",
        transfer:"Driver Transfer",
        cross_dock_drop:"Cross-Dock Drop / Transfer-In",
        cross_dock_pickup:"Cross-Dock Pickup / Transfer-Out",
    })[type]||"Stop";
}
function stopTypeTitle(type){
    return ({
        pickup:"📦 Pickup",
        dropoff:"📬 Drop-off",
        return:"↩ Return",
        transfer:"🤝 Driver Transfer",
        cross_dock_drop:"🏬 Cross-Dock Drop",
        cross_dock_pickup:"🏬 Cross-Dock Pickup",
    })[type]||"📍 Stop";
}
function isPickupStop(type){ return type==="pickup"; }
function isPickupLikeStop(type){ return type==="pickup"||type==="cross_dock_pickup"; }
function isCrossDockStop(type){ return type==="cross_dock_drop"||type==="cross_dock_pickup"; }
function supportsReceivingTruckAssignment(stop){
    return ["transfer","cross_dock_drop"].includes(stop?.type);
}
function availableTransferTrucks(){
    return (S.dayData?.available_transfer_trucks||[]).filter(truck=>truck.driver_name);
}
function transferTruckLabel(truck){
    return [truck.name, truck.plate, truck.driver_name].filter(Boolean).join(" · ");
}
function evidenceTypeForStop(stop){ return isPickupStop(stop?.type) ? "pop" : "pod"; }
function evidenceAttachments(stop){
    return evidenceTypeForStop(stop)==="pop" ? (stop?.pop_attachments||[]) : (stop?.pod_attachments||[]);
}
function proofLabelForStop(stop){
    if(isPickupStop(stop?.type)) return "Proof of Pickup (POP)";
    if(stop?.type==="transfer" || isCrossDockStop(stop?.type)) return "Custody / Transfer Proof";
    return "Proof of Delivery (POD)";
}
function proofRequiredForStop(stop){
    return false;
}
function isClosedStopStatus(status){
    return ["completed","skipped","cancelled","issue"].includes(status);
}
function findStopById(stopId){
    return (S.stops||[]).find(s=>s.id===stopId) || (S.stop?.id===stopId ? S.stop : null);
}
function firstOpenStop(){
    return (S.stops||[]).find(s=>!isClosedStopStatus(s.status));
}
function finishProofOpen(){
    const el=q("#oFinishProof");
    return !!el && el.style.display!=="none";
}
function normalizeCallTarget(raw){
    const value=(raw||"").trim();
    if(!value) return "";
    if (/^(tel|sip):/i.test(value)) return value;
    if (value.includes("@")) return `sip:${value}`;
    return `tel:${value}`;
}
function patchStopState(stopId, patch){
    if(!stopId)return;
    const idx=S.stops.findIndex(s=>s.id===stopId);
    if(idx>=0) S.stops[idx]={...S.stops[idx], ...patch};
    if(S.stop?.id===stopId) S.stop={...S.stop, ...patch};
}

// ── Route options (truck-friendly defaults, persisted per device) ──────────
const RS_KEY="da_route_opts";
function loadRouteOpts(){
    try{
        const saved=JSON.parse(localStorage.getItem(RS_KEY)||"null");
        if(saved) return saved;
    }catch(e){}
    return {tolls:true, highways:false, ferries:true};
}
S.routeOpts=loadRouteOpts();

// ── Boot: DATA loads immediately, Maps loads asynchronously ────────
(async function initData() {
    const initial=parseNavParams();
    S.selDate = initial.date;
    S._suppressHistoryPush = true;
    startGPS();
    try {
        const [wk, day] = await Promise.all([
            rpc("/dispatch/driver/dates",  { week_offset:0 }),
            rpc("/dispatch/driver/stops",  { date_str:S.selDate }),
        ]);
        S.weekOffset=0; S.weekData=wk;
        applyDay(day);
        S.dataLoaded=true;
        showApp();
        // Restore whatever screen/stop the URL pointed at, now that
        // today's (or the requested date's) stops are loaded. A stop id
        // that isn't in the returned list — wrong driver, deleted, wrong
        // date — falls back to the Stops list with a clear message
        // instead of a blank screen.
        if (initial.screen === "sLoadPlan") {
            await openLoadPlan();
        } else if (initial.stopId) {
            const stop = findStopById(initial.stopId);
            if (stop) {
                if (initial.screen === "sNav") openNav(stop); else openStop(stop);
            } else {
                toast("This stop is no longer available.");
                showViewTab(initial.tab);
            }
        } else {
            showViewTab(initial.tab);
        }
        initChat();
        startRealTimePoll();
    } catch(e) {
        console.error(e);
        const el=q("#sLoading");
        if(el) el.innerHTML=`<div class="da-empty">
            <div class="da-empty-icon">⚠️</div>
            <div class="da-empty-title">Could not load route</div>
            <div class="da-empty-sub">${esc(e.message||"Check your connection and refresh")}</div>
            <button class="da-btn da-btn-primary" style="margin-top:16px" onclick="location.reload()">Retry</button>
        </div>`;
    } finally {
        S._suppressHistoryPush = false;
        syncHistory(true);
    }
})();

// Google Maps callback — fires when Maps API is ready
window.__gmReady = function() {
    S.mapsReady=true;
    S.dirSvc=new google.maps.DirectionsService();
    if (S.dataLoaded) {
        initAllMaps();
    }
};

// Fallback: if Maps never loads in 10s, show app without maps
setTimeout(() => {
    if (!S.mapsReady && S.dataLoaded) {
        console.warn("Google Maps not loaded — running without maps");
    }
}, 10000);

document.addEventListener("fullscreenchange", () => {
    setNavFullscreenUI(!!document.fullscreenElement);
});

function setNavFullscreenUI(on){
    S.navFullscreen = !!on;
    q("#navBody")?.classList.toggle("da-nav-body-hidden", S.navFullscreen);
    q("#navMapWrap")?.classList.toggle("da-nav-map-full", S.navFullscreen);
    const btn=q("#navFsBtn");
    if(btn) btn.textContent=S.navFullscreen ? "🗗 Exit Full" : "⛶";
    if(S.maps.nav)setTimeout(()=>trigResize("nav"),200);
}

function showApp() {
    hide("sLoading");
    show("sSchedule");
    renderWeek();
    renderStopList();
    showViewTab("home");
    if (S.mapsReady) initAllMaps();
}

function applyDay(day) {
    S.dayData=day; S.stops=day.stops||[];
    S.loadPlan=null; S.lpSelectedCode=null; // stale for the previous date/truck
    const dn=q("#hDriverName"), tn=q("#hTruckName");
    if(dn) dn.textContent=day.driver_name||"";
    if(tn) tn.textContent=day.truck?.name?"🚛 "+day.truck.name:"";
    loadWeather();
    renderTodaySummary();
    renderLoadPlanChip();
}

function showViewTab(tab){
    S.viewTab=tab;
    const isHome=tab==="home";
    q("#tabHome")?.classList.toggle("active",isHome);
    q("#tabStops")?.classList.toggle("active",!isHome);
    if(q("#todaySummary"))q("#todaySummary").style.display=isHome?"flex":"none";
    if(q("#routeMapWrap"))q("#routeMapWrap").style.display=isHome?"none":"";
    if(q("#stopList"))q("#stopList").style.display=isHome?"none":"block";
    if(!isHome&&S.mapsReady)setTimeout(()=>trigResize("route"),50);
    syncHistory();
}

function renderTodaySummary(){
    const el=q("#todaySummary"); if(!el)return;
    const jobIds=new Set(S.stops.map(s=>s.job_id));
    const r=S.routeSummary;
    el.innerHTML=`
        <div class="da-sum-card"><div class="da-sum-val">${jobIds.size}</div><div class="da-sum-label">Jobs Today</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${S.stops.length}</div><div class="da-sum-label">Stops</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${r?fmtDur(r.totalMin):"—"}</div><div class="da-sum-label">Est. Total Time</div></div>
        <div class="da-sum-card"><div class="da-sum-val">${r?r.km.toFixed(0)+" km":"—"}</div><div class="da-sum-label">Est. Distance</div></div>
    `;
}
function fmtDur(mins){ const h=Math.floor(mins/60),m=Math.round(mins%60); return h?`${h}h ${m}m`:`${m}m`; }

async function loadWeather(){
    const badge=q("#weatherBadge"); if(!badge)return;
    const ref=S.stops.find(s=>s.lat&&s.lng) || (S.dayData?.truck?.lat?S.dayData.truck:null);
    if(!ref){ badge.style.display="none"; return; }
    try{
        const w=await rpc("/dispatch/driver/weather",{lat:ref.lat,lng:ref.lng});
        if(!w?.description){ badge.style.display="none"; return; }
        const precip=(w.precip_percent||0)>=30?` · 💧${w.precip_percent}%`:"";
        badge.innerHTML=`${w.icon_url?`<img src="${w.icon_url}" class="da-weather-icon"/>`:"🌡"} ${Math.round(w.temp_c)}°C · ${esc(w.description)}${precip}`;
        badge.style.display="flex";
    }catch(e){ badge.style.display="none"; }
}

function initAllMaps() {
    initRouteMap();
    if(visibleScreen()==="sNav") initNavMap();
}

// ── Real-time polling (15s fallback) + bus push (instant) ──────────
// refreshRouteNow() is the single fetch-and-apply function used both by
// the 15s safety-net poll below AND by the bus push listener further
// down — the bus push just calls this same function immediately instead
// of waiting for the interval, so there is only one place that knows how
// to fetch/diff/apply the day's stops.
function startRealTimePoll() {
    S.refreshPoll = setInterval(refreshRouteNow, 15000);
    startBusListener();
}

async function refreshRouteNow() {
    try {
        const day = await rpc("/dispatch/driver/stops", { date_str:S.selDate });
        const oldIds = JSON.stringify(S.stops.map(s=>[s.id,s.status,s.sequence]));
        const newIds = JSON.stringify((day.stops||[]).map(s=>[s.id,s.status,s.sequence]));
        if (oldIds !== newIds) {
            applyDay(day);
            renderStopList();
            if (S.stop) {
                const updated = S.stops.find(s=>s.id===S.stop.id);
                if (updated && visibleScreen()==="sStop") { S.stop=updated; renderStopDetail(); }
            }
            toast("🔄 Route updated");
            if (S.mapsReady) initRouteMap();
        }
    } catch(e) {}
}

// ── Real-time bus push (truck assign / unassign / stop changes) ────
// The backend already pushes a bus.bus notification
// ({"type":"route_updated","payload":{"job_id":...}}) to channel
// "driver_route_{driver_partner_id}" whenever a job is assigned/unassigned
// to this driver (prema.dispatch.job write()) or a stop on their route
// changes (prema.dispatch.stop._notify_driver_route_changed) — see
// dispatch_job.py / dispatch_stop.py. There was previously no listener on
// the driver app side (only the 15s poll above covered it). Since this
// file is a plain <script> tag (no OWL bus service available here), we
// connect directly to Odoo's bus websocket endpoint using the same
// {event_name:"subscribe", data:{channels,last}} wire protocol as
// bus/static/src/workers/websocket_worker.js, and just refetch on any
// notification rather than trying to parse/apply the payload ourselves.
let _busWs = null, _busReconnectDelay = 2000, _busLastId = 0, _busChannel = null;
async function startBusListener() {
    try {
        const r = await rpc("/dispatch/driver/bus/channel", {});
        _busChannel = r && r.channel;
        if (_busChannel) connectBus();
    } catch(e) { /* non-fatal — the 15s poll still covers updates */ }
}
function connectBus() {
    if (!_busChannel) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try { _busWs = new WebSocket(`${proto}//${location.host}/websocket`); }
    catch(e) { return; }
    _busWs.addEventListener("open", () => {
        _busReconnectDelay = 2000;
        _busWs.send(JSON.stringify({
            event_name: "subscribe",
            data: { channels: [_busChannel], last: _busLastId },
        }));
    });
    _busWs.addEventListener("message", ev => {
        try {
            const notifications = JSON.parse(ev.data);
            if (!Array.isArray(notifications) || !notifications.length) return;
            _busLastId = notifications[notifications.length - 1].id || _busLastId;
            if (notifications.some(n => n.message && n.message.type === "route_updated")) {
                refreshRouteNow();
            }
        } catch(e) {}
    });
    _busWs.addEventListener("close", scheduleBusReconnect);
    _busWs.addEventListener("error", () => { try { _busWs.close(); } catch(e) {} });
}
function scheduleBusReconnect() {
    _busWs = null;
    _busReconnectDelay = Math.min(_busReconnectDelay * 1.5, 30000);
    setTimeout(connectBus, _busReconnectDelay);
}
function stopBusListener() {
    if (_busWs) { try { _busWs.close(); } catch(e) {} _busWs = null; }
}

// ── Week Calendar ─────────────────────────────────────────────────
const APP = window.APP = {
    prevWeek: async () => {
        S.weekOffset--;
        S.weekData=await rpc("/dispatch/driver/dates",{week_offset:S.weekOffset});
        renderWeek();
    },
    nextWeek: async () => {
        S.weekOffset++;
        S.weekData=await rpc("/dispatch/driver/dates",{week_offset:S.weekOffset});
        renderWeek();
    },
    toggleMap:    () => { S.mapCollapsed=!S.mapCollapsed; q("#routeMapWrap")?.classList.toggle("collapsed",S.mapCollapsed); q("#mapToggleBtn") && (q("#mapToggleBtn").textContent=S.mapCollapsed?"▼":"▲"); if(!S.mapCollapsed&&S.mapsReady)setTimeout(()=>trigResize("route"),280); },
    showHomeTab:  () => showViewTab("home"),
    showStopsTab: () => showViewTab("stops"),
    callDispatch: () => {
        const target=normalizeCallTarget(DISPATCH_VOIP_URI||DISPATCH_PHONE);
        if(!target){toast("No dispatch number or VoIP URI configured");return;}
        window.location.href=target;
    },
    openMapFull:  () => openFullMap(),
    closeMapFull: () => hide("oMapFull"),
    toggleFullSat:() => { S.isSat=!S.isSat; if(S.maps.full)S.maps.full.setMapTypeId(S.isSat?"hybrid":"roadmap"); },
    toggleSat:    () => { S.isSat=!S.isSat; if(S.maps.stop)S.maps.stop.setMapTypeId(S.isSat?"hybrid":"roadmap"); },
    goBack:       () => goBack(),
    openPinEdit:  () => openPinEditor(),
    pinGps:       () => pinGps(),
    pinUseAddress:() => pinUseAddress(),
    pinSave:      () => pinSave(),
    pinClose:     () => hide("oPinEdit"),
    closePhoto:   () => hide("oPhoto"),
    openPhoto:    (url) => { q("#photoImg").src=url; show("oPhoto"); },
    confirmArrived:()=> confirmGeoArrive(),
    dismissGeo:   () => dismissGeo(),
    openChat:     () => openChat(),
    closeChat:    () => closeChat(),
    sendChat:     () => sendChat(),
    pickIssueReason: (r) => pickIssueReason(r),
    sendIssueOther:  () => sendIssueOther(),
    closeIssue:      () => closeIssue(),
    openRouteSettings:  () => openRouteSettings(),
    closeRouteSettings: () => hide("oRouteSettings"),
    saveRouteSettings:  () => saveRouteSettings(),
    closeNav:        () => closeNav(),
    openExternalNav: () => openNativeMaps(S.stop),
    navArrived:      () => navConfirmArrived(),
    toggleNavHeading:() => toggleNavHeading(),
    toggleNavVoice:  () => toggleNavVoice(),
    toggleNavTraffic:() => toggleNavTraffic(),
    toggleNavFullscreen:() => toggleNavFullscreen(),
    recenterNav:     () => { if(S.maps.nav && S.lat && S.lng) S.maps.nav.panTo({lat:S.lat,lng:S.lng}); },
    closeFinishProof:() => closeFinishProof(),
    confirmFinishStop:() => confirmFinishStop(),
    finishNextStop:  () => finishNextStop(),
    finishSchedule:  () => finishSchedule(),
    signOut:      () => { if(confirm("Sign out?")) { clearInterval(S.refreshPoll); stopBusListener(); window.location.href="/web/session/logout?redirect=/dispatch/driver"; }},
};

function renderWeek() {
    const wk=S.weekData; if(!wk)return;
    const lbl=q("#weekLabel"); if(lbl)lbl.textContent=wk.week_label;
    const grid=q("#weekDays"); if(!grid)return;
    grid.innerHTML="";
    wk.days.forEach(d=>{
        const cell=mk("div","da-day-cell"+(d.date===S.selDate?" selected":"")+(d.is_today?" today":"")+(d.is_past?" past":""));
        cell.innerHTML=`<div class="da-day-wd">${d.weekday}</div><div class="da-day-num">${d.day_num}</div><div class="da-day-dot ${d.all_done?"all-done":d.job_count?"has-jobs":""}"></div>`;
        cell.onclick=()=>selectDay(d.date);
        grid.appendChild(cell);
    });
}

async function selectDay(dateStr) {
    if(dateStr===S.selDate)return;
    S.selDate=dateStr; renderWeek(); toast("Loading…");
    try {
        const day=await rpc("/dispatch/driver/stops",{date_str:dateStr});
        applyDay(day); S.maps.route=null;
        showScreen("sSchedule"); renderStopList();
        if(S.mapsReady)initRouteMap(); hide("toast");
    } catch(e){ toast("Could not load stops"); }
}

// ── Stop List ─────────────────────────────────────────────────────
function renderStopList() {
    const list=q("#stopList"); if(!list)return;
    list.innerHTML="";

    if(!S.stops.length){
        list.appendChild(mk("div","da-empty",`<div class="da-empty-icon">📭</div><div class="da-empty-title">No stops today</div><div class="da-empty-sub">Select another day or contact your dispatcher.</div>`));
        return;
    }

    const next=S.stops.find(s=>!["completed","skipped","cancelled"].includes(s.status));
    if(next){
        const banner=mk("div","da-start-banner");
        banner.innerHTML=`<div><div class="da-start-banner-text">Next ${esc(stopTypeTitle(next.type))}</div><div class="da-start-banner-sub">${esc(stopCompany(next))}</div></div><div class="da-start-btn-badge">Go →</div>`;
        banner.onclick=()=>openStop(next);
        list.appendChild(banner);
    }

    const hint=mk("p","");
    hint.style.cssText="font-size:11px;color:#aaa;text-align:center;margin:2px 0 6px;padding:0 12px";
    hint.textContent="Hold and drag ↕ to reorder stops";
    list.appendChild(hint);

    S.stops.forEach((stop,idx)=>{
        const isLast=idx===S.stops.length-1;
        const isPickup=isPickupLikeStop(stop.type);
        const isDone=["completed","skipped","cancelled"].includes(stop.status);
        const isActive=["arrived","en_route"].includes(stop.status);
        const hasPop=stop.pop_attachments?.length>0;
        const hasPod=stop.pod_attachments?.length>0;

        const row=mk("div","da-stop-row"+(isActive?" active-row":""));
        row.draggable=!isDone;
        row.dataset.idx=idx;

        // Touch drag events
        row.addEventListener("dragstart", e=>{S.dragSrcIdx=idx; e.dataTransfer.effectAllowed="move";});
        row.addEventListener("dragover",  e=>{e.preventDefault(); e.dataTransfer.dropEffect="move";});
        row.addEventListener("drop",      e=>{ e.preventDefault(); dropStop(idx); });
        row.addEventListener("dragend",   ()=>{ S.dragSrcIdx=null; });

        const tl=mk("div","da-stop-timeline");
        tl.innerHTML=`<div class="da-dot ${isPickup?"pickup ":""}${stop.status.replace("_route","_route")}"></div>${!isLast?'<div class="da-connector"></div>':""}`;

        const timeStr=stop.actual_arrival_time
            ? `Arrived ${fmtStopTime(stop.actual_arrival_time,stop.tz_name)}`
            : stop.scheduled_time
            ? fmtStopTime(stop.scheduled_time,stop.tz_name)
            : stop.estimated_arrival
            ? `ETA ${fmtStopTime(stop.estimated_arrival,stop.tz_name)}`
            : "";

        const ct=mk("div","da-stop-content");
        ct.innerHTML=
            `<div class="da-stop-type ${isPickup?"pickup":""}">${esc(stopTypeLabel(stop.type))} · <span style="opacity:.65">${esc(stop.job_name)}</span>${timeStr?` · <span class="da-stop-time">${esc(timeStr)}</span>`:""}</div>`+
            `<div class="da-stop-name">${esc(stopCompany(stop))}</div>`+
            `<div class="da-stop-addr">${esc(stop.address)}</div>`+
            (isDone?`<span class="da-stop-badge green">✓ Done${hasPop||hasPod?" 📎":""}</span>`:
             isActive?`<span class="da-stop-badge blue">In Progress</span>`:
             (hasPop||hasPod?`<span class="da-stop-badge">📎 Evidence</span>`:""));
        ct.onclick=()=>openStop(stop);

        const handle=mk("div","");
        handle.style.cssText="color:#ccc;font-size:20px;padding-top:4px;cursor:grab;flex-shrink:0";
        handle.textContent="⠿";

        row.append(tl,ct,handle);
        list.appendChild(row);
    });

    // Progress
    const done=S.stops.filter(s=>["completed","skipped"].includes(s.status)).length;
    if(done>0){
        const sum=mk("p","");
        sum.style.cssText="text-align:center;font-size:12px;color:#888;padding:10px;border-top:1px solid #f0f2f5;margin:4px 0 0";
        sum.textContent=`${done}/${S.stops.length} stops completed`;
        list.appendChild(sum);
    }

    // Job Finished — one row per job whose stops (in today's list) are all done
    const jobIds=[...new Set(S.stops.map(s=>s.job_id).filter(Boolean))];
    for(const jid of jobIds){
        const jobStops=S.stops.filter(s=>s.job_id===jid);
        if(!jobStops.every(s=>["completed","skipped"].includes(s.status)))continue;
        const jobName=jobStops[0]?.job_name||"";
        const row=mk("div","");
        row.style.cssText="margin:8px 10px;padding:10px;border-radius:10px;text-align:center";
        if(jobStops[0]?.job_completed){
            row.style.background="#e8f5e9";row.style.color="#2e7d32";row.style.fontWeight="600";
            row.textContent=`✅ ${jobName} — Job Finished`;
        }else{
            row.style.background="#fff3e0";
            const btn=mk("button","da-btn da-btn-green");
            btn.textContent=`🏁 Finish ${jobName}`;
            btn.onclick=()=>finishJob(jid);
            row.appendChild(btn);
        }
        list.appendChild(row);
    }
}

async function finishJob(jobId){
    try{
        const res=await rpc("/dispatch/driver/job/finish",{job_id:jobId});
        if(res?.success){
            toast(res.completed?"🏁 Job Finished!":"All stops done");
            S.stops.forEach(s=>{ if(s.job_id===jobId) s.job_completed=!!res.completed; });
            renderStopList();
        }else{
            toast(res?.error||"Could not finish job");
        }
    }catch(e){ toast("Could not finish job"); }
}
window.finishJob=finishJob;

async function dropStop(targetIdx) {
    const srcIdx=S.dragSrcIdx;
    if(srcIdx===null||srcIdx===targetIdx)return;
    const reordered=[...S.stops];
    const[moved]=reordered.splice(srcIdx,1);
    reordered.splice(targetIdx,0,moved);

    // Group by job_id
    const jobMap={};
    for(const s of reordered){ (jobMap[s.job_id]=jobMap[s.job_id]||[]).push(s.id); }
    try{
        for(const[jid,ids]of Object.entries(jobMap)){
            await rpc("/dispatch/driver/stop/reorder",{job_id:parseInt(jid),stop_order:ids});
        }
        S.stops=reordered;
        renderStopList();
        if(S.mapsReady)initRouteMap();
        toast("Stop order saved");
    }catch(e){ toast("Reorder failed"); }
}

// ── Route Map ─────────────────────────────────────────────────────
function initRouteMap() {
    const el=q("#routeMap"); if(!el||!isMaps())return;
    el.style.height="200px";
    if(!S.maps.route){
        S.maps.route=new google.maps.Map(el,{
            center:{lat:43.65,lng:-79.38},zoom:10,
            mapTypeId:"roadmap",disableDefaultUI:false,gestureHandling:"greedy",
            fullscreenControl:true,zoomControl:true,mapTypeControl:true,streetViewControl:false
        });
    }
    if(!S.trafficRoute) S.trafficRoute=new google.maps.TrafficLayer();
    S.trafficRoute.setMap(S.navTrafficOn ? S.maps.route : null);
    const pts=S.stops.filter(s=>s.lat&&s.lng);
    drawStopsOnMap(S.maps.route,pts,true);
}

function drawStopsOnMap(map,stops,withRoute) {
    (S.markers[map.getDiv().id]||[]).forEach(m=>m.setMap(null));
    S.markers[map.getDiv().id]=[];
    if(!stops.length)return;
    const bounds=new google.maps.LatLngBounds();
    stops.forEach((s,i)=>{
        const isDone=["completed","skipped"].includes(s.status);
        const m=new google.maps.Marker({
            position:{lat:s.lat,lng:s.lng},map,
            label:{text:letterLabel(i),color:"#fff",fontSize:"10px",fontWeight:"700"},
            icon:{path:google.maps.SymbolPath.CIRCLE,scale:13,
                  fillColor:isDone?"#9e9e9e":s.type==="cross_dock_drop"?"#f4b400":isPickupLikeStop(s.type)?"#34a853":"#1565C0",
                  fillOpacity:1,strokeColor:"#fff",strokeWeight:2}
        });
        S.markers[map.getDiv().id].push(m);
        bounds.extend({lat:s.lat,lng:s.lng});
    });

    if(withRoute&&stops.length>=2&&S.dirSvc){
        const rend=new google.maps.DirectionsRenderer({suppressMarkers:true,
            polylineOptions:{strokeColor:"#1565C0",strokeWeight:4,strokeOpacity:.85}});
        rend.setMap(map);
        S.dirSvc.route({
            origin:{lat:stops[0].lat,lng:stops[0].lng},
            destination:{lat:stops[stops.length-1].lat,lng:stops[stops.length-1].lng},
            waypoints:stops.slice(1,-1).map(s=>({location:{lat:s.lat,lng:s.lng},stopover:true})),
            travelMode:google.maps.TravelMode.DRIVING,
            avoidTolls:S.routeOpts.tolls,avoidHighways:S.routeOpts.highways,avoidFerries:S.routeOpts.ferries
        },(r,st)=>{
            if(st==="OK"){
                rend.setDirections(r);
                if(r.routes[0]?.bounds)map.fitBounds(r.routes[0].bounds,30);
                const legs=r.routes[0]?.legs||[];
                const driveMin=Math.round(legs.reduce((s,l)=>s+(l.duration?.value||0),0)/60);
                const km=legs.reduce((s,l)=>s+(l.distance?.value||0),0)/1000;
                const svcMin=stops.reduce((s,st2)=>s+(st2.service_time_min||15),0);
                S.routeSummary={driveMin,km,totalMin:driveMin+svcMin};
                renderTodaySummary();
            }
            else map.fitBounds(bounds,40);
        });
    } else if(stops.length){
        map.fitBounds(bounds,40);
    }
}

// ── Stop Detail ───────────────────────────────────────────────────
function openStop(stop) {
    S.stop=stop;
    const title=q("#stopDetailTitle");
    if(title) title.textContent=stopTypeTitle(stop.type);
    showScreen("sStop");
    renderStopDetail();
    if(S.mapsReady) initStopMap(stop);
    armGeo(stop);
}

function renderStopTimeLine(stop){
    const tz=stop.tz_name||"America/Toronto";
    const parts=[];
    if(stop.scheduled_time) parts.push(`Scheduled ${fmtStopTime(stop.scheduled_time,tz)}`);
    if(stop.estimated_arrival && !stop.actual_arrival_time) parts.push(`ETA ${fmtStopTime(stop.estimated_arrival,tz)}`);
    if(stop.actual_arrival_time) parts.push(`Arrived ${fmtStopTime(stop.actual_arrival_time,tz)}`);
    if(stop.actual_departure_time) parts.push(`Departed ${fmtStopTime(stop.actual_departure_time,tz)}`);
    if(!parts.length) return "";
    return `<div class="da-detail-time">🕐 ${esc(parts.join(" · "))}</div>`;
}

function renderStopDetail() {
    const stop=S.stop;
    const body=q("#stopDetailBody"); if(!body)return;
    const isPickup=isPickupStop(stop.type);
    const isDone=["completed","skipped","cancelled"].includes(stop.status);
    const isActive=["arrived","en_route"].includes(stop.status);
    const phone=stop.contact_phone||"";

    body.innerHTML=
        `<div class="da-detail-info">`+
        `<div class="da-detail-ref">🗂 ${esc(stop.job_name)}</div>`+
        `<div class="da-detail-type ${isPickupLikeStop(stop.type)?"pickup":""}">${esc(stopTypeLabel(stop.type))}</div>`+
        `<div class="da-detail-name">${esc(stopCompany(stop))}</div>`+
        `<div class="da-detail-addr">${esc(stop.address)}</div>`+
        (stop.address_warning?`<div class="da-addr-warn">⚠ ${esc(stop.address_warning)}</div>`:"")+
        (renderStopTimeLine(stop))+
        (stop.type==="pickup"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_in||"?"} pallets</strong> to pick up${stop.pallets_in_estimated?' <span class="da-est-badge" title="Estimated from downstream deliveries">✨ est.</span>':""}</div>`
            :stop.type==="cross_dock_pickup"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_in||"?"} pallets</strong> to reload from the cross-dock</div>`
            :stop.type==="cross_dock_drop"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_out||"?"} pallets</strong> to stage at the cross-dock</div>`
            :stop.type==="transfer"
            ?`<div class="da-detail-meta">📦 <strong>${stop.pallets_out||"?"} pallets</strong> to hand off / stage</div>`
            :`<div class="da-detail-meta">📦 <strong>${stop.pallets_out||"?"} pallets</strong> to deliver</div>`)+
        renderFreightItems(stop)+
        (!isDone?`<div class="da-detail-svc">⏱ Service time:
            <button class="da-svc-btn" onclick="bumpSvcTime(-5)">−</button>
            <span class="da-svc-val">${stop.service_time_min||15}m</span>
            <button class="da-svc-btn" onclick="bumpSvcTime(5)">+</button>
        </div>`:"")+
        (stop.dock_door?`<div class="da-detail-dock">🚪 Dock: ${esc(stop.dock_door)}</div>`:"")+
        (phone?`<a href="tel:${esc(phone)}" class="da-phone-link">📞 ${esc(stop.contact_name||stop.partner||phone)}</a>`:"")+
        (stop.parking_notes?`<div class="da-detail-notes">🅿️ ${esc(stop.parking_notes)}</div>`:"")+
        (stop.entrance_photo_url?`<button class="da-photo-btn" onclick="APP.openPhoto('${stop.entrance_photo_url}')">📷 Entrance Photo</button>`:
            (streetViewUrl(stop.lat,stop.lng)?`<div class="da-streetview-wrap">
                <img src="${streetViewUrl(stop.lat,stop.lng)}" class="da-streetview-img" loading="lazy" alt="Street View"
                     onclick="APP.openPhoto('${streetViewUrl(stop.lat,stop.lng)}')"/>
                <div class="da-streetview-label">📍 Street View (no entrance photo saved yet)</div>
            </div>`:""))+
        `</div>`+
        renderTransitEvidence(stop)+
        renderEvidence(stop)+
        renderActions(stop,isDone,isActive);
}

function renderFreightItems(stop){
    const items=stop.freight_items||[];
    if(!items.length) return "";
    return `<div class="da-detail-items">
        <div class="da-detail-items-title">📦 Transit Pallets / Freight</div>
        <div class="da-detail-items-list">${items.map(item=>`
            <div class="da-detail-item-chip">${esc(item.label)}</div>
        `).join("")}</div>
    </div>`;
}

function renderTransitEvidence(stop){
    const items=stop.transit_evidence||[];
    if(!items.length) return "";
    return `<div class="da-evidence-section da-evidence-history">
        <div class="da-evidence-title">🧾 Transit / Transfer Evidence Already On This Freight</div>
        <div class="da-evidence-list">${items.map(att=>`
            <div class="da-evidence-item">
                <span class="da-ev-name">${esc(att.name)}</span>
                <a href="${esc(att.url)}" target="_blank" class="da-ev-view">View</a>
            </div>
        `).join("")}</div>
    </div>`;
}

function renderReceivingTruckSection(stop,isDone){
    if(!supportsReceivingTruckAssignment(stop)) return "";
    const options=availableTransferTrucks();
    const selected=String(stop.transfer_to_vehicle_id||"");
    const blankLabel=stop.type==="cross_dock_drop"
        ? "Keep remaining route on this truck"
        : "Leave blank to stage / unassign at handoff";
    const hint=stop.type==="cross_dock_drop"
        ? (isDone
            ? "Assign the receiving truck now to move the remaining route off this truck."
            : "Choose another truck now if the staged freight should continue on a different truck after this cross-dock drop.")
        : (isDone
            ? "Assign the receiving truck now if this staged transfer is ready to continue."
            : "Choose the receiving truck now, or leave it blank and this transfer will stage / unassign when you finish it.");
    return `<div class="da-transfer-assign">
        <div class="da-transfer-assign-title">Receiving Truck</div>
        <div class="da-transfer-assign-hint">${esc(hint)}</div>
        <select class="da-select" onchange="setReceivingTruckSelection(this.value)">
            <option value="">${esc(blankLabel)}</option>
            ${options.map(truck=>`
                <option value="${truck.id}" ${selected===String(truck.id)?"selected":""}>${esc(transferTruckLabel(truck))}</option>
            `).join("")}
        </select>
        <div class="da-btn-row">
            <button class="da-btn da-btn-secondary" onclick="applyReceivingTruck(false)">${isDone?"Transfer Remaining Route":"Save Receiving Truck"}</button>
            <button class="da-btn da-btn-ghost" onclick="clearReceivingTruckSelection()">Clear Target</button>
        </div>
        ${isDone?`<button class="da-btn da-btn-orange" onclick="applyReceivingTruck(true)">Stage / Unassign Remaining Route</button>`:""}
    </div>`;
}

const EVIDENCE_IMAGE_ACCEPT="image/jpeg,image/png,image/heic,image/heif";
const EVIDENCE_PDF_ACCEPT="application/pdf";

function renderEvidence(stop){
    const isPickup=isPickupStop(stop.type);
    const isCrossDock=isCrossDockStop(stop.type);
    const evType=isPickup?"pop":"pod";
    const evLabel=isPickup
        ?"Proof of Pickup (POP)"
        :isCrossDock
        ?"Custody / Transfer Proof"
        :"Proof of Delivery (POD)";
    const atts=isPickup?(stop.pop_attachments||[]):(stop.pod_attachments||[]);
    const isDone=["completed","skipped","cancelled"].includes(stop.status);
    const busy=S.uploadState && S.uploadState.stopId===stop.id && S.uploadState.evType===evType
        && ["preparing","uploading"].includes(S.uploadState.phase);

    let h=`<div class="da-evidence-section"><div class="da-evidence-title">📎 ${evLabel}</div><div class="da-evidence-list">`;
    atts.forEach(a=>{
        h+=`<div class="da-evidence-item">
            <span class="da-ev-name">${esc(a.name)}</span>
            <a href="${esc(a.url)}" target="_blank" class="da-ev-view">View</a>
            ${!isDone?`<button class="da-ev-del" onclick="delEv(${stop.id},'${evType}',${a.id})">✕</button>`:""}
        </div>`;
    });
    if(!atts.length) h+=`<div class="da-ev-empty">No evidence yet</div>`;
    h+=`</div>`;
    if(!isDone){
        h+=`<div class="da-evidence-btns" data-stop="${stop.id}" data-evtype="${evType}">
            <label class="da-btn da-btn-secondary da-btn-sm da-ev-action-btn" ${busy?"disabled":""}>📷 Take Photo
                <input type="file" accept="${EVIDENCE_IMAGE_ACCEPT}" capture="camera" style="display:none" ${busy?"disabled":""}
                       onchange="pickEvidenceFile(${stop.id},'${evType}',this)">
            </label>
            <label class="da-btn da-btn-secondary da-btn-sm da-ev-action-btn" ${busy?"disabled":""}>🖼️ Choose Photo
                <input type="file" accept="${EVIDENCE_IMAGE_ACCEPT}" style="display:none" ${busy?"disabled":""}
                       onchange="pickEvidenceFile(${stop.id},'${evType}',this)">
            </label>
            <label class="da-btn da-btn-secondary da-btn-sm da-ev-action-btn" ${busy?"disabled":""}>📄 Upload PDF
                <input type="file" accept="${EVIDENCE_PDF_ACCEPT}" style="display:none" ${busy?"disabled":""}
                       onchange="pickEvidenceFile(${stop.id},'${evType}',this)">
            </label>
            <button class="da-btn da-btn-secondary da-btn-sm da-ev-action-btn" ${busy?"disabled":""} onclick="openScanner(${stop.id},'${evType}')">📄 Scan Doc</button>
        </div>
        <div id="evStatusRow-${stop.id}-${evType}" class="da-ev-status-row" style="display:none"></div>
        <div class="da-evidence-hint">Take Photo uses the camera directly; Choose Photo picks from your library.${evType==="pod" ? " Camera photos are stamped with date, time, and GPS." : ""}</div>`;
    }
    h+=`</div>`;
    if(S.uploadState && S.uploadState.stopId===stop.id && S.uploadState.evType===evType && S.uploadState.phase!=="idle"){
        // Render the initial status immediately (subsequent progress ticks
        // update the row in place without a full re-render — see
        // renderUploadStatus()).
        setTimeout(()=>renderUploadStatus(stop.id,evType),0);
    }
    return h;
}

function renderUploadStatus(stopId,evType){
    const row=q(`#evStatusRow-${stopId}-${evType}`);
    const st=S.uploadState;
    if(!row||!st||st.stopId!==stopId||st.evType!==evType||st.phase==="idle"){ if(row)row.style.display="none"; return; }
    row.style.display="flex";
    const parts=[`<span class="da-ev-status-file">${esc(st.filename||"")}</span>`];
    if(st.phase==="preparing") parts.push(`<span class="da-ev-status-msg">Preparing file…</span>`);
    else if(st.phase==="uploading") parts.push(`<span class="da-ev-status-msg">Uploading ${st.progress||0}%…</span><progress class="da-ev-progress" max="100" value="${st.progress||0}"></progress>`);
    else if(st.phase==="success") parts.push(`<span class="da-ev-status-msg da-ev-status-ok">✓ Upload complete</span>`);
    else if(st.phase==="duplicate") parts.push(`<span class="da-ev-status-msg da-ev-status-ok">✓ ${esc(st.message||"Already uploaded")}</span>`);
    else if(st.phase==="failed") parts.push(
        `<span class="da-ev-status-msg da-ev-status-err">✕ ${esc(st.message||"Upload failed")}</span>`+
        `<button class="da-btn da-btn-secondary da-btn-sm" onclick="retryEvidenceUpload(${stopId},'${evType}')">Retry</button>`+
        `<button class="da-btn da-btn-ghost da-btn-sm" onclick="cancelEvidenceUpload(${stopId},'${evType}')">Dismiss</button>`
    );
    row.innerHTML=parts.join("");
}

function renderFinishProof(){
    const body=q("#finishProofBody");
    const title=q("#finishProofTitle");
    if(!body || !title) return;
    const stop=findStopById(S.finishFlow?.stopId) || S.stop;
    if(!stop){
        title.textContent="Stop Completion";
        body.innerHTML=`<div class="da-finish-empty">This stop is no longer available.</div>
            <div class="da-finish-actions">
                <button class="da-btn da-btn-primary" onclick="APP.finishSchedule()">Back to Schedule</button>
            </div>`;
        return;
    }

    const atts=evidenceAttachments(stop);
    const needsProof=proofRequiredForStop(stop);
    const summary=`<div class="da-finish-summary">
        <div class="da-finish-stop-type">${esc(stopTypeLabel(stop.type))}</div>
        <div class="da-finish-stop-name">${esc(stopCompany(stop))}</div>
        <div class="da-finish-stop-addr">${esc(stop.address||"")}</div>
        <div class="da-finish-stop-meta">Pallets: <strong>${stop.pallets_in || stop.pallets_out || 0}</strong>${stop.freight_item_summary?` · ${esc(stop.freight_item_summary)}`:""}</div>
    </div>`;

    if(S.finishFlow?.completed){
        const next=findStopById(S.finishFlow.nextStopId) || firstOpenStop();
        title.textContent="Stop Completed";
        body.innerHTML=
            `${summary}
            <div class="da-finish-note">✅ ${esc(stopCompany(stop) || stop.address || "Stop")} was marked complete. Uploaded proof for this stop has already been copied to the linked invoice.</div>`+
            (next
                ?`<div class="da-finish-next-card">
                    <div class="da-finish-next-label">Next stop ready</div>
                    <div class="da-finish-next-name">${esc(stopCompany(next) || stopTypeLabel(next.type))}</div>
                    <div class="da-finish-next-addr">${esc(next.address || "")}</div>
                    <div class="da-finish-next-type">${esc(stopTypeLabel(next.type))}</div>
                </div>
                <div class="da-finish-actions">
                    <button class="da-btn da-btn-secondary" onclick="APP.finishSchedule()">Back to Schedule</button>
                    <button class="da-btn da-btn-green" onclick="APP.finishNextStop()">🗺️ Navigate to Next Stop</button>
                </div>`
                :`<div class="da-finish-note">🎉 No remaining open stops on this route.</div>
                <div class="da-finish-actions">
                    <button class="da-btn da-btn-primary" onclick="APP.finishSchedule()">Back to Schedule</button>
                </div>`);
        return;
    }

    title.textContent=proofLabelForStop(stop);
    body.innerHTML=
        `${summary}
        <div class="da-finish-note">${needsProof
            ?`${esc(proofLabelForStop(stop))} is required before this stop can be finished.`
            :`Add ${esc(proofLabelForStop(stop))} now if needed. Any photo or scanned document uploaded here is copied to the linked invoice automatically.`}
        </div>
        ${renderEvidence(stop)}
        <div class="da-finish-actions">
            <button class="da-btn da-btn-ghost" onclick="APP.closeFinishProof()">Cancel</button>
            <button class="da-btn ${needsProof && !atts.length ? "da-btn-ghost" : "da-btn-primary"}" ${needsProof && !atts.length ? "disabled" : ""} onclick="APP.confirmFinishStop()">Finish Stop</button>
        </div>`;
}

function openFinishProof(){
    if(!S.stop) return;
    S.finishFlow={stopId:S.stop.id, completed:false, nextStopId:false};
    renderFinishProof();
    show("oFinishProof");
}

function closeFinishProof(){
    hide("oFinishProof");
    S.finishFlow=null;
}

async function confirmFinishStop(){
    const flow=S.finishFlow;
    const stop=findStopById(flow?.stopId) || S.stop;
    if(!stop) return;
    const ok=await callStop(stop.id,"completed",{});
    if(!ok) return;
    patchStopState(stop.id,{status:"completed",actual_departure_time:new Date().toISOString()});
    toast("Stop completed ✅");
    await reloadDay();
    const updated=findStopById(stop.id);
    S.stop=updated || {...stop, status:"completed"};
    renderStopList();
    renderStopDetail();
    S.finishFlow={
        stopId:stop.id,
        completed:true,
        nextStopId:firstOpenStop()?.id || false,
    };
    renderFinishProof();
}

async function finishNextStop(){
    const next=findStopById(S.finishFlow?.nextStopId) || firstOpenStop();
    closeFinishProof();
    if(!next){
        toast("🎉 All stops complete!");
        showScreen("sSchedule");
        renderStopList();
        return;
    }
    S.stop=next;
    await callStop(next.id,"en_route",{});
    patchStopState(next.id,{status:"en_route"});
    renderStopList();
    openNav(next);
}

function finishSchedule(){
    closeFinishProof();
    showScreen("sSchedule");
    renderStopList();
}

function renderActions(stop,isDone,isActive){
    if(isDone) return `<div class="da-detail-actions"><div class="da-done-card">✅ Completed</div>${renderReceivingTruckSection(stop,true)}<div class="da-btn-row"><button class="da-btn da-btn-ghost" onclick="APP.goBack()">← Back</button><button class="da-btn da-btn-secondary" onclick="doRestoreStop()">↺ Restore</button></div></div>`;
    if(stop.type==="transfer"){
        const hasProof=(stop.pod_attachments||[]).length>0;
        return `<div class="da-detail-actions">
            <div class="da-transfer-note">${stop.transfer_to_vehicle
                ?`🤝 Transferring the selected freight to <strong>${esc(stop.transfer_to_driver||"another driver")}</strong> on <strong>${esc(stop.transfer_to_vehicle)}</strong>.`
                :`📍 This transfer will unassign the selected freight and stage it at this meet point until another truck reloads it.`}
                ${hasProof ? "Custody proof has been added." : "Custody proof is optional."}
            </div>
            ${renderReceivingTruckSection(stop,false)}
            ${!isActive
                ?`<button class="da-btn da-btn-nav" onclick="doNavigate()">🗺️ Navigate to Handoff Point</button>
                  <div class="da-btn-row"><button class="da-btn da-btn-green" onclick="doArrived()">✓ I'm Here</button><button class="da-btn da-btn-orange" onclick="doDelayed()">⚠ Issue</button></div>`
                :`<button class="da-btn da-btn-green" onclick="doExecuteTransfer()">✓ Finish Transfer</button>
                  <div class="da-btn-row"><button class="da-btn da-btn-secondary" onclick="doNavigate()">🗺️ Open Map Again</button><button class="da-btn da-btn-orange" onclick="doDelayed()">⚠ Report Issue</button></div>`}
            <button class="da-btn da-btn-ghost" onclick="doRestoreStop()">↺ Restore Stop</button>
        </div>`;
    }
    if(isCrossDockStop(stop.type)){
        const hasProof=(stop.pod_attachments||[]).length>0;
        const isDrop=stop.type==="cross_dock_drop";
        return `<div class="da-detail-actions">
            <div class="da-transfer-note">${isDrop
                ?`🏬 This is a temporary cross-dock unload, not final delivery. ${hasProof ? "Custody proof has been added." : "Custody proof is optional."}`
                :`🏬 This is a cross-dock reload / transfer-out, not the original shipper pickup. ${hasProof ? "Custody proof has been added." : "Custody proof is optional."}`}
            </div>
            ${isDrop ? renderReceivingTruckSection(stop,false) : ""}
            ${!isActive
                ?`<button class="da-btn da-btn-nav" onclick="doNavigate()">🗺️ Navigate to Cross-Dock</button>
                  <div class="da-btn-row"><button class="da-btn da-btn-green" onclick="doArrived()">✓ Arrived</button><button class="da-btn da-btn-orange" onclick="doDelayed()">⚠ Issue</button></div>`
                :`<button class="da-btn da-btn-green" onclick="doComplete()">✅ Finish ${isDrop?"Cross-Dock Drop":"Cross-Dock Pickup"}</button>
                  <div class="da-btn-row"><button class="da-btn da-btn-secondary" onclick="doNavigate()">🗺️ Open Map Again</button><button class="da-btn da-btn-orange" onclick="doDelayed()">⚠ Report Issue</button></div>`}
            <button class="da-btn da-btn-ghost" onclick="doRestoreStop()">↺ Restore Stop</button>
        </div>`;
    }
    return `<div class="da-detail-actions">
        <button class="da-btn da-btn-nav" onclick="doNavigate()">🗺️ Navigate — Truck Route</button>
        ${!isActive
            ?`<div class="da-btn-row"><button class="da-btn da-btn-green" onclick="doArrived()">✓ Arrived</button><button class="da-btn da-btn-orange" onclick="doDelayed()">⚠ Issue</button></div>`
            :`<button class="da-btn da-btn-green" onclick="doComplete()">✅ Complete &amp; Next Stop</button><button class="da-btn da-btn-orange" onclick="doDelayed()">⚠ Report Issue</button>`}
        <div class="da-btn-row">
            <button class="da-btn da-btn-secondary" onclick="APP.openPinEdit()">📍 Edit Pin</button>
            <button class="da-btn da-btn-secondary" onclick="doRestoreStop()">↺ Restore</button>
            <button class="da-btn da-btn-ghost" onclick="doSkip()">↩ Skip</button>
        </div>
        ${stop.status==="pending"?`<button class="da-btn da-btn-ghost da-btn-del" onclick="doDeleteStop()">🗑 Delete Stop</button>`:""}
    </div>`;
}

// ── Evidence ──────────────────────────────────────────────────────
function readFileAsDataUrl(file){
    return new Promise((resolve,reject)=>{
        const reader=new FileReader();
        reader.onload=e=>resolve(e.target.result);
        reader.onerror=()=>reject(reader.error || new Error("Could not read file"));
        reader.readAsDataURL(file);
    });
}

function loadImageFromDataUrl(dataUrl){
    return new Promise((resolve,reject)=>{
        const img=new Image();
        img.onload=()=>resolve(img);
        img.onerror=()=>reject(new Error("Could not load image"));
        img.src=dataUrl;
    });
}

function resolveStampCoords(stop){
    const lat=Number.isFinite(S.lat) ? S.lat : stop?.lat;
    const lng=Number.isFinite(S.lng) ? S.lng : stop?.lng;
    return {
        lat: Number.isFinite(lat) ? lat : null,
        lng: Number.isFinite(lng) ? lng : null,
    };
}

function stampedEvidenceFilename(name){
    const base=(name||"evidence").replace(/\.[^.]+$/, "");
    return `${base}_stamped.jpg`;
}

async function maybeBuildStampedEvidence(stopId, evType, file){
    const fallback=await readFileAsDataUrl(file);
    const original={filename:file.name, data_b64:fallback.split(",")[1]};
    const isImage=/^image\//.test(file.type || "") || /\.(jpe?g|png|webp|heic|heif)$/i.test(file.name || "");
    if(evType!=="pod" || !isImage) return original;

    try{
        const stop=findStopById(stopId) || S.stop;
        const image=await loadImageFromDataUrl(fallback);
        const canvas=document.createElement("canvas");
        canvas.width=image.naturalWidth || image.width;
        canvas.height=image.naturalHeight || image.height;
        const ctx=canvas.getContext("2d");
        ctx.drawImage(image,0,0,canvas.width,canvas.height);

        const now=new Date();
        const dateText=new Intl.DateTimeFormat("en-CA",{
            year:"numeric",month:"2-digit",day:"2-digit"
        }).format(now);
        const timeText=new Intl.DateTimeFormat("en-CA",{
            hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false
        }).format(now);
        const coords=resolveStampCoords(stop);
        const gpsText=(coords.lat!==null && coords.lng!==null)
            ?`Lat ${coords.lat.toFixed(6)}  Lng ${coords.lng.toFixed(6)}`
            :"GPS unavailable";

        const inset=Math.max(18, Math.round(Math.min(canvas.width, canvas.height) * 0.05));
        const pad=Math.max(10, Math.round(canvas.width * 0.012));
        let fontSize=Math.max(18, Math.round(canvas.width * 0.022));
        const lines=[dateText, timeText, gpsText];
        const maxBoxWidth=Math.round(canvas.width * 0.52);
        let metricsWidth=0;
        do{
            ctx.font=`700 ${fontSize}px "Segoe UI", Arial, sans-serif`;
            metricsWidth=Math.max(...lines.map(line=>ctx.measureText(line).width));
            if(metricsWidth <= (maxBoxWidth - (pad * 2)) || fontSize <= 12) break;
            fontSize -= 1;
        }while(fontSize > 12);
        const lineHeight=Math.round(fontSize * 1.24);
        const boxWidth=Math.min(maxBoxWidth, Math.ceil(metricsWidth + (pad * 2)));
        const boxHeight=(lineHeight * lines.length) + (pad * 2);
        const boxX=inset;
        const boxY=canvas.height - boxHeight - inset;

        ctx.fillStyle="rgba(0,0,0,0.58)";
        ctx.fillRect(boxX, boxY, boxWidth, boxHeight);
        ctx.strokeStyle="rgba(255,255,255,0.25)";
        ctx.lineWidth=Math.max(1, Math.round(fontSize * 0.08));
        ctx.strokeRect(boxX, boxY, boxWidth, boxHeight);
        ctx.fillStyle="#ffffff";
        ctx.textBaseline="top";
        lines.forEach((line, idx)=>{
            ctx.fillText(line, boxX + pad, boxY + pad + (idx * lineHeight));
        });

        return {
            filename: stampedEvidenceFilename(file.name),
            data_b64: canvas.toDataURL("image/jpeg", 0.92).split(",")[1],
        };
    }catch(err){
        console.warn("POD stamp failed, uploading original image", err);
        return original;
    }
}

// ── Evidence upload state machine (idle -> selected -> preparing ->
// uploading -> success|duplicate|failed) ────────────────────────────
// Only one upload is tracked at a time (S.uploadState) — matches the
// existing app's single-active-screen style rather than introducing a
// queue. The file is kept on the state object so Retry doesn't require
// re-picking it, and is cleared only on confirmed success/duplicate or
// an explicit Dismiss.
function pickEvidenceFile(stopId,evType,input){
    const file=input.files[0]; if(!file)return;
    input.value=""; // safe to clear the <input> immediately — the File object itself is retained on S.uploadState, not re-read from this input
    if(S.uploadState && ["preparing","uploading"].includes(S.uploadState.phase)) return; // guard: an upload is already active for this stop/type
    S.uploadState={stopId,evType,filename:file.name,phase:"selected",progress:0,message:"",_file:file};
    if(S.stop?.id===stopId) renderStopDetail();
    runEvidenceUpload(stopId,evType);
}
window.pickEvidenceFile=pickEvidenceFile;

function retryEvidenceUpload(stopId,evType){
    if(!S.uploadState||S.uploadState.stopId!==stopId||S.uploadState.evType!==evType||!S.uploadState._file)return;
    S.uploadState.phase="selected"; S.uploadState.progress=0; S.uploadState.message="";
    runEvidenceUpload(stopId,evType);
}
window.retryEvidenceUpload=retryEvidenceUpload;

function cancelEvidenceUpload(stopId,evType){
    if(S.uploadState&&S.uploadState.stopId===stopId&&S.uploadState.evType===evType){
        S.uploadState=null;
        if(S.stop?.id===stopId) renderStopDetail();
    }
}
window.cancelEvidenceUpload=cancelEvidenceUpload;

async function runEvidenceUpload(stopId,evType){
    const st=S.uploadState;
    if(!st||st.stopId!==stopId||st.evType!==evType)return;
    const file=st._file;
    st.phase="preparing"; st.progress=0; st.message="Preparing file…";
    renderUploadStatus(stopId,evType);
    try{
        const payload=await maybeBuildStampedEvidence(stopId, evType, file);
        if(!S.uploadState||S.uploadState!==st)return; // superseded (dismissed/new pick) while preparing
        st.phase="uploading"; st.progress=0;
        renderUploadStatus(stopId,evType);
        const r=await rpcWithProgress("/dispatch/driver/evidence/add",{
            stop_id:stopId, ev_type:evType,
            data_b64:payload.data_b64, filename:payload.filename,
        }, pct=>{
            if(S.uploadState!==st)return;
            st.progress=pct;
            renderUploadStatus(stopId,evType);
        });
        if(S.uploadState!==st)return; // dismissed/replaced while in flight
        if(r?.success){
            const stop=S.stops.find(s=>s.id===stopId);
            if(stop && !r.duplicate){
                const key=evType==="pop"?"pop_attachments":"pod_attachments";
                (stop[key]=stop[key]||[]).push({id:r.id,name:r.name,url:r.url});
            }
            st.phase=r.duplicate?"duplicate":"success";
            st.message=r.duplicate?(r.message||"Already uploaded"):"Upload complete";
            if(S.stop?.id===stopId) renderStopDetail(); else renderUploadStatus(stopId,evType);
            if(finishProofOpen() && S.finishFlow?.stopId===stopId) renderFinishProof();
            setTimeout(()=>{ if(S.uploadState===st){ S.uploadState=null; if(S.stop?.id===stopId)renderStopDetail(); } },2000);
        } else {
            st.phase="failed";
            st.message=r?.error||"Upload failed";
            renderUploadStatus(stopId,evType);
        }
    }catch(e){
        if(S.uploadState!==st)return;
        st.phase="failed";
        st.message=e?.message||"Upload failed — check your connection";
        renderUploadStatus(stopId,evType);
        console.warn("Evidence upload failed", e);
    }
}

async function delEv(stopId,evType,attId){
    if(!confirm("Remove this document?"))return;
    await rpc("/dispatch/driver/evidence/remove",{stop_id:stopId,ev_type:evType,att_id:attId});
    const stop=S.stops.find(s=>s.id===stopId);
    if(stop){const key=evType==="pop"?"pop_attachments":"pod_attachments";stop[key]=(stop[key]||[]).filter(a=>a.id!==attId);}
    if(S.stop?.id===stopId) renderStopDetail();
    if(finishProofOpen() && S.finishFlow?.stopId===stopId) renderFinishProof();
    toast("Removed");
}
window.delEv=delEv;

// ── Document Scanner (jscanify + OpenCV.js — both free, no API cost) ───────
// OpenCV.js is loaded from the official docs.opencv.org build, only when
// Scan Doc is first tapped (it's ~8MB, not worth it on every page load).
// jscanify is vendored locally in static/src/lib (MIT, tiny) so the scanner
// doesn't depend on a third-party CDN at runtime.
let _scannerLoading=null, _scannerReady=false, _jscanify=null;
let _scanStream=null, _scanStopId=null, _scanEvType=null, _scanCapturedCanvas=null, _scanEnhanced=false;

function loadScannerLibs(){
    if(_scannerReady) return Promise.resolve();
    if(_scannerLoading) return _scannerLoading;
    _scannerLoading=new Promise((resolve,reject)=>{
        if(window.cv && window.cv.Mat){ loadJscanify(resolve,reject); return; }
        const cvScript=document.createElement("script");
        cvScript.src="https://docs.opencv.org/4.x/opencv.js";
        cvScript.onload=()=>{
            const waitCv=()=>{
                if(window.cv && window.cv.Mat) loadJscanify(resolve,reject);
                else setTimeout(waitCv,150);
            };
            waitCv();
        };
        cvScript.onerror=()=>reject(new Error("Could not load OpenCV.js"));
        document.head.appendChild(cvScript);
    });
    return _scannerLoading;
}
function loadJscanify(resolve,reject){
    if(window.jscanify){ _jscanify=new window.jscanify(); _scannerReady=true; resolve(); return; }
    const s=document.createElement("script");
    s.src="/prema_dispatch/static/src/lib/jscanify.min.js";
    s.onload=()=>{ _jscanify=new window.jscanify(); _scannerReady=true; resolve(); };
    s.onerror=()=>reject(new Error("Could not load jscanify"));
    document.head.appendChild(s);
}

async function openScanner(stopId,evType){
    _scanStopId=stopId; _scanEvType=evType; _scanEnhanced=false;
    show("oScanner");
    renderScanStatus("idle"); setScanButtonsDisabled(false);
    setScannerStage("camera");
    toast("Loading scanner…");
    try{
        await loadScannerLibs();
        hide("toast");
    }catch(e){
        hide("toast");
        toast("Scanner unavailable — capturing without auto-crop");
    }
    await startScanCamera();
}
window.openScanner=openScanner;

async function startScanCamera(){
    const video=q("#scanVideo");
    try{
        _scanStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:"environment"}, audio:false});
        video.srcObject=_scanStream;
        await video.play();
    }catch(e){ toast("Camera access denied"); closeScanner(); }
}
function stopScanCamera(){
    if(_scanStream){ _scanStream.getTracks().forEach(t=>t.stop()); _scanStream=null; }
}
function setScannerStage(stage){
    const cam=q("#scanCameraStage"), prev=q("#scanPreviewStage");
    if(cam)cam.style.display = stage==="camera" ? "flex":"none";
    if(prev)prev.style.display = stage==="preview" ? "flex":"none";
}

function captureScan(){
    const video=q("#scanVideo");
    if(!video?.videoWidth){ toast("Camera not ready yet"); return; }
    const canvas=document.createElement("canvas");
    canvas.width=video.videoWidth; canvas.height=video.videoHeight;
    canvas.getContext("2d").drawImage(video,0,0);
    processScan(canvas);
}
window.captureScan=captureScan;

function processScan(sourceCanvas){
    let resultCanvas=null;
    if(_jscanify){
        try{
            // jscanify detects the document's 4 corners and perspective-corrects
            // to them ("auto crop"). Falls back to the full frame if no
            // document-like contour is found.
            resultCanvas=_jscanify.extractPaper(sourceCanvas, sourceCanvas.width, Math.round(sourceCanvas.width*1.294));
        }catch(e){ console.warn("Auto-crop failed, using full frame", e); }
    }
    _scanCapturedCanvas=resultCanvas||sourceCanvas;
    renderScanPreview();
    setScannerStage("preview");
}

function renderScanPreview(){
    const out=q("#scanPreviewCanvas");
    if(!out||!_scanCapturedCanvas)return;
    out.width=_scanCapturedCanvas.width; out.height=_scanCapturedCanvas.height;
    const ctx=out.getContext("2d");
    // "Adjustments if needed": optional contrast/brightness boost for a
    // cleaner scanned-document look, toggled by the driver — off by default
    // since some PODs need original colour (stamps, coloured ink).
    ctx.filter = _scanEnhanced ? "contrast(1.3) brightness(1.08) saturate(0.5)" : "none";
    ctx.drawImage(_scanCapturedCanvas,0,0);
}
function toggleScanEnhance(){
    _scanEnhanced=!_scanEnhanced;
    const btn=q("#scanEnhanceBtn");
    if(btn)btn.classList.toggle("active",_scanEnhanced);
    renderScanPreview();
}
window.toggleScanEnhance=toggleScanEnhance;

function retakeScan(){ renderScanStatus("idle"); setScanButtonsDisabled(false); setScannerStage("camera"); }
window.retakeScan=retakeScan;

function setScanButtonsDisabled(disabled){
    ["scanUseBtn","scanRetakeBtn","scanEnhanceBtn"].forEach(id=>{ const el=q("#"+id); if(el)el.disabled=disabled; });
}
function renderScanStatus(phase,message){
    const row=q("#scanStatusRow"); if(!row)return;
    if(phase==="idle"){ row.style.display="none"; row.innerHTML=""; return; }
    row.style.display="flex";
    if(phase==="uploading") row.innerHTML=`<span class="da-ev-status-msg">Uploading scan…</span>`;
    else if(phase==="success") row.innerHTML=`<span class="da-ev-status-msg da-ev-status-ok">✓ ${esc(message||"Scan saved")}</span>`;
    else if(phase==="failed") row.innerHTML=
        `<span class="da-ev-status-msg da-ev-status-err">✕ ${esc(message||"Upload failed")}</span>`+
        `<button class="da-btn da-btn-secondary da-btn-sm" onclick="useScan()">Retry</button>`;
}
async function useScan(){
    const out=q("#scanPreviewCanvas");
    if(!out)return;
    const dataUrl=out.toDataURL("image/jpeg",0.92);
    const b64=dataUrl.split(",")[1];
    setScanButtonsDisabled(true);
    renderScanStatus("uploading");
    try{
        const r=await rpc("/dispatch/driver/evidence/add",{
            stop_id:_scanStopId, ev_type:_scanEvType, data_b64:b64, filename:`scan_${Date.now()}.jpg`,
        });
        if(r?.success){
            renderScanStatus("success", r.duplicate?(r.message||"Already uploaded"):"Scan saved");
            const stop=S.stops.find(s=>s.id===_scanStopId);
            if(stop && !r.duplicate){
                const key=_scanEvType==="pop"?"pop_attachments":"pod_attachments";
                (stop[key]=stop[key]||[]).push({id:r.id,name:r.name,url:r.url});
            }
            if(S.stop?.id===_scanStopId) renderStopDetail();
            if(finishProofOpen() && S.finishFlow?.stopId===_scanStopId) renderFinishProof();
            setTimeout(()=>{ renderScanStatus("idle"); setScannerStage("camera"); },1200);
            return; // keep buttons disabled during the success-message pause; re-enabled by the next scan's setScannerStage
        }
        renderScanStatus("failed", r?.error||"Upload failed");
    }catch(e){
        renderScanStatus("failed", e?.message||"Upload error — check your connection");
    }
    setScanButtonsDisabled(false);
}
window.useScan=useScan;

function closeScanner(){
    stopScanCamera();
    hide("oScanner");
}
window.closeScanner=closeScanner;

// ── Load Plan (Phase 4) ──────────────────────────────────────────────
// Same batched prema.dispatch.load.plan methods the Dispatcher panel
// uses (via controllers/load_plan_driver.py, which never raises to the
// browser — same {success:false,error:...} convention as every other
// driver_* endpoint). Tap-to-select, tap-to-place only — no drag-and-drop
// on mobile, per spec.
function renderLoadPlanChip(){
    const chip=q("#loadPlanChip"); if(!chip)return;
    const truck=S.dayData?.truck;
    if(!truck || !truck.id){ chip.style.display="none"; return; }
    if(S.loadPlan){
        const c=S.loadPlan.counts;
        chip.innerHTML=`<div class="da-lp-chip-title">LOAD PLAN</div>
            <div class="da-lp-chip-line">${c.loaded} / ${c.confirmed} loaded</div>
            <div class="da-lp-chip-line">${S.loadPlan.unassigned_items.length} unassigned</div>
            <div class="da-lp-chip-line">${S.loadPlan.warnings.length} warning(s)</div>`;
        chip.style.display="block";
    } else {
        chip.innerHTML=`<div class="da-lp-chip-title">LOAD PLAN</div><div class="da-lp-chip-line">Tap to open</div>`;
        chip.style.display="block";
    }
}
window.openLoadPlan=openLoadPlan;
async function openLoadPlan(){
    const truck=S.dayData?.truck;
    if(!truck || !truck.id){ toast("No truck assigned for this date"); return; }
    showScreen("sLoadPlan");
    const body=q("#loadPlanBody");
    if(body) body.innerHTML='<div class="da-empty"><div class="da-empty-title">Loading Load Plan…</div></div>';
    try{
        const data=await rpc("/dispatch/driver/loadplan/get",{
            vehicle_id: truck.id, operating_date: S.selDate, driver_id: null,
        });
        if(!data || data.success===false){
            if(body) body.innerHTML=`<div class="da-empty"><div class="da-empty-title">${esc(data?.error||"Could not load Load Plan")}</div></div>`;
            return;
        }
        S.loadPlan=data; S.lpSelectedCode=null;
        renderLoadPlan(); renderLoadPlanChip();
    }catch(e){
        if(body) body.innerHTML=`<div class="da-empty"><div class="da-empty-title">${esc(e.message||"Error loading plan")}</div></div>`;
    }
}

function lpPositionClass(pos){
    if(pos.blocked)return "da-lp-pos da-lp-pos-blocked";
    if(S.lpSelectedCode===pos.position_code)return "da-lp-pos da-lp-pos-selected";
    if(!pos.item)return "da-lp-pos da-lp-pos-vacant";
    if(pos.item.shared_skid)return "da-lp-pos da-lp-pos-shared";
    if(["loaded","in_transit","delivered"].includes(pos.item.status))return "da-lp-pos da-lp-pos-loaded";
    if(pos.item.status==="pending")return "da-lp-pos da-lp-pos-reserved";
    return "da-lp-pos da-lp-pos-occupied";
}

function renderLoadPlanPosCell(pos){
    const stops=pos.item?.stops?.length ? `Stop${pos.item.stops.length>1?"s":""} ${esc(pos.item.stops.map(s=>s.sequence).join("/"))}` : "";
    return `<div class="${lpPositionClass(pos)}" onclick="lpTapPosition('${pos.position_code}')">
        <div class="da-lp-pos-code">${esc(pos.position_code)}</div>
        ${pos.item ? `<div class="da-lp-pos-item">${esc(pos.item.name)}</div>${stops?`<div class="da-lp-pos-stops">${stops}</div>`:""}${pos.item.shared_skid?`<div class="da-lp-pos-shared">SHARED</div>`:""}`
                   : `<div class="da-lp-pos-vacant">VACANT</div>`}
    </div>`;
}

function renderLoadPlan(){
    const body=q("#loadPlanBody"); if(!body||!S.loadPlan)return;
    const lp=S.loadPlan;
    const driverPos=lp.positions.filter(p=>p.side==="driver");
    const passPos=lp.positions.filter(p=>p.side==="passenger");
    const centerPos=lp.positions.filter(p=>p.side==="center");
    const selected=lp.positions.find(p=>p.position_code===S.lpSelectedCode);

    let h=`<div class="da-lp-summary">
        <span>Confirmed ${lp.counts.confirmed}</span><span>Assigned ${lp.counts.assigned}</span>
        <span>Loaded ${lp.counts.loaded}</span><span>Vacant ${lp.counts.vacant}</span>
        ${lp.is_stale?`<span class="da-lp-warn-badge">STALE</span>`:""}
        ${lp.is_locked?`<span class="da-lp-lock-badge">🔒 LOCKED</span>`:""}
    </div>`;

    if(!lp.layout_template.is_verified){
        h+=`<div class="da-lp-unverified-banner">
            <div><b>⚠ UNVERIFIED VEHICLE LAYOUT</b></div>
            <div>Dimensions and capacity have not yet been physically verified — planning aid only, not a guarantee of fit.</div>
            <div>${lp.unverified_layout_acknowledged ? "✓ Acknowledged by dispatcher" : "Awaiting dispatcher acknowledgement before loading can be confirmed."}</div>
        </div>`;
    }

    h+=`<div class="da-lp-diagram-label">FRONT / CAB</div>`;
    h+=`<div class="da-lp-grid">`;
    for(let i=0;i<Math.max(driverPos.length,passPos.length);i++){
        h+=`<div class="da-lp-row">`;
        if(driverPos[i]) h+=renderLoadPlanPosCell(driverPos[i]);
        if(passPos[i]) h+=renderLoadPlanPosCell(passPos[i]);
        h+=`</div>`;
    }
    h+=`</div>`;
    if(centerPos.length) h+=`<div class="da-lp-grid">${centerPos.map(renderLoadPlanPosCell).join("")}</div>`;
    h+=`<div class="da-lp-diagram-label">REAR DOOR / LIFTGATE</div>`;

    if(selected){
        h+=`<div class="da-lp-detail-card">`;
        if(selected.item){
            h+=`<div><b>${esc(selected.item.name)}</b> — ${esc(selected.item.status)}</div>`;
            if(selected.item.stops.length) h+=`<div class="da-lp-detail-stops">Stops: ${esc(selected.item.stops.map(s=>s.sequence+" ("+ (s.customer||"") +")").join(", "))}</div>`;
            h+=`<div class="da-lp-detail-actions">
                <button class="da-btn da-btn-secondary da-btn-sm" onclick="lpMarkLoaded(${selected.item.id})">Mark Loaded</button>
                <button class="da-btn da-btn-ghost da-btn-sm" onclick="lpUnassign(${selected.item.id})">Unassign</button>
                <button class="da-btn da-btn-orange da-btn-sm" onclick="lpReportException(${selected.item.id})">⚠ Exception</button>
            </div>`;
        } else {
            h+=`<div><b>Position ${esc(selected.position_code)}</b> — select a pallet to assign</div><div class="da-lp-unassigned-list">`;
            if(!lp.unassigned_items.length) h+=`<div class="da-empty-sub">No unassigned pallets.</div>`;
            lp.unassigned_items.forEach(it=>{
                h+=`<div class="da-lp-unassigned-item" onclick="lpAssign(${it.id})">${esc(it.name)}</div>`;
            });
            h+=`</div>`;
        }
        h+=`</div>`;
    }

    h+=`<div class="da-lp-section"><b>Unassigned (${lp.unassigned_items.length})</b>`;
    lp.unassigned_items.forEach(it=>{ h+=`<div class="da-lp-unassigned-item">${esc(it.name)}</div>`; });
    h+=`</div>`;

    if(lp.warnings.length){
        h+=`<div class="da-lp-section"><b>Warnings</b>`;
        lp.warnings.forEach(w=>{ h+=`<div class="da-lp-warning">⚠ ${esc(w)}</div>`; });
        h+=`</div>`;
    }

    h+=`<div class="da-lp-actions">
        <button class="da-btn da-btn-secondary" onclick="openScanner(0,'pod')" title="Upload a loading/pallet photo">📷 Upload Loading Photo</button>
        ${!lp.is_locked?`<button class="da-btn da-btn-primary" onclick="lpConfirmLoading()">✓ Confirm Loading</button>`:""}
    </div>`;

    body.innerHTML=h;
}

function lpTapPosition(code){
    S.lpSelectedCode = (S.lpSelectedCode===code) ? null : code;
    renderLoadPlan();
}
window.lpTapPosition=lpTapPosition;

async function lpCall(route, params){
    try{
        const r=await rpc(route, {load_plan_id:S.loadPlan.id, version:S.loadPlan.version, ...params});
        if(!r || r.success===false){ toast(r?.error||"Action failed"); return null; }
        return r;
    }catch(e){ toast("Error: "+(e.message||"failed")); return null; }
}

async function lpAssign(itemId){
    const pos=S.loadPlan.positions.find(p=>p.position_code===S.lpSelectedCode);
    if(!pos)return;
    const r=await lpCall("/dispatch/driver/loadplan/assign",{item_id:itemId, position_id:pos.id});
    if(r){ S.loadPlan=r; S.lpSelectedCode=null; renderLoadPlan(); renderLoadPlanChip(); }
}
window.lpAssign=lpAssign;

async function lpUnassign(itemId){
    const r=await lpCall("/dispatch/driver/loadplan/unassign",{item_id:itemId});
    if(r){ S.loadPlan=r; S.lpSelectedCode=null; renderLoadPlan(); renderLoadPlanChip(); }
}
window.lpUnassign=lpUnassign;

async function lpMarkLoaded(itemId){
    const r=await lpCall("/dispatch/driver/loadplan/mark_loaded",{item_id:itemId});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); toast("Marked loaded ✓"); }
}
window.lpMarkLoaded=lpMarkLoaded;

async function lpConfirmLoading(){
    const r=await lpCall("/dispatch/driver/loadplan/confirm",{});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); toast("Loading confirmed ✓"); }
}
window.lpConfirmLoading=lpConfirmLoading;

async function lpReportException(itemId){
    const notes=prompt("Describe the exception (damage, shortage, etc.):");
    if(!notes)return;
    const r=await lpCall("/dispatch/driver/loadplan/exception",{item_id:itemId, exception_type:"damaged", notes});
    if(r){ S.loadPlan=r; renderLoadPlan(); renderLoadPlanChip(); toast("Exception reported"); }
}
window.lpReportException=lpReportException;

// ── Stop Map ──────────────────────────────────────────────────────
function initStopMap(stop){
    const el=q("#stopDetailMap"); if(!el||!isMaps())return;
    const c=(stop.lat&&stop.lng)?{lat:stop.lat,lng:stop.lng}:{lat:43.65,lng:-79.38};
    if(!S.maps.stop){
        S.maps.stop=new google.maps.Map(el,{
            center:c,zoom:18,mapTypeId:S.isSat?"hybrid":"roadmap",disableDefaultUI:false,
            gestureHandling:"greedy",fullscreenControl:true,zoomControl:true,mapTypeControl:true,streetViewControl:false
        });
    } else { S.maps.stop.setCenter(c); S.maps.stop.setZoom(18); S.maps.stop.setMapTypeId(S.isSat?"hybrid":"roadmap"); }
    if(S.markers.stopPin)S.markers.stopPin.setMap(null);
    if(stop.lat&&stop.lng){
        S.markers.stopPin=new google.maps.Marker({position:c,map:S.maps.stop,
            icon:{url:"https://maps.google.com/mapfiles/ms/icons/red-dot.png",scaledSize:new google.maps.Size(40,40),anchor:new google.maps.Point(20,40)}});
    }
}

// ── Actions ───────────────────────────────────────────────────────
function doNavigate(){
    openNav(S.stop);
    if(["arrived","completed","cancelled"].includes(S.stop?.status)) return;
    rpc("/dispatch/driver/stop/status",{stop_id:S.stop.id,action:"en_route",data:{}}).then(()=>{
        patchStopState(S.stop.id,{status:"en_route"});
        renderStopList();
        if(visibleScreen()==="sStop") renderStopDetail();
    });
}

// ── In-app Navigation (Amazon Flex style) ──────────────────────────
// Real turn-by-turn voice navigation requires a native mobile SDK (what
// Amazon Flex uses) — there is no equivalent for a browser web app. This
// gives the closest practical equivalent: a live map with the driver's
// blue-arrow GPS position, the truck-friendly route line, a running
// distance/ETA badge, and the next-turn instruction text from the
// Directions API, refreshed periodically as the driver moves. "Open in
// Maps app" remains available for spoken turn-by-turn guidance.
function openNav(stop){
    S.stop=stop;
    showScreen("sNav");
    const dt=q("#navDestType"), dn=q("#navDestName"), da=q("#navDestAddr");
    if(dt)dt.textContent=stopTypeTitle(stop.type);
    if(dn)dn.textContent=stopCompany(stop);
    if(da)da.textContent=stop.address||"";
    armGeo(stop);
    if(S.mapsReady) initNavMap();
    // "Navigate — Truck Route" should land the driver straight into a
    // fullscreen map, matching what a real nav app does — not an inline
    // map the driver has to separately tap ⛶ to expand.
    if(!document.fullscreenElement) toggleNavFullscreen();
    if(S.navTimer)clearInterval(S.navTimer);
    S.navTimer=setInterval(()=>{ if(visibleScreen()==="sNav") refreshNavRoute(); }, 20000);
}

function closeNav(){
    if(S.navTimer){ clearInterval(S.navTimer); S.navTimer=null; }
    showScreen("sStop");
    renderStopDetail();
}

function initNavMap(){
    const el=q("#navMap"); if(!el||!isMaps())return;
    if(!S.maps.nav){
        S.maps.nav=new google.maps.Map(el,{
            center:{lat:43.65,lng:-79.38},zoom:17,
            mapTypeId:"roadmap",disableDefaultUI:false,gestureHandling:"greedy",
            fullscreenControl:true,zoomControl:true,streetViewControl:false,
            // Map/Satellite toggle moved to the top-right so it stops
            // sitting under the top-left ✕ Close button.
            mapTypeControl:true,
            mapTypeControlOptions:{position:google.maps.ControlPosition.TOP_RIGHT}
        });
        S.navDirRenderer=new google.maps.DirectionsRenderer({
            map:S.maps.nav, suppressMarkers:true,
            polylineOptions:{strokeColor:"#1565C0",strokeWeight:6,strokeOpacity:.9}
        });
        S.trafficNav=new google.maps.TrafficLayer();
    }
    S.trafficNav?.setMap(S.navTrafficOn ? S.maps.nav : null);
    refreshNavRoute();
}

function refreshNavRoute(){
    const stop=S.stop;
    if(!stop?.lat||!stop?.lng||!S.dirSvc||!S.maps.nav)return;
    const origin=(S.lat&&S.lng)?{lat:S.lat,lng:S.lng}:null;
    if(!origin){ const ins=q("#navInstruction"); if(ins)ins.textContent="Waiting for GPS…"; return; }
    S.dirSvc.route({
        origin, destination:{lat:stop.lat,lng:stop.lng},
        travelMode:google.maps.TravelMode.DRIVING,
        avoidTolls:S.routeOpts.tolls, avoidHighways:S.routeOpts.highways, avoidFerries:S.routeOpts.ferries
    },(result,status)=>{
        if(status!=="OK")return;
        S.navDirRenderer.setDirections(result);
        const leg=result.routes[0]?.legs?.[0];
        if(!leg)return;
        const badge=q("#navDistBadge");
        if(badge)badge.textContent=`${leg.distance?.text||""} · ${leg.duration?.text||""}`;
        const firstStep=leg.steps?.[0];
        const ins=q("#navInstruction");
        const text=firstStep?stripHtml(firstStep.instructions):"";
        if(ins&&text)ins.textContent=text;
        speakNavInstruction(text);
        updateNavDriverMarker();
    });
}

// Voice guidance via the browser's built-in Web Speech API — reads the next
// turn instruction aloud. This is NOT Google's native spoken turn-by-turn
// (that requires a mobile Navigation SDK, unavailable to a web app); it's
// the closest practical equivalent achievable in a browser, and free.
function speakNavInstruction(text){
    if(!S.navVoiceOn||!text||text===S._lastSpoken||!window.speechSynthesis)return;
    S._lastSpoken=text;
    try{
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
    }catch(e){}
}

function updateNavDriverMarker(){
    if(!S.maps.nav||!S.lat||!S.lng)return;
    const pos={lat:S.lat,lng:S.lng};
    // Heading mode: rotate the arrow to match the direction of travel
    // (computed from consecutive GPS fixes). Google Maps JS API only
    // supports true map-rotation ("heading-up") on vector maps with a
    // Map ID — without one, rotating the arrow icon is the honest
    // equivalent available here.
    let rotation=0;
    if(S.navHeadingMode&&S._prevNavPos&&window.google?.maps?.geometry){
        rotation=google.maps.geometry.spherical.computeHeading(
            new google.maps.LatLng(S._prevNavPos.lat,S._prevNavPos.lng),
            new google.maps.LatLng(pos.lat,pos.lng)
        );
    }
    S._prevNavPos=pos;
    if(!S.markers.navDriver){
        S.markers.navDriver=new google.maps.Marker({
            position:pos, map:S.maps.nav, zIndex:300,
            icon:{path:google.maps.SymbolPath.FORWARD_CLOSED_ARROW,scale:8,rotation,
                  fillColor:"#1565C0",fillOpacity:1,strokeColor:"#fff",strokeWeight:2}
        });
        // First real GPS fix — snap straight to a close, nav-style zoom
        // instead of leaving the map at the wide fallback framing.
        S.maps.nav.setCenter(pos);
        S.maps.nav.setZoom(17);
    } else {
        S.markers.navDriver.setPosition(pos);
        if(S.navHeadingMode){
            const icon=S.markers.navDriver.getIcon();
            icon.rotation=rotation;
            S.markers.navDriver.setIcon(icon);
        }
    }
    S.maps.nav.panTo(pos);
}

function toggleNavHeading(){
    S.navHeadingMode=!S.navHeadingMode;
    const btn=q("#navHeadingBtn"); if(btn)btn.innerHTML=S.navHeadingMode?"🧭 Heading":"🧭 North";
    if(!S.navHeadingMode&&S.markers.navDriver){
        const icon=S.markers.navDriver.getIcon(); icon.rotation=0; S.markers.navDriver.setIcon(icon);
    }
}
window.toggleNavHeading=toggleNavHeading;

function toggleNavVoice(){
    S.navVoiceOn=!S.navVoiceOn;
    const btn=q("#navVoiceBtn"); if(btn)btn.innerHTML=S.navVoiceOn?"🔊 Voice":"🔇 Voice";
    if(!S.navVoiceOn&&window.speechSynthesis)window.speechSynthesis.cancel();
}
window.toggleNavVoice=toggleNavVoice;

function toggleNavTraffic(){
    S.navTrafficOn=!S.navTrafficOn;
    const btn=q("#navTrafficBtn"); if(btn)btn.innerHTML=S.navTrafficOn?"🚦 Traffic":"🚦 No Traffic";
    S.trafficNav?.setMap(S.navTrafficOn && S.maps.nav ? S.maps.nav : null);
    S.trafficRoute?.setMap(S.navTrafficOn && S.maps.route ? S.maps.route : null);
}
window.toggleNavTraffic=toggleNavTraffic;

function toggleNavFullscreen(){
    const wrap=q("#navMapWrap");
    if(!wrap) return;
    if(document.fullscreenElement){
        if(document.exitFullscreen){
            document.exitFullscreen().catch(()=>setNavFullscreenUI(false));
        }else{
            setNavFullscreenUI(false);
        }
        return;
    }
    if(wrap.requestFullscreen){
        wrap.requestFullscreen()
            .then(()=>setNavFullscreenUI(true))
            .catch(()=>setNavFullscreenUI(!S.navFullscreen));
        return;
    }
    setNavFullscreenUI(!S.navFullscreen);
}
window.toggleNavFullscreen=toggleNavFullscreen;

function stripHtml(html){
    const d=document.createElement("div"); d.innerHTML=html||""; return d.textContent||d.innerText||"";
}

async function doArrived(){
    const stopId=S.stop?.id;
    if(!stopId) return;
    const ok=await callStop(stopId,"arrived",{lat:S.lat||0,lng:S.lng||0});
    if(ok){
        patchStopState(stopId,{status:"arrived",actual_arrival_time:new Date().toISOString()});
        await reloadDay();
        const updated=findStopById(stopId);
        if(updated) S.stop=updated;
        renderStopList();
        renderStopDetail();
        toast("Marked as Arrived ✓");
    }
}

async function doComplete(){
    openFinishProof();
}

async function doRestoreStop(){
    if(!S.stop) return;
    if(!confirm("Restore this stop back to Pending?"))return;
    const fromNav=visibleScreen()==="sNav";
    const ok=await callStop(S.stop.id,"restore",{});
    if(ok){
        await reloadDay();
        const updated=S.stops.find(s=>s.id===S.stop.id) || S.stop;
        S.stop=updated;
        if(finishProofOpen()) closeFinishProof();
        if(fromNav) showScreen("sStop");
        renderStopList();
        renderStopDetail();
        if(fromNav && S.mapsReady) initStopMap(S.stop);
        toast("Stop restored");
    }
}
window.doRestoreStop=doRestoreStop;

function setReceivingTruckSelection(rawValue){
    if(!S.stop || !supportsReceivingTruckAssignment(S.stop)) return;
    const truckId=parseInt(rawValue||"0",10)||false;
    const truck=availableTransferTrucks().find(entry=>entry.id===truckId);
    const patch={
        transfer_to_vehicle_id: truckId || false,
        transfer_to_vehicle: truck ? truck.name : "",
        transfer_to_vehicle_plate: truck ? (truck.plate || "") : "",
        transfer_to_driver_id: truck ? (truck.driver_id || false) : false,
        transfer_to_driver: truck ? (truck.driver_name || "") : "",
    };
    patchStopState(S.stop.id,patch);
    const updated=findStopById(S.stop.id);
    if(updated) S.stop=updated;
    renderStopDetail();
}
window.setReceivingTruckSelection=setReceivingTruckSelection;

function clearReceivingTruckSelection(){
    setReceivingTruckSelection("");
}
window.clearReceivingTruckSelection=clearReceivingTruckSelection;

async function applyReceivingTruck(stageUnassigned=false){
    if(!S.stop || !supportsReceivingTruckAssignment(S.stop)) return;
    const stopId=S.stop.id;
    const vehicleId=stageUnassigned ? false : (S.stop.transfer_to_vehicle_id || false);
    if(!stageUnassigned && !vehicleId){
        toast("Choose a receiving truck first");
        return;
    }
    try{
        const r=await rpc("/dispatch/driver/stop/status",{
            stop_id:stopId,
            action:"assign_receiving_truck",
            data:{vehicle_id:vehicleId,stage_unassigned:stageUnassigned},
        });
        if(r?.success){
            toast(r.message || (stageUnassigned ? "Remaining route staged and unassigned" : "Receiving truck updated"));
            await reloadDay();
            const updated=findStopById(stopId);
            if(updated){
                S.stop=updated;
                renderStopList();
                renderStopDetail();
                if(S.mapsReady && visibleScreen()==="sStop") initStopMap(S.stop);
            }else{
                showScreen("sSchedule");
                renderStopList();
                if(S.mapsReady) initRouteMap();
            }
        }else{
            toast(r?.error||"Could not update receiving truck");
        }
    }catch(e){
        toast("Error: "+(e.message||"failed"));
    }
}
window.applyReceivingTruck=applyReceivingTruck;

function doDelayed(){
    show("oIssue");
    hide("issueOtherRow");
    const inp=q("#issueOtherInput"); if(inp)inp.value="";
}
function closeIssue(){ hide("oIssue"); }

function openRouteSettings(){
    const t=q("#rsTolls"), h=q("#rsHighways"), f=q("#rsFerries");
    if(t)t.checked=!!S.routeOpts.tolls;
    if(h)h.checked=!!S.routeOpts.highways;
    if(f)f.checked=!!S.routeOpts.ferries;
    show("oRouteSettings");
}
function saveRouteSettings(){
    S.routeOpts={
        tolls:    !!q("#rsTolls")?.checked,
        highways: !!q("#rsHighways")?.checked,
        ferries:  !!q("#rsFerries")?.checked,
    };
    localStorage.setItem(RS_KEY, JSON.stringify(S.routeOpts));
    if(S.mapsReady) initRouteMap();
}
function pickIssueReason(reason){
    if(reason==="Others"){
        show("issueOtherRow");
        const inp=q("#issueOtherInput"); if(inp)inp.focus();
        return;
    }
    submitIssue(reason);
}
function sendIssueOther(){
    const inp=q("#issueOtherInput");
    const text=(inp?.value||"").trim();
    if(!text){ toast("Type a description first"); return; }
    submitIssue(text);
}
async function submitIssue(reason){
    closeIssue();
    await callStop(S.stop.id,"delayed",{delay_reason:reason});
    S.stop.status="issue";renderStopDetail();toast("Issue reported: "+reason);
}

async function doExecuteTransfer(){
    try{
        const r=await rpc("/dispatch/driver/stop/transfer",{stop_id:S.stop.id});
        if(r?.success){
            toast(r.unassigned ? "✓ Freight staged and unassigned" : "✓ Transfer complete");
            patchStopState(S.stop.id,{status:"completed"});
            await reloadDay(); advanceNext();
        } else toast(r?.error||"Could not complete transfer");
    }catch(e){ toast("Error: "+(e.message||"failed")); }
}
window.doExecuteTransfer=doExecuteTransfer;

async function doSkip(){
    if(!confirm("Skip this stop?"))return;
    await callStop(S.stop.id,"en_route",{});
    S.stop.status="skipped";await reloadDay();advanceNext();
}

async function bumpSvcTime(delta){
    const mins=Math.max(5,Math.min(120,(S.stop.service_time_min||15)+delta));
    try{
        await rpc("/dispatch/driver/stop/service_time",{stop_id:S.stop.id,minutes:mins});
        S.stop.service_time_min=mins;
        const idx=S.stops.findIndex(s=>s.id===S.stop.id);
        if(idx>=0) S.stops[idx].service_time_min=mins;
        renderStopDetail();
    }catch(e){ toast("Could not update service time"); }
}
window.bumpSvcTime=bumpSvcTime;

async function doDeleteStop(){
    if(!confirm("Delete this stop? This cannot be undone."))return;
    try{
        const r=await rpc("/dispatch/driver/stop/delete",{stop_id:S.stop.id});
        if(r?.success){
            toast("Stop deleted");
            await reloadDay();
            showScreen("sSchedule"); renderStopList();
            if(S.mapsReady) initRouteMap();
        } else toast(r?.error||"Could not delete stop");
    }catch(e){ toast("Error: "+(e.message||"failed")); }
}
window.doDeleteStop=doDeleteStop;

function advanceNext(){
    const next=S.stops.find(s=>!["completed","skipped","cancelled","issue"].includes(s.status));
    if(!next){toast("🎉 All stops complete!");showScreen("sSchedule");renderStopList();return;}
    S.stop=next; rpc("/dispatch/driver/stop/status",{stop_id:next.id,action:"en_route",data:{}}).catch(()=>{});
    next.status="en_route"; renderStopList(); openStop(next);
}

async function reloadDay(){
    try{ const d=await rpc("/dispatch/driver/stops",{date_str:S.selDate}); applyDay(d); }catch(e){}
}

async function callStop(id,action,data){
    try{
        const r=await rpc("/dispatch/driver/stop/status",{stop_id:id,action,data});
        if(!r?.success && r?.error) toast(r.error);
        return !!r?.success;
    }
    catch(e){ toast("Error: "+(e.message||"failed")); return false; }
}

// ── Native Maps Navigation ────────────────────────────────────────
function openNativeMaps(stop){
    if(!stop?.lat||!stop?.lng){toast("No GPS pin for this stop");return;}
    const avoidList=[];
    if(S.routeOpts.tolls)avoidList.push("tolls");
    if(S.routeOpts.highways)avoidList.push("highways");
    if(S.routeOpts.ferries)avoidList.push("ferries");
    const avoidParam=avoidList.length?`&avoid=${avoidList.join("|")}`:"";
    const isIOS=/iPhone|iPad|iPod/.test(navigator.userAgent);
    const url=`https://www.google.com/maps/dir/?api=1&destination=${stop.lat},${stop.lng}&travelmode=driving${avoidParam}`;
    if(isIOS){ window.location.href=`maps://maps.apple.com/?daddr=${stop.lat},${stop.lng}&dirflg=d`; setTimeout(()=>window.open(url,"_blank"),600); }
    else window.open(url,"_blank");
}

// ── GPS & Geofence ────────────────────────────────────────────────
function startGPS(){
    if(!navigator.geolocation)return;
    S.gpsId=navigator.geolocation.watchPosition(pos=>{
        S.lat=pos.coords.latitude; S.lng=pos.coords.longitude;
        checkGeo();
        if(visibleScreen()==="sNav") updateNavDriverMarker();
    },null,{enableHighAccuracy:true,maximumAge:15000});
}

function armGeo(stop){ S.geoArmed=true; clearGeoTimer(); hide("geoBanner"); }
function checkGeo(){
    if(!S.geoArmed||!S.stop?.lat||!S.lat)return;
    const s=S.stop;
    if(["completed","skipped","cancelled","arrived"].includes(s.status))return;
    if(haversine(S.lat,S.lng,s.lat,s.lng)<=S.GEO_M) triggerGeo();
}
function triggerGeo(){
    if(S.geoTimer)return; S.geoArmed=false;
    let secs=S.GEO_SEC; show("geoBanner");
    const cd=q("#geoCountdown"); if(cd)cd.textContent=`Auto-arriving in ${secs}s…`;
    S.geoTimer=setInterval(()=>{
        secs--;
        if(cd)cd.textContent=secs>0?`Auto-arriving in ${secs}s…`:"Marking arrived…";
        if(secs<=0){clearGeoTimer();confirmGeoArrive();}
    },1000);
}
async function confirmGeoArrive(){
    clearGeoTimer(); hide("geoBanner"); await doArrived();
    if(visibleScreen()==="sNav") closeNav();
}
async function navConfirmArrived(){ await doArrived(); closeNav(); }
function dismissGeo(){ clearGeoTimer(); hide("geoBanner"); S.geoArmed=false; }
function clearGeoTimer(){ if(S.geoTimer){clearInterval(S.geoTimer);S.geoTimer=null;} }

// ── Fullscreen Map ────────────────────────────────────────────────
function openFullMap(){
    show("oMapFull");
    const el=q("#fullMap"); if(!isMaps()||!el)return;
    if(!S.maps.full){
        S.maps.full=new google.maps.Map(el,{center:{lat:43.65,lng:-79.38},zoom:10,
            mapTypeId:S.isSat?"hybrid":"roadmap",disableDefaultUI:false,gestureHandling:"greedy",
            fullscreenControl:true,zoomControl:true,mapTypeControl:true,streetViewControl:false});
    }
    if(!S.trafficFull) S.trafficFull=new google.maps.TrafficLayer();
    S.trafficFull.setMap(S.navTrafficOn ? S.maps.full : null);
    const pts=S.stops.filter(s=>s.lat&&s.lng);
    drawStopsOnMap(S.maps.full,pts,true);
    if(S.lat&&S.lng){
        if(S.markers.fullDrv)S.markers.fullDrv.setMap(null);
        S.markers.fullDrv=new google.maps.Marker({position:{lat:S.lat,lng:S.lng},map:S.maps.full,zIndex:300,
            icon:{path:google.maps.SymbolPath.FORWARD_CLOSED_ARROW,scale:8,fillColor:"#1565C0",fillOpacity:1,strokeColor:"#fff",strokeWeight:2}});
    }
}

// ── Pin Editor ────────────────────────────────────────────────────
let _pinMk=null;
function openPinEditor(){
    const s=S.stop; show("oPinEdit");
    const el=q("#pinMap"); if(!isMaps()||!el)return;
    const c=(s?.lat&&s?.lng)?{lat:s.lat,lng:s.lng}:{lat:43.65,lng:-79.38};
    if(!S.maps.pin){ S.maps.pin=new google.maps.Map(el,{center:c,zoom:18,mapTypeId:"hybrid",disableDefaultUI:true,gestureHandling:"greedy"}); }
    else{ S.maps.pin.setCenter(c); S.maps.pin.setZoom(18); }
    if(_pinMk)_pinMk.setMap(null);
    _pinMk=new google.maps.Marker({position:c,map:S.maps.pin,draggable:true,
        icon:{url:"https://maps.google.com/mapfiles/ms/icons/red-dot.png",scaledSize:new google.maps.Size(48,48),anchor:new google.maps.Point(24,48)}});
}
function pinGps(){
    navigator.geolocation?.getCurrentPosition(p=>{
        const pos={lat:p.coords.latitude,lng:p.coords.longitude};
        if(_pinMk)_pinMk.setPosition(pos); if(S.maps.pin)S.maps.pin.panTo(pos);
    });
}
async function pinUseAddress(){
    toast("Looking up address…");
    try{
        const r=await rpc("/dispatch/driver/stop/regeocode",{stop_id:S.stop.id});
        if(r?.success){
            const pos={lat:r.lat,lng:r.lng};
            if(_pinMk)_pinMk.setPosition(pos); if(S.maps.pin){S.maps.pin.panTo(pos);S.maps.pin.setZoom(18);}
            patchStopState(S.stop.id,{lat:r.lat,lng:r.lng,pin_set:false});
            await reloadDay();
            const updated=S.stops.find(s=>s.id===S.stop.id);
            if(updated) S.stop=updated;
            toast("Pin set to address ✓");
        } else toast(r?.error||"Could not find this address");
    }catch(e){ toast("Error: "+(e.message||"lookup failed")); }
}
async function pinSave(){
    if(!_pinMk)return;
    const pos=_pinMk.getPosition();
    const r=await rpc("/dispatch/driver/stop/pin",{stop_id:S.stop.id,lat:pos.lat(),lng:pos.lng()});
    if(r?.success){
        patchStopState(S.stop.id,{lat:pos.lat(),lng:pos.lng(),pin_set:true});
        await reloadDay();
        const updated=S.stops.find(s=>s.id===S.stop.id);
        if(updated) S.stop=updated;
        hide("oPinEdit");
        initStopMap(S.stop);
        if(visibleScreen()==="sNav") refreshNavRoute();
        toast("Pin saved ✓");
    }
}

// ── Chat ──────────────────────────────────────────────────────────
async function initChat(){
    try{
        const d=await rpc("/dispatch/driver/chat/init",{});
        S.channelId=d.channel_id; renderMessages(d.messages);
        S.chatPoll=setInterval(async()=>{
            if(!S.chatOpen||!S.channelId)return;
            const r=await rpc("/dispatch/driver/chat/messages",{channel_id:S.channelId});
            renderMessages(r.messages);
        },30000);
    }catch(e){ console.warn("Chat init failed",e); }
}
function openChat(){ S.chatOpen=true; show("oChat"); scrollChat(); }
function closeChat(){ S.chatOpen=false; hide("oChat"); }
async function sendChat(){
    const inp=q("#chatInput"); const body=(inp?.value||"").trim();
    if(!body||!S.channelId)return;
    inp.value="";
    const r=await rpc("/dispatch/driver/chat/send",{channel_id:S.channelId,body});
    if(r?.success){ const d=await rpc("/dispatch/driver/chat/messages",{channel_id:S.channelId}); renderMessages(d.messages); }
}
function renderMessages(msgs){
    const box=q("#chatMessages"); if(!box)return;
    box.innerHTML="";
    (msgs||[]).forEach(m=>{
        const el=mk("div",`da-chat-msg ${m.is_me?"me":"other"}`);
        el.innerHTML=(!m.is_me?`<div class="da-chat-author">${esc(m.author)}</div>`:"")+
            `<div class="da-chat-bubble">${m.body}</div>`+
            `<div class="da-chat-time">${m.date?new Date(m.date).toLocaleTimeString("en-CA",{hour:"2-digit",minute:"2-digit"}):""}</div>`;
        box.appendChild(el);
    });
    scrollChat();
}
function scrollChat(){ const b=q("#chatMessages"); if(b)b.scrollTop=b.scrollHeight; }

// ── Navigation ────────────────────────────────────────────────────
function goBack(){
    const scr=visibleScreen();
    if(scr==="sStop") showScreen("sSchedule");
    else showScreen("sSchedule");
}
function showScreen(id){
    ["sSchedule","sStop","sNav","sLoadPlan"].forEach(s=>{ const el=q("#"+s); if(el)el.style.display=s===id?"flex":"none"; });
    if(id==="sSchedule"){renderStopList();if(S.mapsReady)initRouteMap();}
    syncHistory();
}
function visibleScreen(){ for(const id of["sNav","sStop","sLoadPlan","sSchedule"]){ const el=q("#"+id); if(el&&el.style.display!=="none")return id; } return "sSchedule"; }
function trigResize(k){ if(S.maps[k]&&isMaps())google.maps.event.trigger(S.maps[k],"resize"); }

// ── Navigation state (URL/history sync) ─────────────────────────────
// showScreen()/showViewTab() are the ONLY two places that change what the
// driver sees — every other screen change (goBack, closeNav, openStop,
// openNav, finishSchedule, selectDay, etc.) already funnels through one
// of those two, so hooking history sync there covers the whole app
// without a second router or per-call-site changes.
//
// URL shape: ?screen=sStop&date=2026-07-19&stop=123&tab=home
let _lastHistParams=null;
function currentNavParams(){
    const scr=visibleScreen();
    const p=new URLSearchParams();
    p.set("screen",scr);
    p.set("date",S.selDate||today());
    if(scr==="sSchedule") p.set("tab",S.viewTab==="stops"?"stops":"home");
    if((scr==="sStop"||scr==="sNav")&&S.stop?.id) p.set("stop",String(S.stop.id));
    return p;
}
function syncHistory(replace){
    if(S._suppressHistoryPush)return;
    const params=currentNavParams();
    const qs=params.toString();
    const isDup=(qs===_lastHistParams);
    _lastHistParams=qs;
    if(isDup&&!replace)return; // repeated taps on the same tab/screen: no new entry
    const state=Object.fromEntries(params.entries());
    const url="?"+qs;
    if(replace||!history.state) history.replaceState(state,"",url);
    else history.pushState(state,"",url);
}

function parseNavParams(source){
    const usp=(source instanceof URLSearchParams)?source:new URLSearchParams(source||location.search);
    const screen=usp.get("screen");
    const dateParam=usp.get("date");
    const stopParam=usp.get("stop");
    const tab=usp.get("tab");
    const dateOk=!!dateParam&&/^\d{4}-\d{2}-\d{2}$/.test(dateParam);
    return {
        screen:["sStop","sNav","sLoadPlan","sSchedule"].includes(screen)?screen:"sSchedule",
        date:dateOk?dateParam:today(),
        stopId:stopParam?parseInt(stopParam,10):null,
        tab:tab==="stops"?"stops":"home",
    };
}

// Applies a target nav state (from popstate, or from boot-time URL
// restoration). Re-fetches the day from the server whenever the target
// date differs from what's loaded — the server call itself
// (get_driver_stops_for_date) is already scoped to this driver's own
// jobs, so "is the requested stop present in the returned list" IS the
// server-side authorization check for the restored id; no separate
// endpoint is needed. A stop that's missing (wrong driver, deleted,
// wrong date) falls back to the Stops list with a clear message instead
// of a blank screen or a raw RPC error.
async function applyNavState(target){
    S._suppressHistoryPush=true;
    try{
        if(target.date!==S.selDate){
            try{
                const day=await rpc("/dispatch/driver/stops",{date_str:target.date});
                S.selDate=target.date; applyDay(day); renderWeek(); S.maps.route=null;
            }catch(e){
                toast("The route date changed. Returning to today's routes.");
                S.selDate=today();
                const day=await rpc("/dispatch/driver/stops",{date_str:S.selDate});
                applyDay(day); renderWeek();
                showScreen("sSchedule"); showViewTab("home");
                return;
            }
        }
        if(target.screen==="sLoadPlan"){
            await openLoadPlan();
        } else if(target.stopId){
            const stop=findStopById(target.stopId);
            if(!stop){
                toast("This stop is no longer available.");
                showScreen("sSchedule"); showViewTab("stops");
                return;
            }
            if(target.screen==="sNav") openNav(stop); else openStop(stop);
        } else {
            showScreen("sSchedule");
            showViewTab(target.tab);
        }
    } finally {
        S._suppressHistoryPush=false;
        syncHistory(true);
    }
}

window.addEventListener("popstate", (ev) => {
    const target=ev.state?{
        screen:ev.state.screen,
        date:ev.state.date,
        stopId:ev.state.stop?parseInt(ev.state.stop,10):null,
        tab:ev.state.tab,
    }:parseNavParams();
    applyNavState(target);
});

// ── Utils ─────────────────────────────────────────────────────────
async function rpc(url,params){
    const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},credentials:"include",
        body:JSON.stringify({jsonrpc:"2.0",method:"call",id:(Math.random()*1e9)|0,params})});
    const d=await r.json();
    if(d.error)throw new Error(d.error.data?.message||d.error.message||"RPC error");
    return d.result;
}
// Same JSON-RPC envelope as rpc(), but via XMLHttpRequest so upload
// transmission progress is observable — fetch() has no equivalent event
// for request-body upload progress. Only used for file-upload calls;
// every other RPC in the app keeps using the plain fetch-based rpc().
function rpcWithProgress(url,params,onProgress){
    return new Promise((resolve,reject)=>{
        const xhr=new XMLHttpRequest();
        xhr.open("POST",url,true);
        xhr.setRequestHeader("Content-Type","application/json");
        xhr.withCredentials=true;
        if(xhr.upload&&onProgress){
            xhr.upload.onprogress=(e)=>{ if(e.lengthComputable) onProgress(Math.round(e.loaded/e.total*100)); };
        }
        xhr.onload=()=>{
            let d;
            try{ d=JSON.parse(xhr.responseText); }
            catch(e){ reject(new Error("Bad response from server")); return; }
            if(d.error) reject(new Error(d.error.data?.message||d.error.message||"RPC error"));
            else resolve(d.result);
        };
        xhr.onerror=()=>reject(new Error("Network error — check your connection"));
        xhr.send(JSON.stringify({jsonrpc:"2.0",method:"call",id:(Math.random()*1e9)|0,params}));
    });
}
function haversine(a,b,c,d){const R=6371000,r=Math.PI/180;const x=Math.sin((c-a)*r/2)**2+Math.cos(a*r)*Math.cos(c*r)*Math.sin((d-b)*r/2)**2;return R*2*Math.atan2(Math.sqrt(x),Math.sqrt(1-x));}
function show(id){const e=q("#"+id);if(e)e.style.display="";}
function hide(id){const e=q("#"+id);if(e)e.style.display="none";}
function q(sel){return document.querySelector(sel);}
function mk(tag,cls,html){const el=document.createElement(tag);if(cls)el.className=cls;if(html)el.innerHTML=html;return el;}
function esc(s){return(s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
// Same rule as static/src/js/dispatch_time_utils.js — duplicated here because
// driver_app.js is loaded as a plain <script> tag on the standalone public
// page, not through the OWL asset bundle, so it can't `import` that module.
function fmtStopTime(isoUtc,tzName){
    if(!isoUtc)return"";
    try{
        const d=new Date(isoUtc);
        if(isNaN(d.getTime()))return"";
        return new Intl.DateTimeFormat("en-US",{hour:"numeric",minute:"2-digit",hour12:true,timeZone:tzName||"America/Toronto"}).format(d);
    }catch{return"";}
}
let _tT;
function toast(msg){const t=q("#toast");if(!t)return;t.textContent=msg;t.style.display="block";clearTimeout(_tT);_tT=setTimeout(()=>{t.style.display="none";},3000);}
