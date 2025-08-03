import pytest
from fluctour.utils import (
    get_api_key,
    format_itinerary_output,
    validate_itinerary_params,
)
from unittest.mock import patch, MagicMock
import os


def test_get_api_key_with_provided_key():
    """Test getting API key when provided directly."""
    api_key = "test_api_key_123"
    result = get_api_key(api_key)
    assert result == api_key


@patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "env_api_key"})
def test_get_api_key_from_environment():
    """Test getting API key from environment variable."""
    result = get_api_key()
    assert result == "env_api_key"


def test_get_api_key_missing():
    """Test error when API key is missing."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError) as exc_info:
            get_api_key()
        assert "Google Maps API key not found" in str(exc_info.value)


def test_format_itinerary_output():
    """Test itinerary formatting."""
    mock_itinerary = {
        "start_location": "Amsterdam",
        "end_location": "Copenhagen",
        "start_date": "2025-08-03",
        "end_date": "2025-08-10",
        "total_days": 7,
        "daily_schedule": [
            {
                "start_date": "Aug 03",
                "end_date": "Aug 04",
                "location": {
                    "name": "Amsterdam",
                    "formatted_address": "Amsterdam, Netherlands",
                },
                "days": 1,
                "google_maps_url": "https://maps.google.com/?q=Amsterdam",
            }
        ],
        "travel_suggestions": [
            {
                "from": "Amsterdam",
                "to": "Copenhagen",
                "distance": "461 km",
                "duration": "4 hours 30 mins",
                "directions_url": "https://maps.google.com/dir/Amsterdam/Copenhagen",
            }
        ],
    }

    formatted = format_itinerary_output(mock_itinerary)
    assert "Amsterdam" in formatted
    assert "Copenhagen" in formatted
    assert "TRAVEL ITINERARY" in formatted
    assert "https://maps.google.com" in formatted


def test_validate_itinerary_params():
    """Test parameter validation."""
    # Mock args object
    valid_args = MagicMock()
    valid_args.max_stops = 5
    valid_args.min_stay = 2

    # Should not raise any exception
    validate_itinerary_params(valid_args)

    # Test invalid max_stops
    invalid_args = MagicMock()
    invalid_args.max_stops = -1
    invalid_args.min_stay = 1

    with pytest.raises(ValueError) as exc_info:
        validate_itinerary_params(invalid_args)
    assert "max_stops must be non-negative" in str(exc_info.value)

    # Test invalid min_stay
    invalid_args2 = MagicMock()
    invalid_args2.max_stops = 5
    invalid_args2.min_stay = 0

    with pytest.raises(ValueError) as exc_info:
        validate_itinerary_params(invalid_args2)
    assert "min_stay must be at least 1 day" in str(exc_info.value)
