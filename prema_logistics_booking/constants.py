"""Shared selection constants for Prema Dispatch logistics domain.

Import from here instead of duplicating selection tuples across models.
"""

# ── Service Mode ──────────────────────────────────────────────────────
SERVICE_MODE = [
    ("dedicated", "Dedicated"),
    ("expedited", "Expedited"),
]

# ── Load Type ─────────────────────────────────────────────────────────
LOAD_TYPE = [
    ("ltl", "LTL"),
    ("ftl", "FTL"),
]

# ── Equipment Requirement ──────────────────────────────────────────────
EQUIPMENT_REQUIREMENT = [
    ("dry", "Dry"),
    ("reefer", "Reefer"),
]

# ── Temperature Mode (migrated from dry/chilled/frozen) ────────────────
TEMPERATURE_MODE = EQUIPMENT_REQUIREMENT  # dry/reefer

# Legacy temperature values — used only for data migration
LEGACY_TEMPERATURE_MODE = [
    ("dry", "Dry"),
    ("chilled", "Chilled"),
    ("frozen", "Frozen"),
]

# ── Booking Channel (constrained) ──────────────────────────────────────
BOOKING_CHANNEL = [
    ("customer_portal", "Customer Portal"),
    ("staff", "Staff"),
    ("phone", "Phone"),
    ("internal", "Internal"),
    ("whatsapp", "WhatsApp"),
    ("invoice", "Invoice"),
    ("custom_quote", "Custom Quote"),
    ("recurring", "Recurring"),
    ("email", "Email"),
    ("api", "API"),
    ("imported", "Imported"),
]

# ── Leg Type ───────────────────────────────────────────────────────────
LEG_TYPE = [
    ("direct", "Direct"),
    ("feeder", "Feeder"),
    ("linehaul", "Linehaul"),
    ("transfer", "Transfer"),
    ("final_delivery", "Final Delivery"),
]

# ── Departure Status ───────────────────────────────────────────────────
DEPARTURE_STATUS = [
    ("planned", "Planned"),
    ("loading", "Loading"),
    ("departed", "Departed"),
    ("en_route", "En Route"),
    ("delayed", "Delayed"),
    ("returning", "Returning"),
    ("back_at_hub", "Back at Hub"),
    ("completed", "Completed"),
    ("cancelled", "Cancelled"),
]

# ── Direction ───────────────────────────────────────────────────────────
DIRECTION = [
    ("eastbound", "Eastbound"),
    ("westbound", "Westbound"),
    ("northbound", "Northbound"),
    ("southbound", "Southbound"),
    ("bidirectional", "Bidirectional"),
    ("local_loop", "Local Loop"),
    ("round_trip", "Round Trip"),
]

# ── Pricing Method ──────────────────────────────────────────────────────
PRICING_METHOD = [
    ("per_km", "Per Kilometre"),
    ("fixed_corridor", "Fixed Corridor"),
    ("minimum_charge", "Minimum Charge"),
    ("custom_quote", "Custom Quote Required"),
]

# ── Route Mode (for estimator) ──────────────────────────────────────────
ROUTE_MODE = [
    ("one_way", "One Way"),
    ("return_to_hub", "Return to Hub"),
    ("current_vehicle_to_stops_to_hub", "Current Vehicle → Stops → Hub"),
]

# ── Pricing Precedence (for customer contract resolution) ───────────────
PRICING_PRECEDENCE = [
    ("fixed_price", "Exact Fixed-Price Contract"),
    ("customer_rate", "Customer-Specific Rate"),
    ("customer_discount", "Customer Percentage Discount"),
    ("standard_rate", "Standard Effective Rate"),
    ("custom_quote", "Custom Quote Required"),
]
