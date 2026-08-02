"""Where We Go — Interactive service coverage and route-planning map."""
import json
from odoo import http
from odoo.http import request


class WhereWeGoMap(http.Controller):

    @http.route("/logistics/where-we-go", type="http", auth="user", website=False)
    def where_we_go_page(self, **kwargs):
        api_key = request.env["ir.config_parameter"].sudo().get_param("google_maps_api_key", "")
        return request.make_response(self._where_we_go_html(api_key), [
            ('Content-Type', 'text/html'),
        ])

    def _where_we_go_html(self, api_key):
        return '''<!DOCTYPE html>
<html><head><title>Where We Go</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<script src="https://maps.googleapis.com/maps/api/js?key=''' + (api_key or '') + '''&libraries=drawing" async defer></script>
<style>body{margin:0;font-family:Arial,sans-serif}#app{display:flex;height:100vh}#panel{width:380px;min-width:380px;background:#fff;overflow-y:auto;border-right:1px solid #ddd}#panel h2{margin:0;font-size:18px}#phdr{background:#1a73e8;color:#fff;padding:16px 20px}#pform{padding:16px 20px}#pform select{width:100%;padding:8px;border:1px solid #ccc;border-radius:4px;margin:4px 0 12px}#pform label{font-weight:600;font-size:13px}#pform button{padding:8px 16px;background:#eee;border:1px solid #ccc;border-radius:4px;cursor:pointer}#result{padding:16px 20px;border-top:1px solid #eee}#result p{margin:4px 0}#map{flex:1;min-width:0}.route-box{background:#e8f5e9;border-left:4px solid #34a853;padding:12px;border-radius:4px;margin-bottom:8px}.btn-price{display:inline-block;padding:8px 18px;background:#1a73e8;color:#fff;text-decoration:none;border-radius:4px;font-weight:600;margin-top:12px}</style></head>
<body><div id="app">
<div id="panel"><div id="phdr"><h2>Where We Go</h2><p style="margin:4px 0 0;font-size:12px;opacity:.9">Select pickup and delivery regions.</p></div>
<div id="pform">
<label>Pickup Region</label><select id="origin"><option value="">Loading...</option></select>
<label>Delivery Region</label><select id="dest"><option value="">-- First select pickup --</option></select>
<button id="reset">Reset</button></div>
<div id="result"><p style="color:#999;font-size:13px">Select origin and destination.</p></div></div>
<div id="map"></div></div>
<script>
var D={regions:[],hubs:[],lanes:[],services:[],deps:[]},map=null,markers=[],lines=[],oid=null,did=null;
function init(){map=new google.maps.Map(document.getElementById('map'),{center:{lat:44.8,lng:-77.0},zoom:7,mapTypeControl:false,streetViewControl:false});load();}
function load(){var x=new XMLHttpRequest();x.open('POST','/logistics/where-we-go/data',true);x.setRequestHeader('Content-Type','application/json');x.onload=function(){if(x.status===200){var j=JSON.parse(x.responseText);if(j.result)j=j.result;D.regions=j.regions||[];D.hubs=j.hubs||[];D.lanes=j.lanes||[];D.services=j.services||[];D.deps=j.departures||[];pop();drawMarkers()}};x.send('{"jsonrpc":"2.0","method":"call","params":{},"id":1}')}
function pop(){var o=document.getElementById('origin');o.innerHTML='<option value="">-- Select Pickup --</option>';D.regions.sort(function(a,b){return(a.display_number||a.id)-(b.display_number||b.id)}).forEach(function(r){o.innerHTML+='<option value="'+r.id+'">'+(r.display_number||r.id)+'. '+r.name+' - '+(r.main_city||'')+'</option>'});o.onchange=function(){oid=parseInt(this.value)||null;updateDests();drawRoute()};document.getElementById('dest').onchange=function(){did=parseInt(this.value)||null;drawRoute()};document.getElementById('reset').onclick=function(){oid=null;did=null;o.value='';document.getElementById('dest').value='';document.getElementById('dest').innerHTML='<option value="">-- First select pickup --</option>';document.getElementById('result').innerHTML='<p style="color:#999">Select origin and destination.</p>';drawRoute()}}
function updateDests(){var d=document.getElementById('dest');d.innerHTML='<option value="">-- Select Delivery --</option>';if(!oid)return;var ids=new Set();D.lanes.forEach(function(l){if(l.origin_id===oid)ids.add(l.dest_id)});D.regions.forEach(function(r){if(ids.has(r.id))d.innerHTML+='<option value="'+r.id+'">'+(r.display_number||r.id)+'. '+r.name+' - '+(r.main_city||'')+'</option>'})}
function drawMarkers(){markers.forEach(function(m){m.setMap(null)});markers=[];D.regions.forEach(function(r){var m=new google.maps.Marker({position:{lat:r.lat||44,lng:r.lng||-78},map:map,label:{text:String(r.display_number||r.id),color:'#fff',fontSize:'11px',fontWeight:'bold'},title:r.name,icon:{path:google.maps.SymbolPath.CIRCLE,scale:12,fillColor:'#1a73e8',fillOpacity:.9,strokeColor:'#fff',strokeWeight:2}});m.addListener('click',function(){document.getElementById('origin').value=r.id;oid=r.id;updateDests();drawRoute()});markers.push(m)});D.hubs.forEach(function(h){var m=new google.maps.Marker({position:{lat:h.lat||43.65,lng:h.lng||-79.66},map:map,icon:{path:google.maps.SymbolPath.CIRCLE,scale:8,fillColor:'#ea4335',fillOpacity:.9,strokeColor:'#fff',strokeWeight:2},title:h.public_name||h.name,label:{text:'HUB',color:'#ea4335',fontSize:'8px',fontWeight:'bold'}});markers.push(m)})}
function drawRoute(){lines.forEach(function(l){l.setMap(null)});lines=[];var r=document.getElementById('result');if(!oid||!did){r.innerHTML='<p style="color:#999">Select origin and destination.</p>';return}var o=D.regions.find(function(x){return x.id===oid});var d=D.regions.find(function(x){return x.id===did});if(!o||!d)return;var lane=D.lanes.find(function(l){return l.origin_id===oid&&l.dest_id===did});var direct=lane&&lane.direct_allowed;var hub=D.hubs.find(function(h){return h.is_default})||D.hubs[0];var path=[];if(direct)path=[{lat:o.lat||44,lng:o.lng||-78},{lat:d.lat||44,lng:d.lng||-78}];else if(hub)path=[{lat:o.lat||44,lng:o.lng||-78},{lat:hub.lat||43.65,lng:hub.lng||-79.66},{lat:d.lat||44,lng:d.lng||-78}];if(path.length>=2){var line=new google.maps.Polyline({path:path,map:map,strokeColor:direct?'#34a853':'#1a73e8',strokeOpacity:.7,strokeWeight:3,icons:[{icon:{path:google.maps.SymbolPath.FORWARD_CLOSED_ARROW,scale:3},offset:'100%'}]});lines.push(line);var b=new google.maps.LatLngBounds();path.forEach(function(p){b.extend(p)});map.fitBounds(b)}var svc=D.services.find(function(s){if(!s.stops||s.stops.length<2)return false;var ids=s.stops.map(function(st){return st.region_id});return ids.indexOf(oid)>=0&&ids.indexOf(did)>=0});var dep=svc?D.deps.find(function(dd){return dd.corridor_id===svc.id}):null;var rt=direct?'Direct Service':(hub?'Via '+hub.public_name:'Via Hub');var pd=svc?(svc.weekday||'Scheduled'):'Contact us';var dd=dep?dep.date:'Next available';r.innerHTML='<div class="route-box"><strong>'+rt+'</strong></div><p><strong>Pickup:</strong> '+o.name+' - '+(o.main_city||'')+'</p><p><strong>Delivery:</strong> '+d.name+' - '+(d.main_city||'')+'</p><p><strong>Service Day:</strong> '+pd+'</p><p><strong>Earliest Delivery:</strong> '+dd+'</p>'+(svc?'<p><strong>Weekly Service:</strong> '+svc.name+'</p>':'')+'<p><strong>Frequency:</strong> Weekly</p>'+(lane?'<p><strong>Road Distance:</strong> '+lane.road_km+' km</p>':'')+'<a href="/web#action=1537" class="btn-price">Check Price</a>'}
if(typeof google!=='undefined'&&google.maps)init();else window.initMap=init;
</script></body></html>'''

    @http.route("/logistics/where-we-go/data", type="json", auth="user")
    def where_we_go_data(self, **kwargs):
        """Return all map data: regions, hubs, lanes, services, departures."""
        Region = request.env["logistics.region"].sudo()
        Lane = request.env["logistics.lane"].sudo()
        Hub = request.env["logistics.hub"].sudo()
        Corridor = request.env["logistics.corridor"].sudo()
        Departure = request.env["logistics.corridor.departure"].sudo()
        Fsa = request.env["logistics.fsa"].sudo()
        from datetime import date

        regions = []
        for r in Region.search([("active", "=", True)]):
            regions.append({
                "id": r.id, "code": r.code, "name": r.name,
                "display_number": r.display_number or r.id,
                "main_city": r.main_city or r.name,
                "lat": r.marker_latitude or 44.0,
                "lng": r.marker_longitude or -78.0,
                "polygon": r.polygon_geojson or "",
                "public_description": r.public_description or "",
                "customer_visible": r.customer_visible,
            })

        hubs = []
        for h in Hub.search([("active", "=", True)]):
            hubs.append({
                "id": h.id, "name": h.name, "public_name": h.public_name,
                "lat": h.latitude or 43.649, "lng": h.longitude or -79.659,
                "is_default": h.is_default, "cross_dock_enabled": h.cross_dock_enabled,
            })

        lanes = []
        for l in Lane.search([("active", "=", True)]):
            lanes.append({
                "id": l.id, "name": l.name,
                "origin_id": l.origin_region_id.id,
                "dest_id": l.destination_region_id.id,
                "direct_allowed": l.direct_allowed if hasattr(l, 'direct_allowed') else True,
                "via_hub_allowed": l.via_hub_allowed if hasattr(l, 'via_hub_allowed') else True,
                "road_km": l.road_km or 0,
                "customer_visible": l.customer_visible if hasattr(l, 'customer_visible') else True,
            })

        services = []
        for c in Corridor.search([("active", "=", True)]):
            stops = []
            for s in c.stop_ids.sorted("sequence"):
                stops.append({
                    "region_id": s.region_id.id if s.region_id else None,
                    "name": s.region_id.name if s.region_id else (s.name or ""),
                    "sequence": s.sequence,
                    "pickup_allowed": s.pickup_allowed if hasattr(s, 'pickup_allowed') else True,
                    "delivery_allowed": s.delivery_allowed if hasattr(s, 'delivery_allowed') else True,
                })
            services.append({
                "id": c.id, "name": c.name, "weekday": c.weekday or "",
                "direction": c.direction, "stops": stops,
                "customer_visible": c.customer_visible if hasattr(c, 'customer_visible') else True,
                "default_vehicle_id": c.default_vehicle_id.id if c.default_vehicle_id else None,
            })

        # Upcoming departures (next 14 days)
        today = date.today()
        departures = []
        for d in Departure.search([
            ("departure_date", ">=", today),
            ("departure_date", "<=", today + date.resolution * 14),
            ("active", "=", True), ("status", "=", "scheduled"),
        ], order="departure_date"):
            departures.append({
                "id": d.id, "date": str(d.departure_date),
                "corridor_id": d.corridor_id.id,
                "corridor_name": d.corridor_id.name or "",
                "vehicle": d.vehicle_id.name or "",
                "status": d.status,
            })

        return {
            "regions": regions, "hubs": hubs, "lanes": lanes,
            "services": services, "departures": departures,
        }
