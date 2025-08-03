#!/usr/bin/env python3
"""
Test script to demonstrate gmaps-randomizer functionality.
This script shows how the application would work with a valid API key.
"""

import sys
import os
from datetime import datetime
from gmaps_randomizer.cli import validate_dates, parse_locations
from gmaps_randomizer.utils import format_itinerary_output


def create_mock_itinerary():
    """Create a mock itinerary for demonstration purposes."""
    return {
        "start_location": "Amsterdam",
        "end_location": "Copenhagen",
        "start_date": "2025-08-03",
        "end_date": "2025-08-10",
        "total_days": 7,
        "locations": [
            {
                "formatted_address": "Amsterdam, Netherlands",
                "name": "Amsterdam",
                "place_id": "ChIJVXealLU_xkcRja_At0z9AGY",
            },
            {
                "formatted_address": "Groningen, Netherlands",
                "name": "Groningen",
                "place_id": "ChIJf8lD5jKTxUcRLBcNEuBwCGo",
            },
            {
                "formatted_address": "Bremerhaven, Germany",
                "name": "Bremerhaven",
                "place_id": "ChIJAQAAAFOHsUcRYKlDgjKLtgQ",
            },
            {
                "formatted_address": "Hamburg, Germany",
                "name": "Hamburg",
                "place_id": "ChIJuRMYfoNhsUcRoDrWe_I9JgQ",
            },
            {
                "formatted_address": "Copenhagen, Denmark",
                "name": "Copenhagen",
                "place_id": "ChIJIz2AXDxTUkYRuGeU5t1-3QQ",
            },
        ],
        "daily_schedule": [
            {
                "location": {
                    "formatted_address": "Amsterdam, Netherlands",
                    "name": "Amsterdam",
                },
                "start_date": "Aug 03",
                "end_date": "Aug 04",
                "days": 1,
                "google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJVXealLU_xkcRja_At0z9AGY",
            },
            {
                "location": {
                    "formatted_address": "Groningen, Netherlands",
                    "name": "Groningen",
                },
                "start_date": "Aug 04",
                "end_date": "Aug 05",
                "days": 1,
                "google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJf8lD5jKTxUcRLBcNEuBwCGo",
            },
            {
                "location": {
                    "formatted_address": "Bremerhaven, Germany",
                    "name": "Bremerhaven",
                },
                "start_date": "Aug 05",
                "end_date": "Aug 06",
                "days": 1,
                "google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJAQAAAFOHsUcRYKlDgjKLtgQ",
            },
            {
                "location": {
                    "formatted_address": "Hamburg, Germany",
                    "name": "Hamburg",
                },
                "start_date": "Aug 06",
                "end_date": "Aug 08",
                "days": 2,
                "google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJuRMYfoNhsUcRoDrWe_I9JgQ",
            },
            {
                "location": {
                    "formatted_address": "Copenhagen, Denmark",
                    "name": "Copenhagen",
                },
                "start_date": "Aug 08",
                "end_date": "Aug 10",
                "days": 2,
                "google_maps_url": "https://www.google.com/maps/place/?q=place_id:ChIJIz2AXDxTUkYRuGeU5t1-3QQ",
            },
        ],
        "travel_suggestions": [
            {
                "from": "Amsterdam, Netherlands",
                "to": "Groningen, Netherlands",
                "distance": "185 km",
                "duration": "2 hours 5 mins",
                "mode": "driving",
                "directions_url": "https://www.google.com/maps/dir/Amsterdam,+Netherlands/Groningen,+Netherlands",
            },
            {
                "from": "Groningen, Netherlands",
                "to": "Bremerhaven, Germany",
                "distance": "142 km",
                "duration": "1 hour 38 mins",
                "mode": "driving",
                "directions_url": "https://www.google.com/maps/dir/Groningen,+Netherlands/Bremerhaven,+Germany",
            },
            {
                "from": "Bremerhaven, Germany",
                "to": "Hamburg, Germany",
                "distance": "118 km",
                "duration": "1 hour 15 mins",
                "mode": "driving",
                "directions_url": "https://www.google.com/maps/dir/Bremerhaven,+Germany/Hamburg,+Germany",
            },
            {
                "from": "Hamburg, Germany",
                "to": "Copenhagen, Denmark",
                "distance": "289 km",
                "duration": "3 hours 2 mins",
                "mode": "driving",
                "directions_url": "https://www.google.com/maps/dir/Hamburg,+Germany/Copenhagen,+Denmark",
            },
        ],
    }


def test_cli_functions():
    """Test CLI parsing functions."""
    print("Testing CLI functions...")

    # Test date validation
    try:
        start_date, end_date = validate_dates("3 aug 2025", "10 aug 2025")
        print(f"✓ Date parsing successful: {start_date} to {end_date}")
    except Exception as e:
        print(f"✗ Date parsing failed: {e}")
        return False

    # Test location parsing
    locations = parse_locations("Germany,Netherlands,Denmark")
    print(f"✓ Location parsing successful: {locations}")

    return True


def main():
    """Main test function."""
    print("=" * 60)
    print("GMAPS-RANDOMIZER TEST DEMONSTRATION")
    print("=" * 60)
    print()

    # Test CLI functions
    if not test_cli_functions():
        sys.exit(1)

    print()
    print("Creating mock itinerary...")

    # Create and display mock itinerary
    mock_itinerary = create_mock_itinerary()
    formatted_output = format_itinerary_output(mock_itinerary)

    print()
    print("MOCK ITINERARY OUTPUT:")
    print(formatted_output)

    print()
    print("=" * 60)
    print("TEST COMPLETED SUCCESSFULLY!")
    print("=" * 60)
    print()
    print("To use with real data, you need a Google Maps API key.")
    print("Set GOOGLE_MAPS_API_KEY environment variable and run:")
    print(
        'python3 -m gmaps_randomizer --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"'
    )


if __name__ == "__main__":
    main()
