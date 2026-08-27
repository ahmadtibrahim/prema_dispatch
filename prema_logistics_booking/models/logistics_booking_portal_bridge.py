"""Portal confirmation bridge for canonical multi-stop location snapshots.

Pricing sessions intentionally store internal ``prema.dispatch.location``
references. Portal users must never receive broad read ACLs on that model;
confirmation instead snapshots the already-authorized session stops under a
trusted sudo read and returns plain dictionaries to orchestration.
"""

from odoo import models


class LogisticsBookingPortalBridge(models.Model):
    _inherit = "logistics.booking"

    def _build_confirm_stops_from_session(self, session):
        """Build movement_v1 confirmation stops without portal ACL leakage.

        Also fixes the historical integer-vs-record mismatch in the facility
        hours snapshot helper: it expects the canonical facility record, not
        its numeric id.
        """
        from ..services.itinerary_planner import snapshot_facility_hours

        sudo_env = self.env(su=True)
        session = session.sudo()
        pickups, deliveries = [], []

        for stop in session.stop_ids.sorted("sequence"):
            stop = stop.sudo()
            acc = stop.customer_access_id.sudo().exists() if stop.customer_access_id else False
            fac = stop.facility_id.sudo().exists() if stop.facility_id else False
            if acc and acc.facility_id:
                fac = acc.facility_id.sudo().exists()

            hours_snapshot = snapshot_facility_hours(
                sudo_env, fac, stop.stop_type,
            )
            dispatch_loc_id = fac.id if fac else False

            values = {
                "stop_key": stop.stop_key or "",
                "company_name": (
                    (acc.business_name if acc and acc.business_name else "")
                    or (fac.business_name if fac else "")
                    or stop.location_name
                    or ""
                ),
                "street": (
                    acc.street if acc else (fac.street if fac else stop.street or "")
                ),
                "city": acc.city if acc else (fac.city if fac else stop.city or ""),
                "province_state": (
                    (acc.state_id.code if acc and acc.state_id else "")
                    or (fac.province_code if fac else "")
                    or stop.state_code
                    or ""
                ),
                "postal_code": (
                    acc.postal_code if acc else (fac.postal_code if fac else stop.postal_code or "")
                ),
                "formatted_address": (
                    (acc.formatted_address if acc and acc.formatted_address else "")
                    or ((fac.address or fac.street) if fac else "")
                    or stop.street
                    or ""
                ),
                "latitude": (
                    acc.latitude if acc and acc.latitude
                    else (fac.pin_lat if fac and fac.pin_lat else stop.latitude)
                ),
                "longitude": (
                    acc.longitude if acc and acc.longitude
                    else (fac.pin_lng if fac and fac.pin_lng else stop.longitude)
                ),
                "google_place_id": (
                    acc.google_place_id if acc else (fac.google_place_id if fac else "")
                ),
                "contact_name": acc.contact_name if acc else "",
                "phone": acc.contact_phone if acc else "",
                "instructions": stop.instructions or "",
                "pallet_count": stop.pallets or 0,
                "weight_lb": stop.weight_lbs or 0.0,
                "liftgate_required": stop.liftgate_required,
                "dock_available": stop.dock_available,
                "appointment_required": stop.appointment_required,
                "timing_type": stop.timing_type or "flexible",
                "window_start": stop.window_start,
                "window_end": stop.window_end,
                "appointment_time": stop.appointment_time,
                "service_time_minutes": stop.service_time_minutes or 15,
                "timezone": stop.timezone or (acc.timezone if acc else "America/Toronto"),
                "operating_hours_snapshot": hours_snapshot,
                # Explicit canonical pair. Keeping both prevents downstream
                # code from having to guess whether saved_location_id is an
                # access-row id or a physical-facility id.
                "customer_access_id": acc.id if acc else False,
                "facility_id": dispatch_loc_id,
                "saved_location_id": dispatch_loc_id,
            }
            if stop.stop_type == "pickup":
                pickups.append(values)
            else:
                deliveries.append(values)

        return pickups, deliveries

    def _build_confirm_delivery_stops(self, session, address_vals):
        """Run the legacy/simple confirmation snapshot under trusted reads.

        This keeps the 1-pickup/1-delivery control flow working without
        granting portal users direct access to internal dispatch facilities.
        """
        sudo_self = self.sudo()
        return super(LogisticsBookingPortalBridge, sudo_self)._build_confirm_delivery_stops(
            session.sudo(), address_vals,
        )
