#!/usr/bin/env python3
"""
Simple script to test if your Google Maps API key is working correctly.
Run this before using the full fluctour application.
"""

import os
import sys
from fluctour.maps_client import MapsClient


def test_api_key():
    """Test if the Google Maps API key is working."""

    # Try to get API key from environment or prompt user
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")

    if not api_key:
        print("No GOOGLE_MAPS_API_KEY environment variable found.")
        api_key = input("Please enter your Google Maps API key: ").strip()

        if not api_key:
            print("❌ No API key provided. Exiting.")
            return False

    print(f"🔑 Testing API key: {api_key[:10]}...")

    try:
        # Initialize the client
        client = MapsClient(api_key)

        # Test geocoding with a simple location
        print("🌍 Testing geocoding...")
        location = client.geocode_location("Amsterdam")

        if location:
            print(f"✅ Geocoding successful!")
            print(f"   Location: {location['formatted_address']}")
            print(f"   Coordinates: {location['lat']}, {location['lng']}")

            # Test places search
            print("🔍 Testing places search...")
            places = client.find_places_along_route(location, location)

            if places:
                print(
                    f"✅ Places search successful! Found {len(places)} places."
                )
                print("🎉 Your API key is working correctly!")
                return True
            else:
                print(
                    "⚠️  Places search returned no results, but geocoding works."
                )
                return True

        else:
            print("❌ Geocoding failed. Check your API key and enabled APIs.")
            return False

    except Exception as e:
        print(f"❌ API test failed: {str(e)}")
        print("\nCommon issues:")
        print("- API key is invalid")
        print("- Required APIs are not enabled (Geocoding, Places, Directions)")
        print("- Billing is not set up in Google Cloud Console")
        print("- API key restrictions are too strict")
        return False


if __name__ == "__main__":
    print("🧪 Google Maps API Key Test")
    print("=" * 40)

    success = test_api_key()

    if success:
        print("\n✅ API key test passed! You can now use fluctour.")
        print("\nTry running:")
        print(
            'fluctour --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"'
        )
    else:
        print(
            "\n❌ API key test failed. Please check the SETUP_GUIDE.md for instructions."
        )
        sys.exit(1)
