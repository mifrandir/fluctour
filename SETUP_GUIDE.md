# Google Maps API Setup Guide

The error you're seeing (`REQUEST_DENIED (This API project is not authorized to use this API.)`) means you need to set up a Google Maps API key. Here's how to do it:

## Step 1: Get a Google Maps API Key

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google Maps Platform APIs:

   - Go to "APIs & Services" > "Library"
   - Search for and enable these APIs:
     - **Geocoding API** (required)
     - **Places API** (required)
     - **Directions API** (required)
     - **Distance Matrix API** (required)

4. Create an API key:
   - Go to "APIs & Services" > "Credentials"
   - Click "Create Credentials" > "API Key"
   - Copy the generated API key

## Step 2: Set Up Your API Key

You have three options to provide your API key:

### Option 1: Environment Variable (Recommended)

```bash
export GOOGLE_MAPS_API_KEY="your_actual_api_key_here"
```

Add this to your shell profile to make it permanent:

```bash
echo 'export GOOGLE_MAPS_API_KEY="your_actual_api_key_here"' >> ~/.zshrc
source ~/.zshrc
```

### Option 2: Create a .env file

```bash
echo "GOOGLE_MAPS_API_KEY=your_actual_api_key_here" > .env
```

### Option 3: Command Line Flag

```bash
fluctour --api-key "your_actual_api_key_here" --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"
```

## Step 3: Test Your Setup

Once you have your API key set up, test it:

```bash
fluctour --start "Amsterdam" --end "Copenhagen" --start-date "3 aug 2025" --end-date "10 aug 2025"
```

## Important Notes

- **Billing**: Google Maps APIs require a billing account to be set up, even for the free tier
- **Quotas**: You get $200 free credits per month, which is usually sufficient for personal use
- **Security**: Consider restricting your API key to specific APIs and IP addresses in the Google Cloud Console

## Troubleshooting

- **"REQUEST_DENIED"**: API key not set or invalid
- **"OVER_QUERY_LIMIT"**: You've exceeded your quota
- **"INVALID_REQUEST"**: Check your location names and date formats
- **"ZERO_RESULTS"**: No route found between locations

## Cost Estimate

A typical itinerary generation uses:

- 2-5 Geocoding API calls (~$0.005 per 1000)
- 5-15 Places API calls (~$0.017 per 1000)
- 3-8 Directions API calls (~$0.005 per 1000)

Total cost per itinerary: approximately $0.10-0.50
