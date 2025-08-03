#!/usr/bin/env python3
"""
Flask web application for fluctour.
"""

import os
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
from fluctour.maps_client import MapsClient
from fluctour.itinerary import ItineraryGenerator
from dateutil.parser import parse as parse_date

app = Flask(__name__)

# Configure Flask
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "dev-key-change-in-production"
)


@app.route("/")
def index():
    """Main page with the itinerary form."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    return render_template("index.html", google_maps_api_key=api_key)


@app.route("/api/generate-itinerary", methods=["POST"])
def generate_itinerary_api():
    """API endpoint to generate travel itinerary."""
    try:
        data = request.get_json()

        # Validate required fields
        required_fields = ["start", "end", "start_date", "end_date"]
        for field in required_fields:
            if not data.get(field):
                return jsonify({
                    "error": f"Missing required field: {field}"
                }), 400

        # Parse dates
        try:
            start_date = parse_date(data["start_date"])
            end_date = parse_date(data["end_date"])
        except Exception as e:
            return jsonify({"error": f"Invalid date format: {str(e)}"}), 400

        # Validate date range
        if start_date >= end_date:
            return jsonify({"error": "Start date must be before end date"}), 400

        # Get optional parameters
        locations = data.get("locations", "").strip()
        constraint_locations = (
            [loc.strip() for loc in locations.split(",")] if locations else None
        )
        max_stops = int(data.get("max_stops", 5))
        min_stay = int(data.get("min_stay", 1))

        # Initialize Google Maps client
        api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        if not api_key:
            return jsonify({"error": "Google Maps API key not configured"}), 500

        maps_client = MapsClient(api_key)
        generator = ItineraryGenerator(maps_client)

        # Generate itinerary
        itinerary = generator.generate_itinerary(
            start_location=data["start"],
            end_location=data["end"],
            start_date=start_date,
            end_date=end_date,
            constraint_locations=constraint_locations,
            max_stops=max_stops,
            min_stay=min_stay,
        )

        return jsonify({"success": True, "itinerary": itinerary})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/favicon.ico")
def favicon():
    """Serve favicon."""
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
