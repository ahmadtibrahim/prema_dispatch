# -*- coding: utf-8 -*-
"""One mapping for the public owner-operator form.

The live website form (website 22, view 5600) posts these names. They are
kept verbatim so an existing page and a saved "Other Information" note can be
replayed without translation loss. Everything that is not in this file is
ignored by the public intake.
"""
import html
import re
from datetime import datetime

# Canada only. The website checkbox suffix is the state code.
PROVINCES = {
    "ON": "Ontario",
    "QC": "Quebec",
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "PE": "Prince Edward Island",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}

BUSINESS_STRUCTURES = {
    "sole": "Sole proprietor",
    "corporation": "Corporation",
    "partnership": "Partnership",
}

EXPERIENCE_RANGES = {
    "Less than 1 year": "Less than 1 year",
    "1–2 years": "1–2 years",
    "3–5 years": "3–5 years",
    "6–10 years": "6–10 years",
    "11–15 years": "11–15 years",
    "16+ years": "16+ years",
}

OWNER_OPERATOR_RANGES = dict(EXPERIENCE_RANGES, **{
    "New owner-operator": "New owner-operator",
})

LICENCE_CLASSES = {"AZ", "DZ", "G", "other"}

YES_NO = {"yes": "Yes", "no": "No"}

COLLISIONS = {"none": "None", "1": "1", "2": "2", "3+": "3 or more"}

TDG = {"yes": "Yes", "no": "No", "expired": "Expired / renewal needed"}

VEHICLE_TYPES = {
    "cargo_van": "Cargo Van",
    "straight": "Straight Truck",
    "tractor": "Tractor / Power Unit",
}

OWNERSHIPS = {"own": "Own", "lease": "Lease", "financed": "Financed"}

TRANSMISSIONS = {
    "automatic": "Automatic",
    "manual": "Manual / standard",
    "automated_manual": "Automated manual",
}

CARGO_BODIES = {
    "dry": "Dry freight",
    "reefer": "Refrigerated / reefer",
    "both": "Reefer and dry capable",
    "other": "Other / not applicable",
}

TRAILER_OWNERSHIPS = {
    "own": "Own / leased trailer",
    "power_only": "Power-only",
}

TRAILER_TYPES = {
    "dry": "Dry Van",
    "reefer": "Reefer",
    "flatbed": "Flatbed",
    "other": "Other",
}

SERVICE_AREAS = {
    "Toronto / GTA": "Toronto / GTA local",
    "Ontario wide": "Ontario wide",
    "Quebec": "Quebec",
    "Ontario and Quebec": "Ontario and Quebec",
    "Canada wide": "Canada wide",
    "Selected provinces": "Selected provinces / territories",
}

WORK_PREFERENCES = {
    "local": "Local",
    "regional": "Regional",
    "longhaul": "Long haul",
    "flexible": "Flexible",
}

# Raw website name -> internal application field. None means "handled
# separately" (provinces and uploads).
FIELD_MAP = {
    "partner_name": "contact_name",
    "email_from": "email",
    "partner_phone": "phone",
    "LegalBusinessName": "business_name",
    "BusinessStructure": "business_structure",
    "HomeCity": "home_city",
    "HomeProvince": "home_province",
    "CommercialDrivingYears": "commercial_driving_experience",
    "DriverLicenceClass": "licence_class",
    "LicenceJurisdiction": "licence_jurisdiction",
    "OwnerOperatorYears": "owner_operator_experience",
    "LicenceSuspension": "licence_suspension",
    "RecentCollisions": "recent_collisions",
    "RecentConvictions": "recent_convictions",
    "TDGTraining": "tdg_training",
    "RecentDrivingWork": "recent_driving_work",
    "VehicleType": "vehicle_type",
    "TruckMakeModel": "equipment_model",
    "TruckYear": "truck_year",
    "UnitNumber": "unit_number",
    "VehicleOwnership": "vehicle_ownership",
    "Transmission": "transmission",
    "OdometerKm": "odometer_km",
    "TruckPlateJurisdiction": "truck_plate_jurisdiction",
    "CargoBodyType": "cargo_body_type",
    "TrailerOwnership": "trailer_ownership",
    "TrailerType": "trailer_type",
    "TrailerLengthFt": "trailer_length_ft",
    "TrailerUnit": "trailer_unit",
    "LegalPayloadLb": "payload_lbs",
    "PalletCapacity": "pallet_capacity",
    "CargoLengthFt": "box_length_ft",
    "CargoWidthIn": "cargo_width_in",
    "CargoHeightIn": "cargo_height_in",
    "DoorHeightIn": "door_clearance_in",
    "Liftgate": "liftgate",
    "LiftgateCapacityLb": "liftgate_capacity_lb",
    "DockHeightCapable": "dock_height_capable",
    "AirRide": "air_ride",
    "ReeferTemperatureRange": "reefer_temperature_range",
    "CVORNumber": "cvor_number",
    "InsuranceAgent": "insurance_agent_name",
    "InsuranceAgentEmail": "insurance_agent_email",
    "PreferredLanes": "preferred_service_area",
    "AvailabilityDate": "available_start_date",
    "WorkPreference": "work_preference",
    "WhatsAppConsent": "whatsapp_consent",
    "ApplicationDeclaration": "application_declaration",
    "applicant_notes": "applicant_notes",
}

# Website file input -> checklist document type.
UPLOAD_MAP = {
    "CVORCertificate": "cvor",
    "InsuranceCertificate": "coi_applicant",
    "BusinessRegistration": "business_registration",
    "VehicleRegistrationInspection": "vehicle_registration",
    "TruckFront": "photo_front",
    "TruckRear": "photo_rear",
    "TruckDriverSide": "photo_left",
    "TruckPassengerSide": "photo_right",
    "CargoInterior": "photo_cargo_interior",
}

REQUIRED_RAW = (
    "partner_name", "email_from", "partner_phone",
    "LegalBusinessName", "BusinessStructure", "HomeCity", "HomeProvince",
    "CommercialDrivingYears", "DriverLicenceClass", "LicenceJurisdiction",
    "LicenceSuspension", "RecentCollisions", "RecentConvictions",
    "VehicleType", "TruckMakeModel", "VehicleOwnership", "OdometerKm",
    "TruckPlateJurisdiction", "PreferredLanes", "WhatsAppConsent",
    "ApplicationDeclaration",
)

# Internal fields that every direct _create_from_public caller must supply.
REQUIRED_INTERNAL = (
    "contact_name", "email", "phone", "business_name", "business_structure",
    "home_city", "home_province", "commercial_driving_experience",
    "licence_class", "licence_jurisdiction", "licence_suspension",
    "recent_collisions", "recent_convictions", "vehicle_type",
    "equipment_model", "vehicle_ownership", "odometer_km",
    "truck_plate_jurisdiction", "preferred_service_area",
    "whatsapp_consent", "application_declaration",
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
PLATE_SPLIT_RE = re.compile(r"\s*[/,]\s*")


def _text(value):
    return (value or "").strip() if isinstance(value, str) else (
        "" if value is None else str(value).strip())


def _choice(value, allowed, label, errors):
    value = _text(value)
    if not value:
        return False
    if isinstance(allowed, dict):
        if value not in allowed:
            errors.append("Choose a valid %s." % label)
            return False
        return allowed[value]
    if value not in allowed:
        errors.append("Choose a valid %s." % label)
        return False
    return value


def _number(value, label, errors, minimum=0.0, maximum=None,
            integer=False, required=False):
    value = _text(value)
    if not value:
        if required:
            errors.append("Please provide %s." % label)
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        errors.append("%s must be a number." % label)
        return False
    if integer and number != int(number):
        errors.append("%s must be a whole number." % label)
        return False
    if number < minimum or (maximum is not None and number > maximum):
        errors.append("%s is out of range." % label)
        return False
    return int(number) if integer else number


def format_cvor(value):
    """Return ###-###-### or raise ValueError. Five digits is not a CVOR."""
    digits = re.sub(r"[^0-9]", "", value or "")
    if not digits:
        return False
    if len(digits) != 9:
        raise ValueError("CVOR must contain exactly 9 digits.")
    return "%s-%s-%s" % (digits[:3], digits[3:6], digits[6:])


def split_plate_jurisdiction(value):
    """Split the combined website field without inventing a jurisdiction."""
    raw = _text(value)
    if not raw:
        return False, False
    parts = [p for p in PLATE_SPLIT_RE.split(raw) if p]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return raw, False


def _truthy(value):
    return _text(value).lower() in ("1", "true", "on", "yes", "confirmed")


def _parse_date(value, label, errors):
    value = _text(value)
    if not value:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        errors.append("%s is not a valid date." % label)
        return False


def normalise_public(post, strict=True):
    """Map the website's names to application fields.

    Returns ``(vals, errors)``. In strict mode every required field is
    enforced. In non-strict mode (the audit backfill) a missing or malformed
    answer is skipped, never turned into a false "No".
    """
    errors = []
    vals = {}

    if strict:
        for raw in REQUIRED_RAW:
            if not _text(post.get(raw)):
                errors.append("Please complete “%s”." % raw)

    def put(raw, internal, value):
        if value is not False and value is not None and value != "":
            vals[internal] = value

    put("partner_name", "contact_name", _text(post.get("partner_name")))
    put("email_from", "email", _text(post.get("email_from")).lower())
    put("partner_phone", "phone", _text(post.get("partner_phone")))
    put("LegalBusinessName", "business_name",
        _text(post.get("LegalBusinessName")))
    put("BusinessStructure", "business_structure",
        _choice(post.get("BusinessStructure"), set(BUSINESS_STRUCTURES),
                "business structure", errors))
    put("HomeCity", "home_city", _text(post.get("HomeCity")))
    home_province = _choice(
        post.get("HomeProvince"), set(PROVINCES) | {"other"},
        "home province", errors)
    put("HomeProvince", "home_province", home_province)
    put("CommercialDrivingYears", "commercial_driving_experience",
        _choice(post.get("CommercialDrivingYears"), set(EXPERIENCE_RANGES),
                "commercial driving experience", errors))
    put("DriverLicenceClass", "licence_class",
        _choice(post.get("DriverLicenceClass"), LICENCE_CLASSES,
                "licence class", errors))
    put("LicenceJurisdiction", "licence_jurisdiction",
        _text(post.get("LicenceJurisdiction")))
    put("OwnerOperatorYears", "owner_operator_experience",
        _choice(post.get("OwnerOperatorYears"), set(OWNER_OPERATOR_RANGES),
                "owner-operator experience", errors))
    put("LicenceSuspension", "licence_suspension",
        _choice(post.get("LicenceSuspension"), {"No": "no", "Yes": "yes"},
                "licence suspension answer", errors))
    put("RecentCollisions", "recent_collisions",
        _choice(post.get("RecentCollisions"),
                {"None": "none", "1": "1", "2": "2", "3+": "3+"},
                "collision answer", errors))
    put("RecentConvictions", "recent_convictions",
        _choice(post.get("RecentConvictions"),
                {"None": "none", "1": "1", "2": "2", "3+": "3+"},
                "conviction answer", errors))
    put("TDGTraining", "tdg_training",
        _choice(post.get("TDGTraining"),
                {"Yes": "yes", "No": "no", "Expired": "expired"},
                "dangerous goods answer", errors))
    put("RecentDrivingWork", "recent_driving_work",
        _text(post.get("RecentDrivingWork")))

    vehicle_type = _choice(post.get("VehicleType"), set(VEHICLE_TYPES),
                           "vehicle type", errors)
    put("VehicleType", "vehicle_type", vehicle_type)
    put("TruckMakeModel", "equipment_model",
        _text(post.get("TruckMakeModel")))
    truck_year = _text(post.get("TruckYear"))
    if truck_year and truck_year != "2000 or earlier" \
            and not YEAR_RE.match(truck_year):
        errors.append("Choose a valid model year.")
    elif truck_year:
        vals["truck_year"] = truck_year
    put("UnitNumber", "unit_number", _text(post.get("UnitNumber")))
    put("VehicleOwnership", "vehicle_ownership",
        _choice(post.get("VehicleOwnership"),
                {"Own": "own", "Lease": "lease", "Financed": "financed"},
                "ownership answer", errors))
    put("Transmission", "transmission",
        _choice(post.get("Transmission"),
                {"Automatic": "automatic", "Manual": "manual",
                 "Automated manual": "automated_manual"},
                "transmission", errors))
    put("OdometerKm", "odometer_km",
        _number(post.get("OdometerKm"), "odometer", errors,
                minimum=0, maximum=9999999, integer=True,
                required=strict))
    plate = _text(post.get("TruckPlateJurisdiction"))
    if plate:
        vals["truck_plate_jurisdiction"] = plate
        plate_no, jurisdiction = split_plate_jurisdiction(plate)
        if plate_no:
            vals["license_plate"] = plate_no[:32]
        if jurisdiction:
            vals["plate_jurisdiction"] = jurisdiction[:64]

    if vehicle_type == "tractor":
        put("TrailerOwnership", "trailer_ownership",
            _choice(post.get("TrailerOwnership"),
                    {"own": "own", "power_only": "power_only"},
                    "trailer ownership", errors))
        put("TrailerType", "trailer_type",
            _choice(post.get("TrailerType"), set(TRAILER_TYPES),
                    "trailer type", errors))
        put("TrailerLengthFt", "trailer_length_ft",
            _number(post.get("TrailerLengthFt"), "trailer length", errors,
                    minimum=1, maximum=60))
        put("TrailerUnit", "trailer_unit", _text(post.get("TrailerUnit")))
        if strict and _text(post.get("TrailerOwnership")) == "own" \
                and not _text(post.get("TrailerType")):
            errors.append("Choose the trailer type.")
        # Cargo-capacity answers are not part of a power unit. They are
        # ignored even if a caller posts them by hand.
    elif vehicle_type in ("cargo_van", "straight"):
        put("CargoBodyType", "cargo_body_type",
            _choice(post.get("CargoBodyType"), set(CARGO_BODIES),
                    "cargo body type", errors))
        put("LegalPayloadLb", "payload_lbs",
            _number(post.get("LegalPayloadLb"), "legal payload", errors,
                    minimum=0, maximum=150000))
        put("PalletCapacity", "pallet_capacity",
            _number(post.get("PalletCapacity"), "pallet capacity", errors,
                    minimum=0, maximum=60, integer=True))
        put("CargoLengthFt", "box_length_ft",
            _number(post.get("CargoLengthFt"), "cargo length", errors,
                    minimum=0, maximum=60))
        put("CargoWidthIn", "cargo_width_in",
            _number(post.get("CargoWidthIn"), "cargo width", errors,
                    minimum=0, maximum=120))
        put("CargoHeightIn", "cargo_height_in",
            _number(post.get("CargoHeightIn"), "cargo height", errors,
                    minimum=0, maximum=150))
        put("DoorHeightIn", "door_clearance_in",
            _number(post.get("DoorHeightIn"), "door opening height", errors,
                    minimum=0, maximum=150))
        put("Liftgate", "liftgate",
            _choice(post.get("Liftgate"), {"yes": "yes", "no": "no"},
                    "liftgate answer", errors))
        put("LiftgateCapacityLb", "liftgate_capacity_lb",
            _number(post.get("LiftgateCapacityLb"), "liftgate capacity",
                    errors, minimum=0, maximum=20000))
        put("DockHeightCapable", "dock_height_capable",
            _choice(post.get("DockHeightCapable"),
                    {"yes": "yes", "no": "no"},
                    "dock-height answer", errors))
        put("ReeferTemperatureRange", "reefer_temperature_range",
            _text(post.get("ReeferTemperatureRange")))
    put("AirRide", "air_ride",
        _choice(post.get("AirRide"), {"yes": "yes", "no": "no"},
                "air-ride answer", errors))

    try:
        cvor = format_cvor(_text(post.get("CVORNumber")))
        if cvor:
            vals["cvor_number"] = cvor
    except ValueError as exc:
        errors.append(str(exc))

    put("InsuranceAgent", "insurance_agent_name",
        _text(post.get("InsuranceAgent")))
    agent_email = _text(post.get("InsuranceAgentEmail")).lower()
    if agent_email:
        if EMAIL_RE.match(agent_email):
            vals["insurance_agent_email"] = agent_email
        else:
            errors.append("The insurance agent email is not valid.")

    service_codes = [
        code for code in PROVINCES
        if _text(post.get("ServiceProvince%s" % code))
    ]
    if service_codes:
        vals["service_province_codes"] = service_codes

    preferred = _choice(post.get("PreferredLanes"), set(SERVICE_AREAS),
                        "preferred service area", errors)
    put("PreferredLanes", "preferred_service_area", preferred)
    if strict and preferred == "Selected provinces" and not service_codes:
        errors.append(
            "Select at least one province or territory you will serve.")
    put("AvailabilityDate", "available_start_date",
        _parse_date(post.get("AvailabilityDate"), "available start date",
                    errors))
    put("WorkPreference", "work_preference",
        _choice(post.get("WorkPreference"), set(WORK_PREFERENCES),
                "work preference", errors))
    put("WhatsAppConsent", "whatsapp_consent",
        _choice(post.get("WhatsAppConsent"),
                {"yes": "yes", "no": "no"}, "WhatsApp answer", errors))
    if _truthy(post.get("ApplicationDeclaration")):
        vals["application_declaration"] = True
    elif strict:
        errors.append("Please confirm the application declaration.")

    notes = _text(post.get("applicant_notes"))
    if notes:
        vals["applicant_notes"] = notes

    email = vals.get("email")
    if email and not EMAIL_RE.match(email):
        errors.append("That email address does not look valid.")
    phone = vals.get("phone")
    if phone and len(re.sub(r"[^0-9]", "", phone)) < 7:
        errors.append("Please provide a complete phone number.")

    if strict:
        if not vals.get("home_city"):
            errors.append("Please provide your truck home city.")
        if not vals.get("home_province"):
            errors.append("Please provide your home province or territory.")
        if not vals.get("contact_name"):
            errors.append("Please give your full name.")
        if not vals.get("email"):
            errors.append("Please give your email address.")
    return vals, errors


def parse_other_information(body):
    """Extract ``Name : value`` lines from the saved chatter note.

    The note is HTML; tags become newlines and entities are decoded. The note
    itself is never modified.
    """
    if not body:
        return {}
    # The chatter stores the body HTML-escaped (e.g. &lt;p&gt;), so decode
    # first and only then strip the tags.
    text = re.sub(r"<[^>]+>", "\n", html.unescape(body))
    if "Other Information:" not in text:
        return {}
    values = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if key in FIELD_MAP or key.startswith("ServiceProvince") \
                or key in UPLOAD_MAP:
            values[key] = value
    return values
