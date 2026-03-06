# Automatic Redeployment Setup

This project automatically builds and deploys Docker images on every commit to the main branch.

## How It Works

1. **GitHub Actions** builds the Docker image and pushes it to GitHub Container Registry (ghcr.io)
2. **SSH Deployment** automatically redeploys on your server
3. **Docker Compose** pulls the latest image and restarts the container

## Setup Instructions

### 1. Configure GitHub Secrets

Go to your repository Settings → Secrets and variables → Actions, and add these secrets:

- `SSH_HOST`: Your server's IP address (e.g., `172.20.204.26`)
- `SSH_USER`: Your SSH username (e.g., `root`)
- `SSH_PRIVATE_KEY`: Your SSH private key (generate with `ssh-keygen -t ed25519 -a 200 -C "github-actions"`)

### 2. Set up SSH Key on Your Server

On your server, add the public key to `~/.ssh/authorized_keys`:

```bash
# Copy the public key content and add it to authorized_keys
echo "ssh-ed25519 AAAAC3... github-actions" >> ~/.ssh/authorized_keys
```

### 3. Ensure Docker Compose File Exists on Server

Your server should have the repository cloned with the docker-compose.yml:

```bash
cd ~
git clone https://github.com/kobius77/fronius2vim.git
```

### 4. Test the Connection

The GitHub Action will automatically run on your next commit. You can also trigger it manually from the Actions tab.

## Alternative: Watchtower (No SSH Required)

If you prefer not to use SSH from GitHub Actions, you can use [Watchtower](https://containrrr.dev/watchtower/) on your server:

```yaml
# Add to your docker-compose.yml on the server
  watchtower:
    image: containrrr/watchtower
    container_name: watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - WATCHTOWER_POLL_INTERVAL=60
      - WATCHTOWER_CLEANUP=true
      - REPO_USER=${GITHUB_USERNAME}
      - REPO_PASS=${GITHUB_TOKEN}
    command: --interval 60 fronius2vim
```

This requires a GitHub Personal Access Token with `read:packages` scope.

## Manual Deployment (Fallback)

If automatic deployment fails, you can always deploy manually:

```bash
cd ~/fronius2vim
git pull
docker compose pull
docker compose up -d
```
