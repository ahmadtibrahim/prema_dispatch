from . import logistics_temperature_mixin
from . import res_country_extension
from . import res_country_state_extension
# logistics_saved_location / logistics_saved_location_hours RETIRED in
# 18.0.13.25.0 (SAVED LOCATION CONSOLIDATION): zero live references after
# test-data cleanup; model + tables dropped in migrations/18.0.13.25.0.
from . import logistics_location_customer_access
from . import prema_dispatch_location_hours
from . import prema_dispatch_location_hours_wizard
from . import logistics_direct_delivery_rule
from . import logistics_region
from . import logistics_fsa
from . import logistics_fsa_zone
from . import logistics_city
from . import logistics_region_destination
from . import logistics_equipment_profile
from . import fleet_vehicle
from . import logistics_lane
from . import logistics_service_level
from . import logistics_service_offering
from . import logistics_holiday_calendar
from . import logistics_lane_schedule
from . import logistics_rate_plan
from . import logistics_rate_tier
from . import logistics_pallet_volume_tier
from . import logistics_ftl_regional_minimum
from . import fleet_vehicle_pallet_layout
from . import logistics_fsa_rate_adjustment
from . import logistics_surcharge_type
from . import logistics_customer_rate
from . import logistics_pricing_session
from . import logistics_pricing_session_stop
from . import logistics_booking_line
from . import logistics_booking_pallet
from . import logistics_booking_stop
from . import logistics_booking_leg
from . import logistics_booking
from . import logistics_booking_portal_bridge
from . import logistics_booking_price_adjustment
from . import account_move_booking
from . import logistics_corridor
from . import res_users_temperature_preference
from . import dispatch_job_extension
from . import dispatch_job_risk
from . import dispatch_timeline_extension
from . import dispatch_load_plan_extension
from . import dispatch_stop_extension
from . import dispatch_day_route_proposal
from . import dispatch_item_extension
from . import logistics_custom_quote_send_attempt
from . import logistics_custom_quote
from . import logistics_payment_method
from . import logistics_recurring_agreement
from . import logistics_daily_local
from . import res_partner_logistics
from . import res_config_settings
from . import logistics_hub
from . import logistics_weekly_plan
from . import logistics_booking_execution
from . import logistics_booking_leg_execution
from . import logistics_execution_scenario
from . import logistics_carrier_lane_rate
from . import logistics_booking_leg_carrier_offer
from . import purchase_order_freight
from . import res_partner_carrier
from . import temperature_override
from . import crm_lead_rate_confirmation
# D-A3B — CRM recurring-opportunity bridge (extends logistics.recurring.
# agreement + crm.recurring.opportunity; engine module dependency).
from . import logistics_recurring_agreement_crm_bridge
from . import crm_lead_estimate_bridge
# MP1 §9-13 — Estimator bridge: Dispatch-side authority entry point for
# the Prema AI estimator (three scenarios, availability, intel, pairing,
# route development). Engine side calls env["logistics.estimator.bridge"].
from . import logistics_estimator_bridge

# Weekly Capacity board RPCs (TODO 9-13).
from . import dispatch_job_weekly_capacity
