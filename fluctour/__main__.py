"""
Main entry point for fluctour CLI application.
"""

import sys
import logging
from .cli import parse_arguments, validate_dates, parse_locations
from .maps_client import MapsClient
from .itinerary import ItineraryGenerator
from .utils import (
    setup_logging,
    get_api_key,
    format_itinerary_output,
    validate_itinerary_params,
)


def main():
    """Main entry point for the application."""
    try:
        # Parse command line arguments
        args = parse_arguments()

        # Set up logging
        setup_logging()
        logger = logging.getLogger(__name__)

        # Validate parameters
        validate_itinerary_params(args)

        # Validate and parse dates
        start_date, end_date = validate_dates(args.start_date, args.end_date)

        # Parse constraint locations
        constraint_locations = (
            parse_locations(args.locations) if args.locations else []
        )

        # Get API key
        api_key = get_api_key(args.api_key)

        # Initialize clients
        logger.info("Initializing Google Maps client...")
        maps_client = MapsClient(api_key)

        logger.info("Generating travel itinerary...")
        itinerary_generator = ItineraryGenerator(maps_client)

        # Generate itinerary
        itinerary = itinerary_generator.generate_itinerary(
            start_location=args.start,
            end_location=args.end,
            start_date=start_date,
            end_date=end_date,
            constraint_locations=constraint_locations,
            max_stops=args.max_stops,
            min_stay=args.min_stay,
        )

        # Format and display output
        output = format_itinerary_output(itinerary)
        print(output)

        logger.info("Itinerary generation completed successfully!")

    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")
        sys.exit(1)

    except Exception as e:
        logger.error(f"Error generating itinerary: {e}")
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
