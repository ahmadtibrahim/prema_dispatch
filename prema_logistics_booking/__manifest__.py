{
    "name": "Prema Logistics Booking",
    "version": "18.0.3.0.0",
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
    "depends": ["base", "base_setup", "mail", "portal", "website", "fleet", "account", "sale_management", "prema_dispatch"],
    "data": [
        "security/logistics_security.xml",
        "security/ir.model.access.csv",
        "data/logistics_config_parameter_data.xml",
        "data/logistics_region_data.xml",
        "data/logistics_equipment_profile_data.xml",
        "data/logistics_sequence_data.xml",
        "data/logistics_fsa_zone_data.xml",
        "data/logistics_city_data.xml",
        "data/logistics_cron.xml",
        "views/logistics_region_views.xml",
        "views/logistics_region_destination_views.xml",
        "views/logistics_fsa_views.xml",
        "views/logistics_fsa_zone_views.xml",
        "views/logistics_city_views.xml",
        "views/logistics_daily_local_views.xml",
        "views/logistics_corridor_views.xml",
        "views/logistics_equipment_profile_views.xml",
        "views/logistics_rate_plan_views.xml",
        "views/logistics_service_level_views.xml",
        "views/logistics_service_offering_views.xml",
        "views/logistics_holiday_calendar_views.xml",
        "views/logistics_lane_schedule_views.xml",
        "views/logistics_lane_views.xml",
        "views/logistics_booking_views.xml",
        "views/logistics_route_run_views.xml",
        "views/logistics_custom_quote_views.xml",
        "views/logistics_recurring_agreement_views.xml",
        "views/logistics_phone_booking_views.xml",
        "views/res_partner_logistics_views.xml",
        "views/rate_simulator_views.xml",
        "views/res_config_settings_views.xml",
        "views/logistics_hub_views.xml",
        "views/menus.xml",
        "views/portal_templates.xml",
        "views/request_quote_templates.xml",
        "views/logistics_network_map_templates.xml",
    ],
    "assets": {
        "web.assets_backend": [
            # Weekly schedule board merged into prema_dispatch's Dispatch Planner.
            # The Owl widget files are kept on disk for reference but no longer loaded.
        ],
    },
    "installable": True,
    "application": True,
    "license": "LGPL-3",
}
