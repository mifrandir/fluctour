"""
Google Maps API client wrapper for gmaps-randomizer.
"""

import googlemaps
import os
from typing import Dict, List, Tuple, Optional
import logging


class MapsClient:
    """Wrapper for Google Maps API client."""

    def __init__(self, api_key: Optional[str] = None):
        """Initialize the Maps client."""
        self.api_key = api_key or os.getenv("GOOGLE_MAPS_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Google Maps API key is required. Set GOOGLE_MAPS_API_KEY environment variable or pass --api-key"
            )

        self.client = googlemaps.Client(key=self.api_key)
        self.logger = logging.getLogger(__name__)

    def geocode_location(self, location: str) -> Dict:
        """Geocode a location string to get coordinates and formatted address."""
        try:
            results = self.client.geocode(location)
            if not results:
                raise ValueError(f"Could not find location: {location}")

            result = results[0]
            return {
                "formatted_address": result["formatted_address"],
                "lat": result["geometry"]["location"]["lat"],
                "lng": result["geometry"]["location"]["lng"],
                "place_id": result["place_id"],
                "types": result.get("types", []),
            }
        except Exception as e:
            self.logger.error(f"Error geocoding {location}: {e}")
            raise

    def get_distance_matrix(
        self, origins: List[str], destinations: List[str]
    ) -> Dict:
        """Get distance and duration between multiple origins and destinations."""
        try:
            result = self.client.distance_matrix(
                origins=origins,
                destinations=destinations,
                mode="driving",
                units="metric",
            )
            return result
        except Exception as e:
            self.logger.error(f"Error getting distance matrix: {e}")
            raise

    def get_directions(
        self, origin: str, destination: str, waypoints: List[str] = None
    ) -> Dict:
        """Get directions between two points, optionally with waypoints."""
        try:
            result = self.client.directions(
                origin=origin,
                destination=destination,
                waypoints=waypoints,
                mode="driving",
                optimize_waypoints=True,
            )
            return result[0] if result else {}
        except Exception as e:
            self.logger.error(f"Error getting directions: {e}")
            raise

    def find_places_along_route(
        self,
        origin: Dict,
        destination: Dict,
        place_type: str = "tourist_attraction",
    ) -> List[Dict]:
        """Find interesting places along a route."""
        try:
            # Calculate midpoint
            mid_lat = (origin["lat"] + destination["lat"]) / 2
            mid_lng = (origin["lng"] + destination["lng"]) / 2

            # Search for places near the midpoint
            places = self.client.places_nearby(
                location=(mid_lat, mid_lng),
                radius=50000,  # 50km radius
                type=place_type,
            )

            results = []
            for place in places.get("results", [])[:10]:  # Limit to 10 results
                place_details = {
                    "name": place["name"],
                    "place_id": place["place_id"],
                    "lat": place["geometry"]["location"]["lat"],
                    "lng": place["geometry"]["location"]["lng"],
                    "rating": place.get("rating", 0),
                    "types": place.get("types", []),
                    "vicinity": place.get("vicinity", ""),
                    "formatted_address": place.get("vicinity", place["name"]),
                }
                results.append(place_details)

            return results
        except Exception as e:
            self.logger.error(f"Error finding places along route: {e}")
            return []

    def search_places_in_area(
        self, location: Dict, radius: int = 25000, place_types: List[str] = None
    ) -> List[Dict]:
        """Search for places in a specific area."""
        if place_types is None:
            place_types = [
                "tourist_attraction",
                "museum",
                "park",
                "point_of_interest",
            ]

        all_places = []

        for place_type in place_types:
            try:
                places = self.client.places_nearby(
                    location=(location["lat"], location["lng"]),
                    radius=radius,
                    type=place_type,
                )

                for place in places.get("results", [])[:5]:  # Limit per type
                    place_details = {
                        "name": place["name"],
                        "place_id": place["place_id"],
                        "lat": place["geometry"]["location"]["lat"],
                        "lng": place["geometry"]["location"]["lng"],
                        "rating": place.get("rating", 0),
                        "types": place.get("types", []),
                        "vicinity": place.get("vicinity", ""),
                        "formatted_address": place.get(
                            "vicinity", place["name"]
                        ),
                        "search_type": place_type,
                    }
                    all_places.append(place_details)

            except Exception as e:
                self.logger.warning(f"Error searching for {place_type}: {e}")
                continue

        # Sort by rating and remove duplicates
        unique_places = {}
        for place in all_places:
            if place["place_id"] not in unique_places:
                unique_places[place["place_id"]] = place

        return sorted(
            unique_places.values(), key=lambda x: x["rating"], reverse=True
        )

    def get_google_maps_url(self, location: Dict) -> str:
        """Generate a Google Maps URL for a location."""
        return f"https://www.google.com/maps/place/?q=place_id:{location['place_id']}"

    def get_directions_url(
        self, origin: str, destination: str, waypoints: List[str] = None
    ) -> str:
        """Generate a Google Maps directions URL."""
        base_url = "https://www.google.com/maps/dir/"

        if waypoints:
            all_points = [origin] + waypoints + [destination]
        else:
            all_points = [origin, destination]

        # URL encode the locations
        encoded_points = [point.replace(" ", "+") for point in all_points]
        return base_url + "/".join(encoded_points)
