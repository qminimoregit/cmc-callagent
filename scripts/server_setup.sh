#!/usr/bin/env bash
set -e

echo "============================================"
echo " CMC Call Agent - Server Setup Script"
echo "============================================"

# ── 1. Install Docker Compose V2 ──────────────────────────────────────────────
echo ""
echo "[1/4] Installing Docker Compose V2..."

sudo apt-get update -qq
sudo apt-get install -y ca-certificates curl gnupg

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -qq
sudo apt-get install -y docker-compose-plugin

echo "[1/4] Docker Compose V2 installed ✅"

# ── 2. Remove old broken containers ───────────────────────────────────────────
echo ""
echo "[2/4] Cleaning up old containers..."
cd ~/cmc-callagent
sudo docker compose down --remove-orphans 2>/dev/null || true
sudo docker system prune -f 2>/dev/null || true
echo "[2/4] Cleanup done ✅"

# ── 3. Stop system Redis (conflicts with Docker Redis) ─────────────────────────
echo ""
echo "[3/4] Freeing port 6379..."
sudo systemctl stop redis-server 2>/dev/null || true
sudo systemctl disable redis-server 2>/dev/null || true
echo "[3/4] Port 6379 freed ✅"

# ── 4. Start all services ─────────────────────────────────────────────────────
echo ""
echo "[4/4] Starting the agent services..."
cd ~/cmc-callagent
sudo docker compose up -d --build db redis app

echo ""
echo "============================================"
echo " Setup Complete! Checking service status..."
echo "============================================"
sudo docker compose ps
echo ""
echo "To watch live logs: sudo docker compose logs -f app"
