"""
Utility functions for fluctour.
"""

import os
import logging
from typing import Optional


def setup_logging(level: str = "INFO") -> None:
    """Set up logging configuration."""
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def get_api_key(provided_key: Optional[str] = None) -> str:
    """Get Google Maps API key from various sources."""
    if provided_key:
        return provided_key

    # Try environment variable
    env_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if env_key:
        return env_key

    # Try .env file
    try:
        from dotenv import load_dotenv

        load_dotenv()
        env_key = os.getenv("GOOGLE_MAPS_API_KEY")
        if env_key:
            return env_key
    except ImportError:
        pass

    raise ValueError(
        "Google Maps API key not found. Please provide it via:\n"
        "1. --api-key command line argument\n"
        "2. GOOGLE_MAPS_API_KEY environment variable\n"
        "3. .env file with GOOGLE_MAPS_API_KEY=your_key"
    )


def format_itinerary_output(itinerary: dict) -> str:
    """Format the itinerary for console output."""
    output = []

    # Header
    output.append("=" * 60)
    output.append("TRAVEL ITINERARY")
    output.append("=" * 60)
    output.append(f"From: {itinerary['start_location']}")
    output.append(f"To: {itinerary['end_location']}")
    output.append(
        f"Duration: {itinerary['start_date']} to {itinerary['end_date']} ({itinerary['total_days']} days)"
    )
    output.append("")

    # Daily schedule
    output.append("SCHEDULE:")
    output.append("-" * 40)

    for i, schedule in enumerate(itinerary["daily_schedule"]):
        location_name = schedule["location"].get(
            "name", schedule["location"]["formatted_address"]
        )

        output.append(
            f"{schedule['start_date']} - {schedule['end_date']}: {location_name}"
        )
        output.append(f"  Days: {schedule['days']}")
        output.append(f"  Google Maps: {schedule['google_maps_url']}")

        if i < len(itinerary["daily_schedule"]) - 1:
            output.append("")

    # Travel suggestions
    if itinerary["travel_suggestions"]:
        output.append("")
        output.append("TRAVEL BETWEEN LOCATIONS:")
        output.append("-" * 40)

        for suggestion in itinerary["travel_suggestions"]:
            output.append(f"From: {suggestion['from']}")
            output.append(f"To: {suggestion['to']}")
            output.append(f"Distance: {suggestion['distance']}")
            output.append(f"Duration: {suggestion['duration']}")
            output.append(f"Directions: {suggestion['directions_url']}")
            output.append("")

    output.append("=" * 60)
    output.append("Have a great trip!")
    output.append("=" * 60)

    return "\n".join(output)


def validate_itinerary_params(args) -> None:
    """Validate itinerary generation parameters."""
    if args.max_stops < 0:
        raise ValueError("max_stops must be non-negative")

    if args.min_stay < 1:
        raise ValueError("min_stay must be at least 1 day")

    if args.max_stops > 10:
        raise ValueError(
            "max_stops should not exceed 10 for reasonable performance"
        )
