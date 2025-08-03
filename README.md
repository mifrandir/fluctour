# Fluctour

A Python application that generates random travel itineraries using Google Maps API. Create efficient travel routes with interesting stops between your start and end destinations.

## Features

- **Command Line Interface**: Easy-to-use CLI with comprehensive options
- **Web Interface**: Beautiful, responsive web UI for generating itineraries
- **Google Maps Integration**: Real location data, distances, and directions
- **Smart Route Planning**: Finds efficient paths with evenly spaced interesting stops
- **Flexible Constraints**: Specify countries, continents, or cities to include
- **Customizable Parameters**: Control max stops, minimum stay duration, and more
- **Google Maps Links**: Direct links to locations and directions
- **GitHub CI/CD**: Automated testing and regression detection

## Installation

### Prerequisites

- Python 3.9 or higher
- Google Maps API key

### Setup

1. **Clone the repository:**

   ```bash
   git clone <repository-url>
   cd fluctour
   ```

2. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

3. **Set up Google Maps API key:**

   ```bash
   # Option 1: Environment variable
   export GOOGLE_MAPS_API_KEY="your-api-key-here"

   # Option 2: Create .env file
   cp .env.example .env
   # Edit .env and add your API key
   ```

4. **Install the package:**
   ```bash
   pip install -e .
   ```

## Usage

### Command Line Interface

Generate a travel itinerary from Amsterdam to Copenhagen:

```bash
fluctour \
  --start "Amsterdam" \
  --end "Copenhagen" \
  --start-date "3 aug 2025" \
  --end-date "10 aug 2025" \
  --locations "Netherlands,Germany,Denmark"
```

#### CLI Options

- `--start`: Starting location (required)
- `--end`: Ending location (required)
- `--start-date`: Start date in flexible format (required)
- `--end-date`: End date in flexible format (required)
- `--locations`: Comma-separated list of constraint locations (optional)
- `--api-key`: Google Maps API key (optional if set in environment)
- `--max-stops`: Maximum intermediate stops (default: 5)
- `--min-stay`: Minimum days per location (default: 1)

### Web Interface

1. **Start the web server:**

   ```bash
   python web_app.py
   ```

2. **Open your browser:**
   Navigate to `http://localhost:5000`

3. **Fill out the form:**
   - Enter start and end locations
   - Specify travel dates
   - Optionally add location constraints
   - Adjust max stops and minimum stay
   - Click "Generate Itinerary"

### Python API

```python
from fluctour.maps_client import MapsClient
from fluctour.itinerary import ItineraryGenerator
from datetime import datetime

# Initialize
client = MapsClient("your-api-key")
generator = ItineraryGenerator(client)

# Generate itinerary
itinerary = generator.generate_itinerary(
    start_location="Amsterdam",
    end_location="Copenhagen",
    start_date=datetime(2025, 8, 3),
    end_date=datetime(2025, 8, 10),
    constraint_locations=["Netherlands", "Germany", "Denmark"],
    max_stops=5,
    min_stay=1
)

print(itinerary)
```

## Example Output

```
Travel Itinerary: Amsterdam → Copenhagen
Duration: 7 days (2025-08-03 to 2025-08-10)

Daily Schedule:
Aug 03 - Aug 04: Amsterdam, Netherlands
  📍 https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 04 - Aug 05: Groningen, Netherlands
  📍 https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 05 - Aug 07: Hamburg, Germany
  📍 https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 07 - Aug 10: Copenhagen, Denmark
  📍 https://www.google.com/maps/place/?q=place_id:ChIJ...

Travel Suggestions:
🚗 Amsterdam → Groningen: 185 km, 2h 15min
   https://www.google.com/maps/dir/Amsterdam/Groningen

🚗 Groningen → Hamburg: 285 km, 3h 5min
   https://www.google.com/maps/dir/Groningen/Hamburg

🚗 Hamburg → Copenhagen: 350 km, 4h 20min
   https://www.google.com/maps/dir/Hamburg/Copenhagen
```

## Development

### Project Structure

```
fluctour/
├── fluctour/          # Main package
│   ├── __init__.py
│   ├── __main__.py           # CLI entry point
│   ├── cli.py                # Command line interface
│   ├── maps_client.py        # Google Maps API wrapper
│   ├── itinerary.py          # Core itinerary generation
│   └── utils.py              # Utility functions
├── tests/                    # Test suite
│   ├── test_cli.py
│   ├── test_utils.py
│   └── test_integration.py
├── templates/                # Web UI templates
│   └── index.html
├── .github/workflows/        # CI/CD configuration
│   └── ci.yml
├── web_app.py               # Flask web application
├── requirements.txt         # Dependencies
├── setup.py                # Package configuration
└── README.md               # This file
```

### Running Tests

```bash
# Run all tests
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=fluctour

# Run only unit tests (skip integration tests that require API key)
python -m pytest tests/test_cli.py tests/test_utils.py -v
```

### Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Run the test suite
6. Submit a pull request

## API Requirements

This application requires a Google Maps API key with the following APIs enabled:

- **Geocoding API**: For converting location names to coordinates
- **Distance Matrix API**: For calculating distances and travel times
- **Directions API**: For route planning and optimization
- **Places API**: For finding interesting stops along routes

### Getting an API Key

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the required APIs listed above
4. Create credentials (API key)
5. Optionally restrict the API key to specific APIs and domains

## Deployment

### GitHub Pages (Web UI)

The web interface can be easily deployed to GitHub Pages:

1. Push your code to GitHub
2. Enable GitHub Pages in repository settings
3. Set up environment variables for the API key
4. The CI/CD pipeline will automatically deploy updates

### Heroku

```bash
# Install Heroku CLI and login
heroku create your-app-name
heroku config:set GOOGLE_MAPS_API_KEY="your-api-key"
git push heroku main
```

### Docker

```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
EXPOSE 5000
CMD ["python", "web_app.py"]
```

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Google Maps API for location data and routing
- Flask for the web framework
- Click for the command line interface
- All contributors and testers

## Support

If you encounter any issues or have questions:

1. Check the [Issues](../../issues) page
2. Review the [Setup Guide](SETUP_GUIDE.md)
3. Create a new issue with detailed information

---

**Happy travels! 🗺️✈️**
