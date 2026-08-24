"""Validate that a region-to-region leg belongs on a corridor departure."""


class DepartureSpanValidator:
    def __init__(self, env):
        try:
            self.env = env(su=True)
        except TypeError:
            self.env = env

    def validate(self, departure, origin_region=None, destination_region=None):
        if not departure or not departure.corridor_id:
            return self._result(False, "ROUTE_DEPARTURE_MISMATCH")
        if not origin_region or not destination_region:
            return self._result(False, "ROUTE_DEPARTURE_MISMATCH")

        corridor = departure.corridor_id
        segment = corridor.resolve_region_segment(origin_region, destination_region)
        if not segment:
            return self._result(False, "ROUTE_DEPARTURE_MISMATCH")

        stops = corridor.stop_ids.filtered(
            lambda stop: stop.active and stop.region_id
        ).sorted("sequence")
        origin_stops = stops.filtered(
            lambda stop: stop.region_id == origin_region and stop.pickup_allowed
        )
        destination_stops = stops.filtered(
            lambda stop: stop.region_id == destination_region and stop.delivery_allowed
        )
        if not origin_stops or not destination_stops:
            return self._result(False, "ROUTE_DEPARTURE_MISMATCH")

        origin_stop = origin_stops[0]
        destination_stop = destination_stops[0]
        if origin_stop.sequence >= destination_stop.sequence:
            return self._result(False, "ROUTE_DEPARTURE_MISMATCH")

        return self._result(
            True,
            None,
            origin_index=stops.ids.index(origin_stop.id),
            destination_index=stops.ids.index(destination_stop.id),
            origin_stop=origin_stop,
            destination_stop=destination_stop,
        )

    @staticmethod
    def _result(valid, reason, **values):
        return {"valid": valid, "reason": reason, **values}
