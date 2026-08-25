{
    "name": "Prema Dispatch",
    "version": "18.0.3.17.0",
    "summary": "Booking, planning, live GPS tracking, driver mobile app, interactive truck Load Plans, warehouse loading mode, AI document extraction, VoIP calling and reporting for PremaFirm dispatch operations",
    "category": "Logistics",
    "description": """
Prema Dispatch — Operational Dispatch Management
=================================================

End-to-end freight dispatch for PremaFirm Logistics: book a load, plan and
assign it to a truck, track it live, let the driver run it from a phone, and
report on how it went.

Booking
-------
* Create a Dispatch Booking directly, or generate one from a Sales Order or
  Invoice ("Book Load" button — reuses an existing booking instead of
  duplicating it, with a wizard to choose when one already exists).
* "Generate from Text" on the Sales Order: paste a customer WhatsApp/SMS/
  email and AI extracts pickup/delivery stops, pallet counts, route, dates
  and reefer/liftgate requirements into a new booking.
* Recurring Agreements (provided by Prema Logistics Booking), an ad-hoc
  "Find Available Truck" mid-day load finder, and LTL consolidation tools.

Dispatch Planner
----------------
* Drag-and-drop board: drag a job card onto a truck row to assign it, or use
  click-to-assign mode; manager override for flagged "impossible" assignments.
* Auto Plan (auto-assign unassigned loads to the best truck), Feasibility
  checks, per-truck ETAs, Optimize (re-sequence stops for shortest drive
  time), and one-click Driver Worksheets.
* Stop View / Block View timeline toggle, a live routed map with toll/
  highway/ferry avoidance, and Normal / Wide / Multi layout modes (Multi and
  "New Window" pop out a synced second board for multi-monitor dispatch
  desks).

Booking Board
-------------
* Manager status overview of every booking (Planned, Picked Up, In-Progress,
  Late, Delivered, Cancelled) with route, skids, equipment, priority and
  feasibility at a glance.
* Unassigned-jobs panel for loads with no truck yet, and a one-click
  Unassign action to pull a job back off its truck.

Live Map & Customer Tracking
-----------------------------
* Real-time truck GPS positions and stop pins on a live dispatch map, pushed
  to the Driver App as stops are updated.
* Public shipment tracking page (/dispatch/track/<tracking_number>) with a
  live-polled map and privacy-filtered stop details for customers.

Driver App
----------
* Mobile web app (/dispatch/driver) with weekly schedule, guided stop workflow,
  and a reference route map; the primary Navigate action hands turn-by-turn
  guidance to the Google Maps mobile app.
* Route-level Start Route lives on Home. Stop work unlocks only after the
  driver confirms arrival, then proceeds one required step at a time.
* Pickup flow: verify freight, verify/assign destinations, assign physical
  load positions, capture pallet photos and pickup proof, review, then confirm.
* Delivery flow: verify stop-specific freight, confirm unload, capture POD,
  review, then confirm.
* Come Back Later is a non-terminal Deferred state; operational exceptions
  remain open until resolved. Neither state counts as freight completion.
* Per-stop arrival geofencing, service-time tracking, pin drop/"Use Address",
  entrance photos, POD/delivery-photo evidence, multi-page document scanner,
  built-in chat with dispatch, and click-to-call.

Load Plan & Pallet Positioning
-------------------------------
* Interactive, airline-seat-style truck diagram: tap a vacant position to
  assign a pallet, tap an occupied one to move/swap/unassign/mark loaded —
  in the Planner's full-screen "Pallet Layout" panel and the Driver App's
  Load Plan screen alike.
* One Load Plan per truck/day can carry pallets from several separate
  customer bookings at once while keeping every booking's invoice, sales
  order and revenue completely independent.
* Configurable vehicle layout templates (Straight / Pin-Wheel / Turned) with
  automatic Straight-to-Pin-Wheel capacity escalation — always proposed for
  confirmation, never applied silently, with existing assignments preserved.
  Turned is never auto-proposed and requires manual selection.
* Shared skids (one physical pallet serving several delivery stops) tracked
  correctly as one pallet with multiple stop allocations, never over-counted.
* Stale-plan detection (a stop/pallet/job change after planning flags the
  plan for review without ever silently moving freight), optimistic-locking
  conflict protection, automatic lock with an immutable snapshot at pickup
  departure, and manager unlock with a mandatory logged reason.
* An "UNVERIFIED VEHICLE LAYOUT" warning and required Dispatcher
  acknowledgement (logged) gate loading confirmation until a template's real
  truck measurements have been entered and marked verified.
* Warehouse Loader role and a dedicated /dispatch/warehouse mobile page for
  authenticated warehouse staff — load plan and position access only, no
  rates, revenue, invoices, or truck/driver reassignment.
* A QR code per physical pallet opens a read-only, non-sensitive summary
  (reference, truck, position, stop numbers, shared/exception status) from
  any phone camera — no login needed to view, required to act.
* Rule-based (not AI/ML) position recommendations — advisory only, always
  requiring explicit Accept/Reject, both logged.

VoIP & Calling
--------------
* Dispatcher calling through Odoo's VoIP widget, with dispatch-wide call
  visibility and one-click playback of the matched Asterisk call recording
  from the call log.

AI Document Extraction
-----------------------
* Upload a rate confirmation, BOL or route sheet (or paste text) and click
  "Analyze with AI" to extract stops, addresses, time windows, pallet/weight
  counts, and rate confirmation fields (rate amount, BOL #, PO #, load
  reference) instead of keying them in by hand.

Reporting Suite
----------------
All Jobs, Completed, Stops (driver/date report), Driver Worksheets (live and
historical), Freight Items, LTL Consolidations, On-Time %, Stop Time by
Location, Driver / Truck Performance, Lane Profitability, Fuel Efficiency,
Missed / Late Stops, and POD Aging.

A full in-app user manual is available from the Prema Dispatch app menu.
""",
    "depends": ["base", "mail", "account", "fleet", "sale", "website", "voip", "premafirm_ai_engine"],
    "data": [
        "security/dispatch_security.xml",
        "security/ir.model.access.csv",
        "data/dispatch_stage_data.xml",
        "data/dispatch_cron.xml",
        "data/dispatch_layout_template_data.xml",
        "views/dispatch_stage_views.xml",
        "views/dispatch_stop_views.xml",
        "views/dispatch_item_views.xml",
        "views/dispatch_assignment_log_views.xml",
        "views/dispatch_job_views.xml",
        "views/dispatch_route_adviser_views.xml",
        "views/dispatch_feasibility_views.xml",
        "views/account_move_dispatch_views.xml",
        "views/dispatch_consolidation_wizard_views.xml",
        "views/dispatch_chat_invite_wizard_views.xml",
        "views/voip_call_views.xml",
        "views/sale_order_dispatch_views.xml",
        "views/booking_board_views.xml",
        "views/dispatch_adhoc_wizard_views.xml",
        "views/consolidation_views.xml",
        "views/dispatch_live_map_views.xml",
        "views/dispatch_timeline_views.xml",
        "views/portal_tracking_templates.xml",
        "views/res_partner_dispatch_views.xml",
        "views/res_partner_customer_vendor.xml",
        "views/dispatch_location_views.xml",
        "views/dispatch_reports_views.xml",
        "views/driver_app_template.xml",
        "views/driver_app_optimizer_v8_assets.xml",
        "views/fleet_vehicle_views.xml",
        "views/warehouse_app_template.xml",
        "views/dispatch_manual_template.xml",
        "views/menus.xml",
        "views/dispatch_book_load_wizard_views.xml",
        "views/dispatch_route_visit_views.xml",
        "views/dispatch_load_plan_views.xml",
        "views/dispatch_error_log_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "prema_dispatch/static/src/css/live_map.css",
            "prema_dispatch/static/src/css/dispatch_board.css",
            "prema_dispatch/static/src/css/booking_status_board.css",
            "prema_dispatch/static/src/css/pallet_layout.css",
            "prema_dispatch/static/src/xml/live_map.xml",
            "prema_dispatch/static/src/xml/dispatch_board.xml",
            "prema_dispatch/static/src/xml/booking_status_board.xml",
            "prema_dispatch/static/src/xml/pallet_layout.xml",
            "prema_dispatch/static/src/js/dispatch_time_utils.js",
            "prema_dispatch/static/src/js/google_maps_loader.js",
            "prema_dispatch/static/src/js/live_map.js",
            "prema_dispatch/static/src/js/pallet_layout.js",
            "prema_dispatch/static/src/js/dispatch_board.js",
            "prema_dispatch/static/src/js/booking_status_board.js",
            "prema_dispatch/static/src/js/google_places_widget.js",
            "prema_dispatch/static/src/js/saved_location_search.js",
        ],
        # Driver layers are hard-guarded to /dispatch/driver. V7 remains the
        # workflow authority; the focused v8 layer only enhances Pickup Step 3
        # with a visual truck-position planner and explicit optimizer preview.
        "web.assets_frontend": [
            "prema_dispatch/static/src/js/google_maps_loader.js",
            "prema_dispatch/static/src/js/driver_flow_v6.js",
            "prema_dispatch/static/src/js/driver_native_nav_v6.js",
            "prema_dispatch/static/src/css/driver_guided_flow_v7.css",
            "prema_dispatch/static/src/js/driver_guided_flow_v7.js",
            "prema_dispatch/static/src/js/driver_guided_flow_v7_hotfix.js",
            "prema_dispatch/static/src/css/driver_load_plan_optimizer_v8.css",
            "prema_dispatch/static/src/js/driver_load_plan_optimizer_v8.js",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
