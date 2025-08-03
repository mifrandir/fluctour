import pytest
import os
from datetime import datetime
from gmaps_randomizer.maps_client import MapsClient
from gmaps_randomizer.itinerary import ItineraryGenerator


class TestIntegration:
    """Integration tests that require a real Google Maps API key."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test fixtures."""
        api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        if not api_key:
            pytest.skip(
                "GOOGLE_MAPS_API_KEY not set - skipping integration tests"
            )

        self.maps_client = MapsClient(api_key)
        self.generator = ItineraryGenerator(self.maps_client)

    def test_basic_itinerary_generation(self):
        """Test basic itinerary generation with real API."""
        start_date = datetime(2025, 8, 3)
        end_date = datetime(2025, 8, 10)

        itinerary = self.generator.generate_itinerary(
            start_location="Amsterdam",
            end_location="Copenhagen",
            start_date=start_date,
            end_date=end_date,
            constraint_locations=None,
            max_stops=3,
            min_stay=1,
        )

        # Verify basic structure
        assert itinerary["start_location"] == "Amsterdam"
        assert itinerary["end_location"] == "Copenhagen"
        assert itinerary["total_days"] == 7
        assert len(itinerary["daily_schedule"]) >= 2  # At least start and end
        assert len(itinerary["locations"]) >= 2

        # Verify dates
        assert itinerary["start_date"] == "2025-08-03"
        assert itinerary["end_date"] == "2025-08-10"

    def test_geocoding_accuracy(self):
        """Test that geocoding returns reasonable results."""
        # Test major cities
        amsterdam = self.maps_client.geocode_location("Amsterdam")
        assert "Amsterdam" in amsterdam["formatted_address"]
        assert 52.0 < amsterdam["lat"] < 53.0  # Approximate latitude
        assert 4.0 < amsterdam["lng"] < 5.0  # Approximate longitude

        copenhagen = self.maps_client.geocode_location("Copenhagen")
        assert (
            "Copenhagen" in copenhagen["formatted_address"]
            or "København" in copenhagen["formatted_address"]
        )
        assert 55.0 < copenhagen["lat"] < 56.0
        assert 12.0 < copenhagen["lng"] < 13.0

    def test_distance_calculation(self):
        """Test distance calculation between known cities."""
        amsterdam = self.maps_client.geocode_location("Amsterdam")
        copenhagen = self.maps_client.geocode_location("Copenhagen")

        # Get distance matrix
        distance_result = self.maps_client.get_distance_matrix(
            [f"{amsterdam['lat']},{amsterdam['lng']}"],
            [f"{copenhagen['lat']},{copenhagen['lng']}"],
        )

        assert distance_result["status"] == "OK"
        assert len(distance_result["rows"]) == 1
        assert len(distance_result["rows"][0]["elements"]) == 1

        element = distance_result["rows"][0]["elements"][0]
        assert element["status"] == "OK"

        # Distance should be reasonable (Amsterdam to Copenhagen is ~600-700km)
        distance_text = element["distance"]["text"]
        assert "km" in distance_text

    def test_places_search(self):
        """Test finding interesting places along a route."""
        amsterdam = self.maps_client.geocode_location("Amsterdam")
        copenhagen = self.maps_client.geocode_location("Copenhagen")

        # Find places along the route
        places = self.maps_client.find_places_along_route(
            amsterdam, copenhagen, "tourist_attraction"
        )

        assert isinstance(places, list)
        # Should find at least some places
        if places:  # Only test if places were found
            place = places[0]
            assert "name" in place
            assert "lat" in place
            assert "lng" in place
            assert "place_id" in place

    def test_itinerary_with_constraints(self):
        """Test itinerary generation with location constraints."""
        start_date = datetime(2025, 8, 3)
        end_date = datetime(2025, 8, 10)

        itinerary = self.generator.generate_itinerary(
            start_location="Amsterdam",
            end_location="Copenhagen",
            start_date=start_date,
            end_date=end_date,
            constraint_locations=["Netherlands", "Germany", "Denmark"],
            max_stops=4,
            min_stay=1,
        )

        # Should have reasonable number of stops
        assert len(itinerary["locations"]) >= 2
        assert len(itinerary["daily_schedule"]) >= 2

        # Should have travel suggestions
        assert len(itinerary["travel_suggestions"]) >= 1

        # Each travel suggestion should have required fields
        for suggestion in itinerary["travel_suggestions"]:
            assert "from" in suggestion
            assert "to" in suggestion
            assert "distance" in suggestion
            assert "duration" in suggestion
            assert "directions_url" in suggestion

    def test_google_maps_urls(self):
        """Test that Google Maps URLs are generated correctly."""
        amsterdam = self.maps_client.geocode_location("Amsterdam")

        # Test place URL
        place_url = self.maps_client.get_google_maps_url(amsterdam)
        assert place_url.startswith("https://www.google.com/maps/place/")
        assert amsterdam["place_id"] in place_url

        # Test directions URL
        directions_url = self.maps_client.get_directions_url(
            "Amsterdam", "Copenhagen"
        )
        assert directions_url.startswith("https://www.google.com/maps/dir/")
        assert "Amsterdam" in directions_url
        assert "Copenhagen" in directions_url
