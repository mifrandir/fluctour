<!-- # Deployment Guide -->

This guide covers multiple deployment options for the GMaps Randomizer web application.

## Prerequisites

1. **Google Maps API Key**: Required for the application to function

   - Get your API key from [Google Cloud Console](https://console.cloud.google.com/)
   - Enable: Geocoding API, Distance Matrix API, Directions API, Places API

2. **Environment Variables**:
   - `GOOGLE_MAPS_API_KEY`: Your Google Maps API key
   - `PORT`: Port number (optional, defaults to 5000)
   - `FLASK_ENV`: Set to "development" for debug mode

## Deployment Options

### 1. Heroku Deployment

#### One-Click Deploy

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy)

#### Manual Deployment

```bash
# Install Heroku CLI
# Create Heroku app
heroku create your-app-name

# Set environment variables
heroku config:set GOOGLE_MAPS_API_KEY="your-api-key-here"

# Deploy
git push heroku main
```

#### GitHub Actions Auto-Deploy

1. Set repository secrets:

   - `HEROKU_API_KEY`: Your Heroku API key
   - `HEROKU_APP_NAME`: Your Heroku app name
   - `HEROKU_EMAIL`: Your Heroku account email

2. Push to main branch - automatic deployment via `.github/workflows/deploy.yml`

### 2. Railway Deployment

#### One-Click Deploy

[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new/template)

#### Manual Deployment

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login and deploy
railway login
railway init
railway up
```

#### Environment Variables

Set in Railway dashboard:

- `GOOGLE_MAPS_API_KEY`: Your Google Maps API key

### 3. Vercel Deployment

#### One-Click Deploy

[![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone)

#### Manual Deployment

```bash
# Install Vercel CLI
npm install -g vercel

# Deploy
vercel --prod
```

#### Environment Variables

Set in Vercel dashboard:

- `GOOGLE_MAPS_API_KEY`: Your Google Maps API key

### 4. Docker Deployment

#### Build and Run Locally

```bash
# Build the image
docker build -t fluctour .

# Run the container
docker run -p 5000:5000 -e GOOGLE_MAPS_API_KEY="your-api-key" fluctour
```

#### Docker Compose

```yaml
version: "3.8"
services:
  web:
    build: .
    ports:
      - "5000:5000"
    environment:
      - GOOGLE_MAPS_API_KEY=your-api-key-here
```

#### Deploy to Container Platforms

- **Google Cloud Run**: Use the Dockerfile
- **AWS ECS**: Use the Docker image
- **Azure Container Instances**: Use the Docker image

### 5. GitHub Pages (Static Hosting)

For a static version without backend functionality:

1. Create `docs/` folder with static HTML
2. Enable GitHub Pages in repository settings
3. Set source to `docs/` folder

### 6. Self-Hosted VPS

#### Using systemd service

```bash
# Install dependencies
sudo apt update
sudo apt install python3 python3-pip nginx

# Clone repository
git clone https://github.com/yourusername/fluctour.git
cd fluctour

# Install Python dependencies
pip3 install -r requirements.txt

# Create systemd service
sudo nano /etc/systemd/system/fluctour.service
```

Service file content:

```ini
[Unit]
Description=GMaps Randomizer Web App
After=network.target

[Service]
User=www-data
WorkingDirectory=/path/to/fluctour
Environment=GOOGLE_MAPS_API_KEY=your-api-key
ExecStart=/usr/bin/python3 web_app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start service
sudo systemctl enable fluctour
sudo systemctl start fluctour

# Configure nginx reverse proxy
sudo nano /etc/nginx/sites-available/fluctour
```

Nginx configuration:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## Environment Configuration

### Required Environment Variables

- `GOOGLE_MAPS_API_KEY`: Your Google Maps API key (required)

### Optional Environment Variables

- `PORT`: Port number (default: 5000)
- `FLASK_ENV`: Set to "development" for debug mode
- `SECRET_KEY`: Flask secret key (auto-generated if not set)

## Monitoring and Logging

### Health Check Endpoint

The application includes a health check at `/` that returns the main page.

### Logging

Application logs are written to stdout and can be viewed through your platform's logging system:

- Heroku: `heroku logs --tail`
- Railway: Railway dashboard logs
- Docker: `docker logs container-name`

## Security Considerations

1. **API Key Security**: Never commit API keys to version control
2. **HTTPS**: Always use HTTPS in production
3. **Environment Variables**: Use platform-specific secret management
4. **Rate Limiting**: Consider implementing rate limiting for API endpoints

## Troubleshooting

### Common Issues

1. **Google Maps API Error**: Verify API key and enabled services
2. **Port Binding**: Ensure PORT environment variable is set correctly
3. **Dependencies**: Run `pip install -r requirements.txt`
4. **Memory Issues**: Increase memory allocation on your platform

### Debug Mode

Set `FLASK_ENV=development` to enable debug mode with detailed error messages.

## Cost Optimization

1. **Google Maps API**: Monitor usage and set quotas
2. **Platform Resources**: Choose appropriate instance sizes
3. **Caching**: Consider implementing caching for repeated requests

## Scaling

For high-traffic deployments:

1. Use multiple instances/containers
2. Implement load balancing
3. Add Redis for session management
4. Consider CDN for static assets
