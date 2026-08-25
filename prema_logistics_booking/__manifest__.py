{
    "name": "Prema Logistics Booking",
    "version": "18.0.13.19.0",
    "summary": "Private customer LTL/FTL pricing, scheduling, and booking engine "
               "for PremaFirm Logistics — integrates with Prema Dispatch.",
    "category": "Logistics",
    "description": """
Prema Logistics Booking (foundation phase)
===========================================

Internal geography/pricing foundation for the customer booking portal:
Regions, FSAs, Equipment Profiles, Lanes, Service Levels, Service Offerings,
Lane Schedules, and Holiday Calendars.

HIDDEN BY DESIGN: no customer-facing routes exist yet. Everything in this
phase is internal-staff configuration only. See CLAUDE.md in the module
root for architecture notes, decisions, and phase status.
""",
    "author": "PremaFirm Logistics",
    "depends": ["base", "base_setup", "mail", "portal", "website", "fleet", "account", "sale_management", "prema_dispatch", "agent_wa"],
    "data": [
        "security/logistics_security.xml",
        "security/ir.model.access.csv",
        "data/logistics_config_parameter_data.xml",
        "data/logistics_region_data.xml",
        "data/logistics_official_region_catalog.xml",
        # "data/logistics_equipment_profile_data.xml",  # ARCHIVED — fleet.vehicle is sole authority
        "data/logistics_sequence_data.xml",
        "data/logistics_fsa_zone_data.xml",
        "data/logistics_city_data.xml",
        "data/logistics_cron.xml",
        "views/logistics_hub_views.xml",
        "views/logistics_region_views.xml",
        "views/logistics_fsa_views.xml",
        "views/logistics_fsa_zone_views.xml",
        "views/logistics_city_views.xml",
        "views/logistics_daily_local_views.xml",
        "views/logistics_corridor_views.xml",
        "views/fleet_vehicle_pallet_layout_views.xml",
        # "views/logistics_equipment_profile_views.xml",  # ARCHIVED — fleet.vehicle is sole authority
        "views/logistics_service_level_views.xml",
        "views/logistics_service_offering_views.xml",
        "views/logistics_holiday_calendar_views.xml",
        "views/logistics_saved_location_views.xml",
        "views/logistics_location_customer_access_views.xml",
        "views/dispatch_stop_integrity_views.xml",
        "views/logistics_booking_views.xml",
        "views/logistics_custom_quote_views.xml",
        "views/logistics_recurring_agreement_views.xml",
        "views/logistics_phone_booking_views.xml",
        # menus.xml must load before any file that references its menu xmlids
        # (logistics_direct_delivery_views.xml, logistics_weekly_plan_views.xml)
        "views/menus.xml",
        "views/logistics_direct_delivery_views.xml",
        "views/logistics_weekly_plan_views.xml",
        "views/res_partner_logistics_views.xml",
        "views/res_config_settings_views.xml",
        "views/res_country_views.xml",
        "views/portal_templates.xml",
        "views/portal_saved_locations_templates.xml",
        "views/portal_booking_templates.xml",
        "views/portal_cleanup_v6.xml",
        "views/request_quote_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "prema_logistics_booking/static/src/css/where_we_go.css",
            "prema_logistics_booking/static/src/xml/where_we_go_action.xml",
            "prema_logistics_booking/static/src/js/where_we_go_action.js",
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
