# -*- coding: utf-8 -*-
"""One mapping for the public owner-operator form.

The public form posts the names below. Legacy website names (view 5600) are
still accepted so a saved "Other Information" note and an in-flight page can
be replayed without translation loss. Everything not listed here is ignored
by the public intake.
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

# Exactly the 13 choices, alphabetically, for licence/plate dropdowns.
PROVINCE_CHOICES = [
    ("AB", "Alberta"),
    ("BC", "British Columbia"),
    ("MB", "Manitoba"),
    ("NB", "New Brunswick"),
    ("NL", "Newfoundland and Labrador"),
    ("NS", "Nova Scotia"),
    ("NT", "Northwest Territories"),
    ("NU", "Nunavut"),
    ("ON", "Ontario"),
    ("PE", "Prince Edward Island"),
    ("QC", "Quebec"),
    ("SK", "Saskatchewan"),
    ("YT", "Yukon"),
]

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

TDG_STATUS = {"current": "current", "expired": "expired", "none": "none"}
TDG_LEGACY_STATUS = {"Yes": "current", "No": "none", "Expired": "expired"}

VEHICLE_TYPES = {
    "cargo_van": "Cargo Van",
    "straight": "Straight Truck",
    "tractor": "Tractor Truck",
    "semi": "Semi-Truck",
}

CAB_TYPES = {
    "not_applicable": "Not Applicable",
    "day": "Day Cab",
    "night": "Night Cab",
}

CAB_REQUIRED_VEHICLE_TYPES = {"tractor", "semi"}

AUTHORITY_TYPES = {
    "ontario_cvor": "Ontario CVOR",
    "canadian_provincial":
        "Other Canadian provincial / territorial authority",
    "usdot": "USDOT",
    "mc": "MC",
    "other": "Other",
}

AUTHORITY_REVIEW_STATES = {
    "pending": "Pending",
    "review": "Needs review",
    "verified": "Verified",
    "rejected": "Rejected",
}


def _authority_digits(value):
    return "".join(ch for ch in (value or "") if ch.isdigit())


def format_authority_number(authority_type, value):
    """Format only the selected type; never impose CVOR on other types."""
    raw = (value or "").strip()
    if authority_type == "ontario_cvor":
        digits = _authority_digits(raw)[:9]
        return "-".join(digits[i:i + 3] for i in range(0, len(digits), 3))
    if authority_type in ("usdot", "mc"):
        return _authority_digits(raw)[:8]
    return raw[:40]

OWNERSHIPS = {"own": "Owned", "lease": "Leased", "financed": "Financed"}

TRANSMISSIONS = {
    "automatic": "Automatic",
    "automated_manual": "Automated Manual",
    "manual": "Manual",
    "other": "Other",
}

CARGO_BODIES = {
    "dry": "Dry freight",
    "reefer": "Refrigerated / reefer",
    "both": "Reefer and dry capable",
    "other": "Other / not applicable",
}

TRAILER_OWNERSHIPS = {
    "own": "Own / leased trailer",
    "power_only": "Tractor only",
}

TRAILER_TYPES = {
    "dry": "Dry Van",
    "reefer": "Reefer",
    "flatbed": "Flatbed",
    "other": "Other",
}

NOTIFICATION_CHANNELS = {
    "whatsapp": "WhatsApp",
    "email": "Email",
    "both": "WhatsApp and email",
    "none": "Do not send load opportunities",
}

# Legacy service-area constants remain for old notes and backfill only; the
# new form has ONE operating-area control (Canadian provinces) and work
# preferences below it.
SERVICE_AREAS = {
    "Toronto / GTA": "Toronto / GTA local",
    "Ontario wide": "Ontario wide",
    "Quebec": "Quebec",
    "Ontario and Quebec": "Ontario and Quebec",
    "Canada wide": "Canada wide",
    "Selected provinces": "Selected provinces / territories",
}

SERVICE_AREA_CODES = {
    "Toronto / GTA": "toronto_gta",
    "Ontario wide": "ontario_wide",
    "Quebec": "quebec",
    "Ontario and Quebec": "ontario_quebec",
    "Canada wide": "canada_wide",
    "Selected provinces": "selected_provinces",
}

SERVICE_AREA_RAW = {
    "PreferredAreaTorontoGTA": "toronto_gta",
    "PreferredAreaOntarioWide": "ontario_wide",
    "PreferredAreaQuebec": "quebec",
    "PreferredAreaOntarioQuebec": "ontario_quebec",
    "PreferredAreaCanadaWide": "canada_wide",
    "PreferredAreaSelectedProvinces": "selected_provinces",
}

WORK_PREFERENCES = {
    "local": "Local",
    "regional": "Regional",
    "longhaul": "Long haul",
    "flexible": "Flexible",
}

WORK_PREFERENCE_RAW = {
    "WorkPreferenceLocal": "local",
    "WorkPreferenceRegional": "regional",
    "WorkPreferenceLonghaul": "longhaul",
    "WorkPreferenceFlexible": "flexible",
}

EQUIPMENT_RAW_MAP = {
    "CargoBodyType": "cargo_body_type",
    "LegalPayloadLb": "payload_lbs",
    "GVWRLb": "gvwr_lbs",
    "PalletCapacity": "pallet_capacity",
    "CargoLengthFt": "box_length_ft",
    "CargoWidthIn": "cargo_width_in",
    "CargoHeightIn": "cargo_height_in",
    "DoorWidthIn": "door_width_in",
    "DoorHeightIn": "door_clearance_in",
    "Liftgate": "liftgate",
    "LiftgateCapacityLb": "liftgate_capacity_lb",
    "ReeferTemperatureRange": "reefer_temperature_range",
    "TrailerType": "trailer_type",
    "TrailerLengthFt": "trailer_length_ft",
    "TrailerYear": "trailer_year",
    "TrailerUnit": "trailer_unit",
}

WHATSAPP_CONSENT_TEXT = (
    "I agree to receive load quote requests and load offers by WhatsApp at "
    "the number I provided. I may change this preference or opt out at any "
    "time."
)

try:
    import phonenumbers
except ImportError:  # pragma: no cover — phone_validation is installed
    phonenumbers = None

# Raw form name -> internal application field. Names are the current public
# form names first; legacy names remain below for old posts/notes.
FIELD_MAP = {
    "partner_name": "contact_name",
    "email_from": "email",
    "partner_phone": "phone",
    "LegalBusinessName": "business_name",
    "BusinessStructure": "business_structure",
    "HomeCity": "home_city",
    "HomeProvince": "home_province",
    "HomeCityPlaceId": "home_city_place_id",
    "HomeCityLat": "home_latitude",
    "HomeCityLng": "home_longitude",
    "HomeCityVerified": "home_location_verified",
    "CommercialDrivingYears": "commercial_driving_experience",
    "DriverLicenceClass": "licence_class",
    "LicenceJurisdiction": "licence_province",
    "OwnerOperatorYears": "owner_operator_experience",
    "LicenceSuspension": "licence_suspension",
    "RecentCollisions": "recent_collisions",
    "RecentConvictions": "recent_convictions",
    "TDGTraining": "tdg_training",
    "TDGStatus": "tdg_status",
    "TDGSpecialTraining": "tdg_special_training",
    "RecentDrivingWork": "recent_driving_work",
    "VehicleType": "vehicle_type",
    "CabType": "cab_type",
    "TruckMake": "truck_make",
    "TruckModel": "truck_model",
    "TruckMakeModel": "equipment_model",
    "TruckYear": "truck_year",
    "UnitNumber": "unit_number",
    "VehicleOwnership": "vehicle_ownership",
    "Transmission": "transmission",
    "OdometerKm": "odometer_km",
    "TruckPlate": "license_plate",
    "TruckPlateProvince": "plate_province",
    "TruckPlateJurisdiction": "truck_plate_jurisdiction",
    "GVWRLb": "gvwr_lbs",
    "CargoBodyType": "cargo_body_type",
    "TrailerOwnership": "trailer_ownership",
    "TrailerType": "trailer_type",
    "TrailerLengthFt": "trailer_length_ft",
    "TrailerYear": "trailer_year",
    "TrailerUnit": "trailer_unit",
    "LegalPayloadLb": "payload_lbs",
    "PalletCapacity": "pallet_capacity",
    "CargoLengthFt": "box_length_ft",
    "CargoWidthIn": "cargo_width_in",
    "CargoHeightIn": "cargo_height_in",
    "DoorWidthIn": "door_width_in",
    "DoorHeightIn": "door_clearance_in",
    "Liftgate": "liftgate",
    "LiftgateCapacityLb": "liftgate_capacity_lb",
    "LiftgatePlatformLengthIn": "liftgate_platform_length_in",
    "LiftgatePlatformWidthIn": "liftgate_platform_width_in",
    "DockHeightCapable": "dock_height_capable",
    "AirRide": "air_ride",
    "ReeferTemperatureRange": "reefer_temperature_range",
    "CVORNumber": "cvor_number",
    "PreferredLanes": "preferred_service_area",
    "AvailabilityDate": "available_start_date",
    "WorkPreference": "work_preference",
    "LoadNotificationChannel": "load_notification_channel",
    "WhatsAppNumber": "whatsapp_number",
    "LoadNotificationEmail": "load_notification_email",
    "UseContactPhoneForWhatsApp": "use_contact_phone_for_whatsapp",
    "WhatsAppConsentGranted": "whatsapp_consent_granted",
    "WhatsAppConsent": "whatsapp_consent",
    "ApplicationDeclaration": "application_declaration",
    "applicant_notes": "applicant_notes",
}

# Website file input -> checklist document type.
UPLOAD_MAP = {
    "DriverLicenceFront": "licence_front",
    "DriverLicenceBack": "licence_back",
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
    "VehicleType",
)

# Internal fields that every direct _create_from_public caller must supply.
# Legacy direct callers may still send equipment_model in place of Make/Model.
REQUIRED_INTERNAL = (
    "contact_name", "email", "phone", "business_name", "business_structure",
    "home_city", "home_province", "commercial_driving_experience",
    "licence_class", "licence_jurisdiction", "licence_suspension",
    "recent_collisions", "recent_convictions", "vehicle_type",
    "vehicle_ownership", "odometer_km", "application_declaration",
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
PLATE_SPLIT_RE = re.compile(r"\s*[/,]\s*")
NUMBER_CLEAN_RE = re.compile(r"[^0-9.]")


def _text(value):
    return (value or "").strip() if isinstance(value, str) else (
        "" if value is None else str(value).strip())


def normalize_phone(value, region="CA"):
    """Return an E.164 number, or False when it is not a usable number."""
    value = _text(value)
    if not value:
        return False
    if phonenumbers is None:  # pragma: no cover
        digits = re.sub(r"[^0-9]", "", value)
        return "+" + digits if 8 <= len(digits) <= 15 else False
    try:
        number = phonenumbers.parse(value, region)
        if not phonenumbers.is_valid_number(number):
            return False
        return phonenumbers.format_number(
            number, phonenumbers.PhoneNumberFormat.E164)
    except Exception:  # noqa: BLE001 — invalid input is a validation error
        return False


def _choice(value, allowed, label, errors, required=False):
    value = _text(value)
    if not value:
        if required:
            errors.append("Choose a valid %s." % label)
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
    raw = _text(value).replace(",", "").replace(" ", "")
    raw = re.sub(r"(?i)(km|lb|lbs|kg)$", "", raw).strip()
    if not raw:
        if required:
            errors.append("Please provide %s." % label)
        return False
    try:
        number = float(raw)
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
    """Split a legacy combined plate value without inventing a jurisdiction."""
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


def _province_code(value, label, errors, required=False):
    raw = _text(value)
    if not raw:
        if required:
            errors.append("Choose a valid %s." % label)
        return False
    upper = raw.upper()
    if upper in PROVINCES:
        return upper
    # Accept a full province name for legacy posts.
    for code, name in PROVINCES.items():
        if raw.lower() == name.lower():
            return code
    errors.append("Choose a valid %s." % label)
    return False


def _equipment_provided(post):
    return {
        field: True for raw, field in EQUIPMENT_RAW_MAP.items()
        if _text(post.get(raw))
    }


def normalise_authorities(post, errors, strict=True, limit=5):
    """Parse the repeatable authority rows (indexed public field names)."""
    entries = []
    for index in range(limit):
        suffix = str(index)
        raw_type = _text(post.get("AuthorityType" + suffix))
        raw_number = _text(post.get("AuthorityNumber" + suffix))
        if not raw_type and not raw_number:
            continue
        authority_type = _choice(
            raw_type, set(AUTHORITY_TYPES), "authority type", errors)
        if not raw_type and raw_number:
            errors.append("Choose the authority type for entry %s." %
                          (index + 1))
        number = format_authority_number(authority_type, raw_number) \
            if authority_type else raw_number
        digits = _authority_digits(number)
        if authority_type == "ontario_cvor" and len(digits) != 9:
            errors.append(
                "Ontario CVOR entry %s must contain exactly 9 digits." %
                (index + 1))
        elif authority_type in ("usdot", "mc") and not (1 <= len(digits) <= 8):
            errors.append(
                "USDOT/MC entry %s must contain 1–8 digits." % (index + 1))
        elif authority_type and not number:
            errors.append("Enter the authority number for entry %s." %
                          (index + 1))
        jurisdiction = _text(post.get("AuthorityJurisdiction" + suffix))
        other = _text(post.get("AuthorityJurisdictionOther" + suffix))
        if authority_type == "ontario_cvor":
            jurisdiction = "CA-ON"
        elif authority_type in ("usdot", "mc") and not jurisdiction:
            jurisdiction = "US-FEDERAL"
        if authority_type == "other":
            if strict and not other:
                errors.append(
                    "Describe the issuing jurisdiction for authority entry "
                    "%s." % (index + 1))
        elif strict and not jurisdiction:
            errors.append(
                "Choose the issuing jurisdiction for authority entry %s." %
                (index + 1))
        expiry = _parse_date(
            post.get("AuthorityExpiry" + suffix),
            "authority expiry", errors)
        entries.append({
            "index": index,
            "authority_type": authority_type,
            "number": number,
            "jurisdiction_code": jurisdiction,
            "jurisdiction_other": other,
            "expiry_date": expiry,
        })
    return entries


def normalise_public(post, strict=True):
    """Map the public form's names to application fields.

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
    phone = normalize_phone(_text(post.get("partner_phone")))
    if phone:
        vals["phone"] = phone
    elif strict:
        errors.append(
            "Provide a valid contact phone number, including area code.")
    put("LegalBusinessName", "business_name",
        _text(post.get("LegalBusinessName")))
    put("BusinessStructure", "business_structure",
        _choice(post.get("BusinessStructure"), set(BUSINESS_STRUCTURES),
                "business structure", errors))

    # ── truck home base ─────────────────────────────────────────────
    put("HomeCity", "home_city", _text(post.get("HomeCity")))
    home_code = _province_code(
        post.get("HomeProvince"), "home province or territory", errors,
        required=False)
    if home_code:
        vals["home_province"] = PROVINCES[home_code]
        vals["home_province_code"] = home_code
    place_id = _text(post.get("HomeCityPlaceId"))
    verified = _truthy(post.get("HomeCityVerified"))
    if place_id:
        try:
            lat = float(_text(post.get("HomeCityLat")))
            lng = float(_text(post.get("HomeCityLng")))
        except (TypeError, ValueError):
            lat = lng = None
        if lat is not None and lng is not None \
                and -90 <= lat <= 90 and -180 <= lng <= 180 \
                and not (lat == 0.0 and lng == 0.0):
            vals.update({
                "home_city_place_id": place_id,
                "home_latitude": lat,
                "home_longitude": lng,
                "home_location_verified": bool(verified),
                "home_location_review": not verified,
            })
            if not verified:
                vals["home_location_review_reason"] = (
                    "City selection was not verified by Google.")
        else:
            vals["home_location_review"] = True
            vals["home_location_review_reason"] = (
                "City coordinates could not be verified.")
    else:
        vals["home_location_review"] = True
        vals["home_location_review_reason"] = (
            "Location not verified — entered manually.")

    put("CommercialDrivingYears", "commercial_driving_experience",
        _choice(post.get("CommercialDrivingYears"), set(EXPERIENCE_RANGES),
                "commercial driving experience", errors))
    put("DriverLicenceClass", "licence_class",
        _choice(post.get("DriverLicenceClass"), LICENCE_CLASSES,
                "licence class", errors))
    licence_code = _province_code(
        post.get("LicenceJurisdiction"), "licence issuing province or territory",
        errors, required=strict)
    if licence_code:
        vals["licence_province"] = licence_code
        vals["licence_jurisdiction"] = PROVINCES[licence_code]
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
    tdg_status = _choice(
        post.get("TDGStatus"), set(TDG_STATUS), "TDG training status", errors)
    if not tdg_status:
        tdg_status = _choice(
            post.get("TDGTraining"), TDG_LEGACY_STATUS,
            "dangerous goods answer", errors)
    if tdg_status:
        vals["tdg_status"] = tdg_status
        put("TDGTraining", "tdg_training", {
            "current": "yes", "expired": "expired", "none": "no",
        }.get(tdg_status))
        if tdg_status == "current":
            classes = [
                str(code) for code in range(1, 10)
                if _truthy(post.get("TDGClass%s" % code))
            ]
            if classes and not _truthy(post.get("TDGClassesNone")):
                vals["tdg_class_codes"] = classes
            put("TDGSpecialTraining", "tdg_special_training",
                _text(post.get("TDGSpecialTraining")))
    put("RecentDrivingWork", "recent_driving_work",
        _text(post.get("RecentDrivingWork")))

    # ── vehicle ─────────────────────────────────────────────────────
    vehicle_type = _choice(post.get("VehicleType"), set(VEHICLE_TYPES),
                           "vehicle type", errors)
    put("VehicleType", "vehicle_type", vehicle_type)
    cab_type = _choice(post.get("CabType"), set(CAB_TYPES),
                       "cab type", errors)
    if vehicle_type in CAB_REQUIRED_VEHICLE_TYPES:
        if cab_type not in ("day", "night"):
            cab_type = False
            if strict and not any(
                    "cab type" in error for error in errors):
                errors.append(
                    "Choose Day Cab or Night Cab for a tractor or "
                    "semi-truck.")
    elif strict and not cab_type:
        # Cargo vans and straight trucks may keep Not Applicable.
        cab_type = "not_applicable"
    put("CabType", "cab_type", cab_type)
    make = _text(post.get("TruckMake"))
    model = _text(post.get("TruckModel"))
    legacy_model = _text(post.get("TruckMakeModel"))
    if make:
        vals["truck_make"] = make
    if model:
        vals["truck_model"] = model
    if make or model:
        vals["equipment_model"] = ("%s %s" % (make, model)).strip()
    elif legacy_model:
        vals["equipment_model"] = legacy_model
        vals["truck_make"] = legacy_model
    truck_year = _text(post.get("TruckYear"))
    if truck_year and truck_year != "2000 or earlier" \
            and not YEAR_RE.match(truck_year):
        errors.append("Choose a valid model year.")
    elif truck_year:
        vals["truck_year"] = truck_year
    put("UnitNumber", "unit_number", _text(post.get("UnitNumber")))
    put("VehicleOwnership", "vehicle_ownership",
        _choice(post.get("VehicleOwnership"),
                {"Own": "own", "Lease": "lease", "Financed": "financed",
                 "Owned": "own", "Leased": "lease"},
                "ownership answer", errors))
    put("Transmission", "transmission",
        _choice(post.get("Transmission"),
                {"Automatic": "automatic", "Manual": "manual",
                 "Automated manual": "automated_manual",
                 "Automated Manual": "automated_manual",
                 "Other": "other"},
                "transmission", errors))
    put("OdometerKm", "odometer_km",
        _number(post.get("OdometerKm"), "odometer", errors,
                minimum=0, maximum=1000000, integer=True,
                required=strict))
    plate = _text(post.get("TruckPlate")).upper()
    if plate:
        vals["license_plate"] = plate[:32]
    plate_code = _province_code(
        post.get("TruckPlateProvince"), "plate province or territory", errors,
        required=strict)
    if plate_code:
        vals["plate_province"] = plate_code
    if plate and plate_code:
        vals["truck_plate_jurisdiction"] = "%s / %s" % (
            plate, PROVINCES[plate_code])
    legacy_plate = _text(post.get("TruckPlateJurisdiction"))
    if legacy_plate and not plate:
        vals["truck_plate_jurisdiction"] = legacy_plate
        plate_no, jurisdiction = split_plate_jurisdiction(legacy_plate)
        if plate_no:
            vals["license_plate"] = plate_no[:32]
        if jurisdiction:
            vals["plate_jurisdiction"] = jurisdiction[:64]
    put("GVWRLb", "gvwr_lbs",
        _number(post.get("GVWRLb"), "GVWR", errors,
                minimum=0, maximum=200000, integer=True, required=strict))

    liftgate = _choice(post.get("Liftgate"), {"yes": "yes", "no": "no"},
                       "liftgate answer", errors)
    put("Liftgate", "liftgate", liftgate)
    if liftgate == "yes":
        put("LiftgateCapacityLb", "liftgate_capacity_lb",
            _number(post.get("LiftgateCapacityLb"), "liftgate capacity",
                    errors, minimum=1, maximum=20000, integer=True,
                    required=strict))
    air_ride = _choice(post.get("AirRide"), {"yes": "yes", "no": "no"},
                       "air-ride answer", errors)
    put("AirRide", "air_ride", air_ride)
    if strict and not air_ride:
        errors.append("Choose Yes or No for air-ride suspension.")
    # Legacy-only fields: the current form does not ask them, but an older
    # post or a saved note must not lose the historical answer.
    if vehicle_type != "tractor":
        put("LiftgatePlatformLengthIn", "liftgate_platform_length_in",
            _number(post.get("LiftgatePlatformLengthIn"),
                    "liftgate platform length", errors,
                    minimum=0, maximum=150))
        put("LiftgatePlatformWidthIn", "liftgate_platform_width_in",
            _number(post.get("LiftgatePlatformWidthIn"),
                    "liftgate platform width", errors,
                    minimum=0, maximum=150))
        put("DockHeightCapable", "dock_height_capable",
            _choice(post.get("DockHeightCapable"),
                    {"yes": "yes", "no": "no"},
                    "dock-height answer", errors))

    def cargo_fields(required):
        put("CargoBodyType", "cargo_body_type",
            _choice(post.get("CargoBodyType"), set(CARGO_BODIES),
                    "cargo body type", errors))
        put("LegalPayloadLb", "payload_lbs",
            _number(post.get("LegalPayloadLb"), "legal payload", errors,
                    minimum=0, maximum=150000, integer=True,
                    required=required))
        put("PalletCapacity", "pallet_capacity",
            _number(post.get("PalletCapacity"), "pallet capacity", errors,
                    minimum=0, maximum=60, integer=True, required=required))
        put("CargoLengthFt", "box_length_ft",
            _number(post.get("CargoLengthFt"), "cargo length", errors,
                    minimum=0, maximum=60, required=required))
        put("CargoHeightIn", "cargo_height_in",
            _number(post.get("CargoHeightIn"), "cargo height", errors,
                    minimum=0, maximum=150, required=required))
        put("DoorHeightIn", "door_clearance_in",
            _number(post.get("DoorHeightIn"), "rear door height", errors,
                    minimum=0, maximum=150, required=required))

    def reefer_fields(applicable):
        if applicable:
            put("ReeferTemperatureRange", "reefer_temperature_range",
                _text(post.get("ReeferTemperatureRange")))
            vals["reefer_applicable"] = "yes"
        else:
            vals["reefer_applicable"] = "not_applicable"

    door_styles = {"rollup": "rollup", "barn": "barn"}
    if vehicle_type in ("cargo_van", "straight"):
        cargo_fields(required=strict)
        if vehicle_type == "straight":
            put("CargoDoorStyle", "cargo_door_style",
                _choice(post.get("CargoDoorStyle"), door_styles,
                        "cargo door style", errors))
            if strict and not vals.get("cargo_door_style"):
                errors.append("Choose the straight-truck cargo door style.")
        else:
            vals.pop("cargo_door_style", None)
        vals.pop("trailer_door_style", None)
        body = vals.get("cargo_body_type")
        reefer_fields(body == "reefer")
        if body != "reefer":
            vals.pop("reefer_temperature_range", None)
    elif vehicle_type == "tractor":
        # Tractor only: never store trailer, cargo, capacity or reefer.
        vals["trailer_ownership"] = "power_only"
        vals["reefer_applicable"] = "not_applicable"
        for field in (
                "cargo_body_type", "payload_lbs", "pallet_capacity",
                "box_length_ft", "cargo_width_in", "cargo_height_in",
                "door_width_in", "door_clearance_in", "liftgate",
                "liftgate_capacity_lb", "reefer_temperature_range",
                "liftgate_platform_length_in", "liftgate_platform_width_in",
                "dock_height_capable",
                "trailer_type", "trailer_length_ft", "trailer_year",
                "trailer_unit"):
            vals.pop(field, None)
    elif vehicle_type == "semi":
        vals["trailer_ownership"] = "own"
        put("TrailerType", "trailer_type",
            _choice(post.get("TrailerType"), set(TRAILER_TYPES),
                    "trailer type", errors))
        put("TrailerLengthFt", "trailer_length_ft",
            _number(post.get("TrailerLengthFt"), "trailer length", errors,
                    minimum=0, maximum=60, required=strict))
        trailer_year = _text(post.get("TrailerYear"))
        if trailer_year and trailer_year != "2000 or earlier" \
                and not YEAR_RE.match(trailer_year):
            errors.append("Choose a valid trailer model year.")
        elif trailer_year:
            vals["trailer_year"] = trailer_year
        elif strict:
            errors.append("Choose the trailer model year.")
        put("TrailerUnit", "trailer_unit", _text(post.get("TrailerUnit")))
        if strict and not vals.get("trailer_unit"):
            errors.append("Provide the trailer unit / identification.")
        trailer_kind = vals.get("trailer_type")
        if trailer_kind in ("dry", "reefer"):
            cargo_fields(required=strict)
            vals["cargo_body_type"] = (
                "reefer" if trailer_kind == "reefer" else "dry")
            put("TrailerDoorStyle", "trailer_door_style",
                _choice(post.get("TrailerDoorStyle"), door_styles,
                        "trailer door style", errors))
            if strict and not vals.get("trailer_door_style"):
                errors.append("Choose the semi-trailer door style.")
            reefer_fields(trailer_kind == "reefer")
        elif trailer_kind == "flatbed":
            put("LegalPayloadLb", "payload_lbs",
                _number(post.get("LegalPayloadLb"), "legal payload", errors,
                        minimum=0, maximum=150000, integer=True,
                        required=strict))
            put("CargoLengthFt", "box_length_ft",
                _number(post.get("CargoLengthFt"), "cargo length", errors,
                        minimum=0, maximum=60, required=strict))
            vals.pop("trailer_door_style", None)
            vals["reefer_applicable"] = "not_applicable"
            for field in ("cargo_body_type", "pallet_capacity",
                          "cargo_height_in", "door_width_in",
                          "door_clearance_in", "reefer_temperature_range",
                          "liftgate", "liftgate_capacity_lb"):
                vals.pop(field, None)
        else:
            vals["reefer_applicable"] = "not_applicable"
            for field in (
                    "cargo_body_type", "payload_lbs", "pallet_capacity",
                    "box_length_ft", "cargo_width_in", "cargo_height_in",
                    "door_width_in", "door_clearance_in", "liftgate",
                    "liftgate_capacity_lb", "reefer_temperature_range"):
                vals.pop(field, None)
        # Tractor-only fields are never submitted by the public form.
        vals.pop("trailer_ownership", None)
        vals["trailer_ownership"] = "own"

    legacy_cvor = None
    try:
        legacy_cvor = format_cvor(_text(post.get("CVORNumber")))
        if legacy_cvor:
            vals["cvor_number"] = legacy_cvor
    except ValueError as exc:
        errors.append(str(exc))

    entries = normalise_authorities(post, errors, strict=strict)
    if not entries and legacy_cvor:
        # Old page/post compatibility: keep the CVOR as one entry without
        # rewriting the historical cvor_number field.
        entries = [{
            "index": 0,
            "legacy": True,
            "authority_type": "ontario_cvor",
            "number": legacy_cvor,
            "jurisdiction_code": "CA-ON",
            "jurisdiction_other": False,
            "expiry_date": False,
        }]
    if entries:
        vals["authority_entries"] = entries
    elif strict:
        errors.append(
            "Add at least one operating authority or registration.")

    # ── operating area and work preferences (each exactly once) ─────
    service_codes = [
        code for code in PROVINCES
        if _text(post.get("ServiceProvince%s" % code))
    ]
    if service_codes:
        vals["service_province_codes"] = service_codes
    elif strict:
        errors.append("Select at least one province or territory you will serve.")

    # Legacy note/older-page value: keep the historical single answer for
    # backfill, but the current form has no preferred-service-area control.
    legacy_area = _choice(
        post.get("PreferredLanes"), set(SERVICE_AREAS),
        "preferred service area", errors)
    if legacy_area:
        vals["preferred_service_area"] = legacy_area
        vals["preferred_service_area_codes"] = [
            SERVICE_AREA_CODES[legacy_area]]

    pref_codes = [
        code for raw, code in WORK_PREFERENCE_RAW.items()
        if _truthy(post.get(raw))
    ]
    if pref_codes:
        vals["work_preference_codes"] = pref_codes
        vals["work_preference"] = pref_codes[0]
    elif strict:
        errors.append("Select at least one work preference.")

    put("AvailabilityDate", "available_start_date",
        _parse_date(post.get("AvailabilityDate"), "available start date",
                    errors))

    # ── load notifications ──────────────────────────────────────────
    channel = _choice(
        post.get("LoadNotificationChannel"),
        {"whatsapp", "email", "both", "none"},
        "load-notification channel", errors, required=strict)
    if channel:
        vals["load_notification_channel"] = channel
        wants_whatsapp = channel in ("whatsapp", "both")
        wants_email = channel in ("email", "both")
        if wants_whatsapp:
            consent = _truthy(post.get("WhatsAppConsentGranted"))
            if strict and not consent:
                errors.append(
                    "Please agree to WhatsApp load notifications.")
            use_phone = _truthy(post.get("UseContactPhoneForWhatsApp"))
            if use_phone:
                number = vals.get("phone")
                vals["use_contact_phone_for_whatsapp"] = True
            else:
                number = normalize_phone(
                    _text(post.get("WhatsAppNumber")))
                vals["use_contact_phone_for_whatsapp"] = False
            if strict and not number:
                errors.append(
                    "Provide a valid WhatsApp number including country code.")
            if number:
                vals["whatsapp_number"] = number
                vals["whatsapp_consent_granted"] = True
                vals["whatsapp_consent_text"] = WHATSAPP_CONSENT_TEXT
                vals["whatsapp_consent_version"] = "1"
                vals["whatsapp_consent_at"] = datetime.now()
                vals["whatsapp_consent"] = "yes"
        else:
            vals["whatsapp_consent"] = "no"
            vals["whatsapp_consent_granted"] = False
            if not wants_email:
                vals["whatsapp_number"] = False
        if wants_email:
            choice = _text(post.get("LoadNotificationEmailChoice"))
            if choice:
                use_app_email = choice == "application"
            else:
                # Legacy page/post compatibility.
                use_app_email = _truthy(
                    post.get("UseApplicationEmailForNotifications")) \
                    or not _text(post.get("LoadNotificationEmail"))
            vals["use_application_email_for_notifications"] = use_app_email
            if use_app_email:
                vals["load_notification_email"] = vals.get("email")
            else:
                raw_email = _text(post.get("LoadNotificationEmail")).lower()
                if EMAIL_RE.match(raw_email):
                    vals["load_notification_email"] = raw_email
                elif strict:
                    errors.append(
                        "Provide a valid load-notification email address.")
        else:
            vals["load_notification_email"] = False
    elif strict:
        errors.append("Choose how you want to receive load notifications.")

    if _truthy(post.get("LoadNotificationOptOut")):
        vals["load_notification_opt_out"] = True
        vals["load_notification_opt_out_at"] = datetime.now()

    provided = _equipment_provided(post)
    if provided:
        vals["equipment_provided"] = provided
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
    if strict:
        if not vals.get("home_city"):
            errors.append("Please provide your truck home city.")
        if not vals.get("home_province"):
            errors.append("Please provide your home province or territory.")
        if not vals.get("contact_name"):
            errors.append("Please give your full legal name.")
        if not vals.get("email"):
            errors.append("Please give your email address.")
        if not vals.get("truck_make") and not vals.get("equipment_model"):
            errors.append("Please provide the vehicle make.")
        if not vals.get("truck_model"):
            errors.append("Please provide the vehicle model.")
        if not vals.get("truck_year"):
            errors.append("Choose the model year.")
        if not vals.get("transmission"):
            errors.append("Choose the transmission.")
        if not vals.get("license_plate"):
            errors.append("Please provide the licence plate.")
        if not vals.get("plate_province"):
            errors.append("Choose the plate province or territory.")
        if not vals.get("gvwr_lbs"):
            errors.append("Please provide the GVWR.")
    return vals, errors


def parse_other_information(body):
    """Extract ``Name : value`` lines from the saved chatter note.

    The note is HTML; tags become newlines and entities are decoded. The note
    itself is never modified.
    """
    if not body:
        return {}
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
