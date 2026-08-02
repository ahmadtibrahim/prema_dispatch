/** Where We Go — Interactive service coverage map. */
var WWG = {regions:[],hubs:[],lanes:[],services:[],deps:[],map:null,markers:[],lines:[],oid:null,did:null};

function wwgInit() {
    WWG.map = new google.maps.Map(document.getElementById('wwg_map'), {
        center: {lat: 44.8, lng: -77.0}, zoom: 7,
        mapTypeControl: false, streetViewControl: false
    });
    wwgLoad();
}

function wwgLoad() {
    var x = new XMLHttpRequest();
    x.open('POST', '/logistics/where-we-go/data', true);
    x.setRequestHeader('Content-Type', 'application/json');
    x.onload = function() {
        if (x.status === 200) {
            var d = JSON.parse(x.responseText);
            if (d.result) d = d.result;
            WWG.regions = d.regions || [];
            WWG.hubs = d.hubs || [];
            WWG.lanes = d.lanes || [];
            WWG.services = d.services || [];
            WWG.deps = d.departures || [];
            wwgPopulate();
            wwgDrawMarkers();
        }
    };
    x.send('{"jsonrpc":"2.0","method":"call","params":{},"id":1}');
}

function wwgPopulate() {
    var o = document.getElementById('wwg_origin');
    o.innerHTML = '<option value="">— Select —</option>';
    WWG.regions.sort(function(a,b){return (a.display_number||a.id)-(b.display_number||b.id)});
    WWG.regions.forEach(function(r) {
        o.innerHTML += '<option value="'+r.id+'">'+(r.display_number||r.id)+'. '+r.name+' — '+(r.main_city||'')+'</option>';
    });
    o.onchange = function() {
        WWG.oid = parseInt(this.value) || null;
        wwgUpdateDests();
        wwgDrawRoute();
    };
    document.getElementById('wwg_dest').onchange = function() {
        WWG.did = parseInt(this.value) || null;
        wwgDrawRoute();
    };
    document.getElementById('wwg_reset').onclick = function() {
        WWG.oid = null; WWG.did = null;
        o.value = '';
        document.getElementById('wwg_dest').value = '';
        document.getElementById('wwg_dest').innerHTML = '<option value="">— First select pickup —</option>';
        document.getElementById('wwg_result').innerHTML = '<p style="color:#999">Select origin and destination to see route details.</p>';
        wwgDrawRoute();
    };
}

function wwgUpdateDests() {
    var d = document.getElementById('wwg_dest');
    d.innerHTML = '<option value="">— Select —</option>';
    if (!WWG.oid) return;
    var ids = new Set();
    WWG.lanes.forEach(function(l) { if (l.origin_id === WWG.oid) ids.add(l.dest_id); });
    WWG.regions.forEach(function(r) {
        if (ids.has(r.id)) d.innerHTML += '<option value="'+r.id+'">'+(r.display_number||r.id)+'. '+r.name+' — '+(r.main_city||'')+'</option>';
    });
}

function wwgDrawMarkers() {
    WWG.markers.forEach(function(m) { m.setMap(null); });
    WWG.markers = [];
    WWG.regions.forEach(function(r) {
        var m = new google.maps.Marker({
            position: {lat: r.lat||44, lng: r.lng||-78}, map: WWG.map,
            label: {text: String(r.display_number||r.id), color: '#fff', fontSize: '11px', fontWeight: 'bold'},
            title: r.name,
            icon: {path: google.maps.SymbolPath.CIRCLE, scale: 12, fillColor: '#1a73e8', fillOpacity: 0.9, strokeColor: '#fff', strokeWeight: 2}
        });
        m.addListener('click', function() {
            document.getElementById('wwg_origin').value = r.id;
            WWG.oid = r.id; wwgUpdateDests(); wwgDrawRoute();
        });
        WWG.markers.push(m);
    });
    WWG.hubs.forEach(function(h) {
        var m = new google.maps.Marker({
            position: {lat: h.lat||43.65, lng: h.lng||-79.66}, map: WWG.map,
            icon: {path: google.maps.SymbolPath.CIRCLE, scale: 8, fillColor: '#ea4335', fillOpacity: 0.9, strokeColor: '#fff', strokeWeight: 2},
            title: h.public_name || h.name,
            label: {text: 'HUB', color: '#ea4335', fontSize: '8px', fontWeight: 'bold'}
        });
        WWG.markers.push(m);
    });
}

function wwgDrawRoute() {
    WWG.lines.forEach(function(l) { l.setMap(null); });
    WWG.lines = [];
    var r = document.getElementById('wwg_result');
    if (!WWG.oid || !WWG.did) {
        r.innerHTML = '<p style="color:#999">Select origin and destination to see route details.</p>';
        return;
    }
    var o = WWG.regions.find(function(rr){return rr.id===WWG.oid});
    var d = WWG.regions.find(function(rr){return rr.id===WWG.did});
    if (!o || !d) return;
    var lane = WWG.lanes.find(function(l){return l.origin_id===WWG.oid && l.dest_id===WWG.did});
    var direct = lane && lane.direct_allowed;
    var hub = WWG.hubs.find(function(h){return h.is_default}) || WWG.hubs[0];
    var path = [];
    if (direct) {
        path = [{lat: o.lat||44, lng: o.lng||-78}, {lat: d.lat||44, lng: d.lng||-78}];
    } else if (hub) {
        path = [{lat: o.lat||44, lng: o.lng||-78}, {lat: hub.lat||43.65, lng: hub.lng||-79.66}, {lat: d.lat||44, lng: d.lng||-78}];
    }
    if (path.length >= 2) {
        var line = new google.maps.Polyline({
            path: path, map: WWG.map,
            strokeColor: direct ? '#34a853' : '#1a73e8', strokeOpacity: 0.7, strokeWeight: 3,
            icons: [{icon: {path: google.maps.SymbolPath.FORWARD_CLOSED_ARROW, scale: 3}, offset: '100%'}]
        });
        WWG.lines.push(line);
        var b = new google.maps.LatLngBounds(); path.forEach(function(p){b.extend(p)}); WWG.map.fitBounds(b);
    }
    var svc = WWG.services.find(function(s){
        if (!s.stops || s.stops.length < 2) return false;
        var ids = s.stops.map(function(st){return st.region_id});
        return ids.indexOf(WWG.oid) >= 0 && ids.indexOf(WWG.did) >= 0;
    });
    var dep = svc ? WWG.deps.find(function(dd){return dd.corridor_id===svc.id}) : null;
    var rt = direct ? 'Direct Service' : (hub ? 'Via '+hub.public_name : 'Via Hub');
    var pd = svc ? (svc.weekday || 'Scheduled') : 'Contact us';
    var dd = dep ? dep.date : 'Next available';
    r.innerHTML = '<div style="background:#e8f5e9;border-left:4px solid #34a853;padding:12px;border-radius:4px;margin-bottom:8px"><strong>'+rt+'</strong></div>'+
        '<p><strong>Pickup:</strong> '+o.name+' — '+(o.main_city||'')+'</p>'+
        '<p><strong>Delivery:</strong> '+d.name+' — '+(d.main_city||'')+'</p>'+
        '<p><strong>Service Day:</strong> '+pd+'</p>'+
        '<p><strong>Earliest Delivery:</strong> '+dd+'</p>'+
        (svc?'<p><strong>Weekly Service:</strong> '+svc.name+'</p>':'')+
        '<p><strong>Frequency:</strong> Weekly</p>'+
        (lane?'<p><strong>Road Distance:</strong> '+lane.road_km+' km</p>':'')+
        '<div style="margin-top:12px"><a href="/web#action=1537" style="display:inline-block;padding:8px 18px;background:#1a73e8;color:#fff;text-decoration:none;border-radius:4px;font-weight:600">Check Price</a></div>';
}

document.addEventListener('DOMContentLoaded', function() {
    if (typeof google !== 'undefined' && google.maps) { wwgInit(); } else { window.initMap = wwgInit; }
});
