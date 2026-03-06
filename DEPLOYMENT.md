# Automatic Redeployment with Watchtower

This project uses **Watchtower** to automatically redeploy when new Docker images are pushed to GitHub Container Registry.

## How It Works

1. **GitHub Actions** builds the Docker image on every commit to `main`
2. **GitHub Container Registry (ghcr.io)** stores the built image
3. **Watchtower** (running on your server) polls every 60 seconds for new images
4. When a new image is detected, Watchtower automatically pulls and restarts the container

## Setup Instructions

### 1. Create GitHub Personal Access Token

1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Give it a name like "Watchtower Docker Pull"
4. Select scope: **`read:packages`**
5. Click "Generate token"
6. **Copy the token immediately** (you won't see it again!)

### 2. Configure Environment Variables on Your Server

On your remote server:

```bash
cd ~/fronius2vim

# Create .env file
cp .env.example .env

# Edit with your credentials
nano .env
```

Fill in:
```env
GITHUB_USERNAME=your-github-username
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

### 3. Start the Services

```bash
# Pull and start both fronius2vim and watchtower
docker compose up -d

# Verify both containers are running
docker compose ps

# Check watchtower logs to confirm it's monitoring
docker logs -f watchtower
```

You should see output like:
```
time="..." level=info msg="Found new ghcr.io/kobius77/fronius2vim:latest image"
time="..." level=info msg"Stopping /fronius2vim (xxxxxxxx) with SIGTERM"
time="..." level=info msg"Creating /fronius2vim"
```

### 4. Test Automatic Deployment

Make any commit to the `main` branch and push to GitHub. Within 1-2 minutes, Watchtower should detect the new image and redeploy automatically.

## How to Monitor

```bash
# Watch watchtower logs in real-time
docker logs -f watchtower

# Check if new image was pulled
docker images | grep fronius2vim

# See container status
docker compose ps
```

## Troubleshooting

**Problem**: Watchtower can't pull the image (authentication error)
**Solution**: Check your `.env` file has correct GITHUB_USERNAME and GITHUB_TOKEN

**Problem**: No new deployments on commit
**Solution**: 
1. Check GitHub Actions completed successfully: https://github.com/kobius77/fronius2vim/actions
2. Verify the image was pushed: https://github.com/kobius77/fronius2vim/pkgs/container/fronius2vim
3. Check watchtower logs: `docker logs watchtower`

**Problem**: Watchtower not detecting updates
**Solution**: Check that the container has the label:
```bash
docker inspect fronius2vim | grep -A 5 Labels
```
Should show: `"com.centurylinklabs.watchtower.enable": "true"`

## Security Notes

- The GitHub token only needs `read:packages` scope (cannot push code or access private repos)
- Token is stored only on your server in the `.env` file (never committed to git)
- Watchtower only has access to Docker socket, not your full system
- No inbound SSH access required from GitHub

## Manual Deployment (Fallback)

If Watchtower fails, deploy manually:

```bash
cd ~/fronius2vim
docker compose pull
docker compose up -d
```
