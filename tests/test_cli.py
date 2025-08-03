import pytest
from datetime import datetime
from fluctour.cli import validate_dates, parse_locations


def test_parse_locations():
    """Test location parsing functionality."""
    # Test valid locations
    locations_str = "Netherlands, Germany, Denmark"
    result = parse_locations(locations_str)
    assert result == ["Netherlands", "Germany", "Denmark"]

    # Test empty string
    assert parse_locations("") == []

    # Test with extra spaces
    locations_str = " Netherlands , Germany , Denmark "
    result = parse_locations(locations_str)
    assert result == ["Netherlands", "Germany", "Denmark"]


def test_validate_dates():
    """Test date validation functionality."""
    # Test valid dates
    start_date, end_date = validate_dates("2025-08-03", "2025-08-10")
    assert isinstance(start_date, datetime)
    assert isinstance(end_date, datetime)
    assert start_date < end_date

    # Test various date formats
    start_date, end_date = validate_dates("3 Aug 2025", "10 Aug 2025")
    assert start_date.year == 2025
    assert start_date.month == 8
    assert start_date.day == 3

    # Test invalid date order
    with pytest.raises(ValueError) as exc_info:
        validate_dates("2025-08-10", "2025-08-03")
    assert "Start date must be before end date" in str(exc_info.value)

    # Test same dates
    with pytest.raises(ValueError) as exc_info:
        validate_dates("2025-08-03", "2025-08-03")
    assert "Start date must be before end date" in str(exc_info.value)
