"""Deterministic communication templates for routing explanations.

AI may improve wording only. Routing/pricing decisions are NEVER made here.
"""


class DeterministicCommunication:
    """Generate human-readable routing explanations from machine decisions."""

    @staticmethod
    def explain_direct(origin_code, origin_name, dest_code, dest_name, day):
        return (
            f"Direct Same-Day Service: {origin_name} → {dest_name} is "
            f"approved for direct same-day delivery on {day.title()}. "
            f"Your shipment will move directly from pickup to delivery "
            f"without Hub transfer."
        )

    @staticmethod
    def explain_hub_transfer(origin_code, origin_name, dest_code, dest_name,
                             leg1_corridor, leg2_corridor, leg1_day, leg2_day):
        return (
            f"Hub Transfer: Your shipment will be collected from "
            f"{origin_name} on {leg1_day.title()} via the {leg1_corridor} "
            f"corridor and brought to the Premafirm Mississauga Hub. "
            f"It will then depart on {leg2_day.title()} via the "
            f"{leg2_corridor} corridor for delivery to {dest_name}."
        )

    @staticmethod
    def explain_at_hub(leg2_day, leg2_corridor):
        return (
            f"At Premafirm Hub — Scheduled for {leg2_day.title()} "
            f"{leg2_corridor} departure."
        )

    @staticmethod
    def explain_no_direct_rule(origin_code, dest_code):
        return (
            f"No approved direct-delivery rule exists for "
            f"{origin_code} → {dest_code}. Shipment must route "
            f"through the Hub."
        )

    @staticmethod
    def explain_manual_quote(location_name):
        return (
            f"{location_name} is outside the scheduled Premafirm service "
            f"corridors. A custom quote is required. Our team will review "
            f"your request and provide pricing."
        )

    @staticmethod
    def explain_network_disabled():
        return (
            "Automatic online booking is currently unavailable for this "
            "location. Please contact Premafirm for assistance."
        )

    @staticmethod
    def explain_pickup_not_served(day, next_day, next_date):
        return (
            f"Regular pickup is not available on {day.title()}. "
            f"Next available pickup: {next_day.title()} {next_date}."
        )
