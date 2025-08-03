"""
Core itinerary generation logic for fluctour.
"""

import random
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
import logging
from .maps_client import MapsClient


class ItineraryGenerator:
    """Generate travel itineraries using Google Maps data."""

    def __init__(self, maps_client: MapsClient):
        """Initialize the itinerary generator."""
        self.maps_client = maps_client
        self.logger = logging.getLogger(__name__)

    def generate_itinerary(
        self,
        start_location: str,
        end_location: str,
        start_date: datetime,
        end_date: datetime,
        constraint_locations: List[str] = None,
        max_stops: int = 5,
        min_stay: int = 1,
    ) -> Dict:
        """Generate a complete travel itinerary."""

        # Calculate trip duration
        total_days = (end_date - start_date).days
        if total_days < min_stay:
            raise ValueError(
                f"Trip duration ({total_days} days) is less than minimum stay ({min_stay} days)"
            )

        # Geocode start and end locations
        start_geo = self.maps_client.geocode_location(start_location)
        end_geo = self.maps_client.geocode_location(end_location)

        self.logger.info(
            f"Planning route from {start_geo['formatted_address']} to {end_geo['formatted_address']}"
        )

        # Find intermediate stops
        intermediate_stops = self._find_intermediate_stops(
            start_geo,
            end_geo,
            constraint_locations,
            max_stops,
            total_days,
            min_stay,
        )

        # Create the complete route
        all_locations = [start_geo] + intermediate_stops + [end_geo]

        # Distribute days across locations
        daily_schedule = self._distribute_days(
            all_locations, start_date, total_days, min_stay
        )

        # Generate travel suggestions
        travel_suggestions = self._generate_travel_suggestions(all_locations)

        return {
            "start_location": start_location,
            "end_location": end_location,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "total_days": total_days,
            "locations": all_locations,
            "daily_schedule": daily_schedule,
            "travel_suggestions": travel_suggestions,
        }

    def _find_intermediate_stops(
        self,
        start_geo: Dict,
        end_geo: Dict,
        constraint_locations: List[str],
        max_stops: int,
        total_days: int,
        min_stay: int,
    ) -> List[Dict]:
        """Find interesting intermediate stops along the route."""

        # Calculate how many stops we can realistically have
        available_days = total_days - (
            2 * min_stay
        )  # Reserve days for start and end
        max_possible_stops = min(max_stops, available_days // min_stay)

        if max_possible_stops <= 0:
            return []

        # Get route information
        route_info = self.maps_client.get_directions(
            f"{start_geo['lat']},{start_geo['lng']}",
            f"{end_geo['lat']},{end_geo['lng']}",
        )

        intermediate_stops = []

        # If constraint locations are provided, try to include them
        if constraint_locations:
            constraint_stops = self._process_constraint_locations(
                constraint_locations, start_geo, end_geo, max_possible_stops
            )
            intermediate_stops.extend(constraint_stops)

        # Fill remaining slots with interesting places along the route
        remaining_stops = max_possible_stops - len(intermediate_stops)
        if remaining_stops > 0:
            route_stops = self._find_places_along_route(
                start_geo, end_geo, remaining_stops
            )
            intermediate_stops.extend(route_stops)

        # Sort stops by distance from start to create logical order
        intermediate_stops = self._sort_stops_by_route(
            start_geo, end_geo, intermediate_stops
        )

        return intermediate_stops[:max_possible_stops]

    def _process_constraint_locations(
        self,
        constraint_locations: List[str],
        start_geo: Dict,
        end_geo: Dict,
        max_stops: int,
    ) -> List[Dict]:
        """Process constraint locations and select the best ones."""

        constraint_stops = []

        for location in constraint_locations:
            try:
                geo_location = self.maps_client.geocode_location(location)

                # Check if this location is reasonably along the route
                if self._is_location_reasonable(
                    start_geo, end_geo, geo_location
                ):
                    constraint_stops.append(geo_location)

                    if len(constraint_stops) >= max_stops:
                        break

            except Exception as e:
                self.logger.warning(
                    f"Could not process constraint location '{location}': {e}"
                )
                continue

        return constraint_stops

    def _find_places_along_route(
        self, start_geo: Dict, end_geo: Dict, num_stops: int
    ) -> List[Dict]:
        """Find interesting places along the route."""

        places = []

        # Create points along the route
        for i in range(1, num_stops + 1):
            ratio = i / (num_stops + 1)

            # Interpolate coordinates
            lat = start_geo["lat"] + (end_geo["lat"] - start_geo["lat"]) * ratio
            lng = start_geo["lng"] + (end_geo["lng"] - start_geo["lng"]) * ratio

            # Search for interesting places near this point
            search_location = {"lat": lat, "lng": lng}
            nearby_places = self.maps_client.search_places_in_area(
                search_location, radius=30000
            )

            if nearby_places:
                # Select the best rated place that's not too close to start/end
                for place in nearby_places:
                    if (
                        self._calculate_distance(place, start_geo) > 10000
                        and self._calculate_distance(place, end_geo) > 10000
                    ):
                        places.append(place)
                        break

        return places

    def _is_location_reasonable(
        self, start_geo: Dict, end_geo: Dict, location: Dict
    ) -> bool:
        """Check if a location is reasonable for the route."""

        # Calculate distances
        start_to_location = self._calculate_distance(start_geo, location)
        location_to_end = self._calculate_distance(location, end_geo)
        direct_distance = self._calculate_distance(start_geo, end_geo)

        # Location should not add more than 50% to the total distance
        total_with_stop = start_to_location + location_to_end
        return total_with_stop <= direct_distance * 1.5

    def _calculate_distance(self, loc1: Dict, loc2: Dict) -> float:
        """Calculate approximate distance between two locations in meters."""
        import math

        lat1, lng1 = math.radians(loc1["lat"]), math.radians(loc1["lng"])
        lat2, lng2 = math.radians(loc2["lat"]), math.radians(loc2["lng"])

        dlat = lat2 - lat1
        dlng = lng2 - lng1

        a = (
            math.sin(dlat / 2) ** 2
            + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
        )
        c = 2 * math.asin(math.sqrt(a))

        # Earth's radius in meters
        r = 6371000

        return c * r

    def _sort_stops_by_route(
        self, start_geo: Dict, end_geo: Dict, stops: List[Dict]
    ) -> List[Dict]:
        """Sort intermediate stops by their position along the route."""

        def route_position(stop):
            # Calculate how far along the route this stop is
            start_to_stop = self._calculate_distance(start_geo, stop)
            start_to_end = self._calculate_distance(start_geo, end_geo)
            return start_to_stop / start_to_end if start_to_end > 0 else 0

        return sorted(stops, key=route_position)

    def _distribute_days(
        self,
        locations: List[Dict],
        start_date: datetime,
        total_days: int,
        min_stay: int,
    ) -> List[Dict]:
        """Distribute days across all locations."""

        if len(locations) == 0:
            return []

        # Calculate days per location
        base_days_per_location = max(min_stay, total_days // len(locations))
        remaining_days = total_days - (base_days_per_location * len(locations))

        daily_schedule = []
        current_date = start_date

        for i, location in enumerate(locations):
            # Assign base days plus any remaining days to interesting locations
            days_here = base_days_per_location
            if (
                remaining_days > 0 and i < len(locations) - 1
            ):  # Don't extend the last location
                extra_days = min(remaining_days, 1)
                days_here += extra_days
                remaining_days -= extra_days

            # For the last location, use all remaining days
            if i == len(locations) - 1:
                days_here += remaining_days

            end_date_here = current_date + timedelta(days=days_here)

            schedule_entry = {
                "location": location,
                "start_date": current_date.strftime("%b %d"),
                "end_date": end_date_here.strftime("%b %d"),
                "days": days_here,
                "google_maps_url": self.maps_client.get_google_maps_url(
                    location
                ),
            }

            daily_schedule.append(schedule_entry)
            current_date = end_date_here

        return daily_schedule

    def _generate_travel_suggestions(self, locations: List[Dict]) -> List[Dict]:
        """Generate travel suggestions between consecutive locations."""

        suggestions = []

        for i in range(len(locations) - 1):
            current_loc = locations[i]
            next_loc = locations[i + 1]

            # Get directions
            directions = self.maps_client.get_directions(
                f"{current_loc['lat']},{current_loc['lng']}",
                f"{next_loc['lat']},{next_loc['lng']}",
            )

            if directions and "legs" in directions:
                leg = directions["legs"][0]

                suggestion = {
                    "from": current_loc["formatted_address"],
                    "to": next_loc["formatted_address"],
                    "distance": leg.get("distance", {}).get("text", "Unknown"),
                    "duration": leg.get("duration", {}).get("text", "Unknown"),
                    "mode": "driving",
                    "directions_url": self.maps_client.get_directions_url(
                        current_loc["formatted_address"],
                        next_loc["formatted_address"],
                    ),
                }

                suggestions.append(suggestion)

        return suggestions
