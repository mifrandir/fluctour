"""
Command line interface for gmaps-randomizer.
"""

import argparse
from datetime import datetime
from dateutil.parser import parse as parse_date
from typing import List, Tuple


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate random travel itineraries using Google Maps",
        prog="gmaps-randomizer",
    )

    parser.add_argument(
        "--start",
        required=True,
        help="Starting location (city, country, or address)",
    )

    parser.add_argument(
        "--end",
        required=True,
        help="Ending location (city, country, or address)",
    )

    parser.add_argument(
        "--start-date",
        required=True,
        help="Start date (e.g., '3 aug 2025', '2025-08-03')",
    )

    parser.add_argument(
        "--end-date",
        required=True,
        help="End date (e.g., '10 aug 2025', '2025-08-10')",
    )

    parser.add_argument(
        "--locations",
        help="Comma-separated list of countries, continents, or cities to include",
    )

    parser.add_argument(
        "--api-key",
        help="Google Maps API key (can also be set via GOOGLE_MAPS_API_KEY environment variable)",
    )

    parser.add_argument(
        "--max-stops",
        type=int,
        default=5,
        help="Maximum number of intermediate stops (default: 5)",
    )

    parser.add_argument(
        "--min-stay",
        type=int,
        default=1,
        help="Minimum days to stay at each location (default: 1)",
    )

    return parser.parse_args()


def validate_dates(
    start_date_str: str, end_date_str: str
) -> Tuple[datetime, datetime]:
    """Validate and parse date strings."""
    try:
        start_date = parse_date(start_date_str)
        end_date = parse_date(end_date_str)

        if start_date >= end_date:
            raise ValueError("Start date must be before end date")

        if (end_date - start_date).days < 1:
            raise ValueError("Trip must be at least 1 day long")

        return start_date, end_date

    except Exception as e:
        raise ValueError(f"Invalid date format: {e}")


def parse_locations(locations_str: str) -> List[str]:
    """Parse comma-separated locations string."""
    if not locations_str:
        return []

    return [loc.strip() for loc in locations_str.split(",") if loc.strip()]
