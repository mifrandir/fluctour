# gmaps-randomizer

A Python application to randomly create travel itineraries using Google Maps.

## Features

- Generate travel itineraries between two locations
- Specify date ranges for your trip
- Include constraint locations (countries, cities, or regions)
- Find interesting stops along your route
- Get Google Maps links for all locations
- Receive travel suggestions between stops

## Installation

1. Clone this repository:

```bash
git clone <repository-url>
cd gmaps-randomizer
```

2. Install the package:

```bash
pip install -e .
```

Or install dependencies directly:

```bash
pip install -r requirements.txt
```

3. **Important**: Add Python user bin directory to your PATH (for macOS/Linux):

```bash
# For zsh (default on macOS)
echo 'export PATH="$(python3 -c "import site; print(site.USER_BASE)")/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc

# For bash
echo 'export PATH="$(python3 -c "import site; print(site.USER_BASE)")/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

This ensures the `gmaps-randomizer` command is available in your terminal.

## Setup

### Google Maps API Key

You need a Google Maps API key to use this application. Get one from:
https://developers.google.com/maps/documentation/javascript/get-api-key

Make sure to enable the following APIs:

- Maps JavaScript API
- Places API
- Directions API
- Geocoding API

### Set your API key

You can provide your API key in several ways:

1. **Command line argument:**

```bash
gmaps-randomizer --api-key YOUR_API_KEY --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"
```

2. **Environment variable:**

```bash
export GOOGLE_MAPS_API_KEY=your_api_key_here
gmaps-randomizer --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"
```

3. **Create a .env file:**

```bash
echo "GOOGLE_MAPS_API_KEY=your_api_key_here" > .env
```

## Usage

### Basic Usage

```bash
gmaps-randomizer --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"
```

### Advanced Usage

```bash
gmaps-randomizer \
  --start "Amsterdam" \
  --end "Copenhagen" \
  --start-date "3 aug 2025" \
  --end-date "10 aug 2025" \
  --locations "Germany,Netherlands" \
  --max-stops 3 \
  --min-stay 2
```

### Command Line Options

- `--start`: Starting location (required)
- `--end`: Ending location (required)
- `--start-date`: Start date in various formats (required)
- `--end-date`: End date in various formats (required)
- `--locations`: Comma-separated list of constraint locations (optional)
- `--api-key`: Google Maps API key (optional if set via environment)
- `--max-stops`: Maximum number of intermediate stops (default: 5)
- `--min-stay`: Minimum days to stay at each location (default: 1)

### Date Formats

The application accepts various date formats:

- "3 aug 2025"
- "2025-08-03"
- "August 3, 2025"
- "03/08/2025"

## Example Output

```
============================================================
TRAVEL ITINERARY
============================================================
From: Amsterdam
To: Copenhagen
Duration: 2025-08-03 to 2025-08-10 (7 days)

SCHEDULE:
----------------------------------------
Aug 03 - Aug 04: Amsterdam, Netherlands
  Days: 1
  Google Maps: https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 04 - Aug 05: Groningen, Netherlands
  Days: 1
  Google Maps: https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 05 - Aug 06: Bremerhaven, Germany
  Days: 1
  Google Maps: https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 07 - Aug 08: Hamburg, Germany
  Days: 1
  Google Maps: https://www.google.com/maps/place/?q=place_id:ChIJ...

Aug 08 - Aug 10: Copenhagen, Denmark
  Days: 2
  Google Maps: https://www.google.com/maps/place/?q=place_id:ChIJ...

TRAVEL BETWEEN LOCATIONS:
----------------------------------------
From: Amsterdam, Netherlands
To: Groningen, Netherlands
Distance: 185 km
Duration: 2 hours 5 mins
Directions: https://www.google.com/maps/dir/Amsterdam/Groningen

...

============================================================
Have a great trip!
============================================================
```

## Development

### Project Structure

```
gmaps-randomizer/
├── gmaps_randomizer/
│   ├── __init__.py
│   ├── __main__.py          # Main entry point
│   ├── cli.py               # Command line interface
│   ├── maps_client.py       # Google Maps API wrapper
│   ├── itinerary.py         # Core itinerary generation
│   └── utils.py             # Utility functions
├── requirements.txt
├── setup.py
└── README.md
```

### Running from Source

```bash
python -m gmaps_randomizer --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"
```

## Troubleshooting

### Common Issues

1. **API Key Issues:**

   - Make sure your API key is valid
   - Ensure required APIs are enabled
   - Check API quotas and billing

2. **Location Not Found:**

   - Try more specific location names
   - Use full city names with country
   - Check spelling

3. **No Route Found:**
   - Ensure locations are accessible by car
   - Try different constraint locations
   - Reduce max-stops parameter

### Error Messages

- `Google Maps API key is required`: Set your API key via environment variable or command line
- `Could not find location`: The location string couldn't be geocoded
- `Trip duration is less than minimum stay`: Increase trip duration or reduce min-stay

## License

MIT License - see LICENSE file for details.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## API Costs

This application uses several Google Maps APIs which may incur costs:

- Geocoding API: ~$5 per 1000 requests
- Places API: ~$17 per 1000 requests
- Directions API: ~$5 per 1000 requests

A typical itinerary generation uses 10-20 API calls, costing approximately $0.10-0.50 per itinerary.
