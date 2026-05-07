#!/bin/bash
# Run this ONCE on the VPS to install and configure coturn.
# Usage: chmod +x setup_turn.sh && ./setup_turn.sh

set -e

# ── Detect VPS public IP ──────────────────────────────
VPS_IP=$(curl -s https://api.ipify.org 2>/dev/null || curl -s http://ifconfig.me 2>/dev/null || echo "")
if [ -z "$VPS_IP" ]; then
    echo "Could not detect public IP. Enter it manually:"
    read -r VPS_IP
fi
echo "VPS IP: $VPS_IP"

# ── Generate TURN secret ─────────────────────────────
TURN_SECRET=$(openssl rand -hex 32)
echo "Generated TURN secret."

# ── Install coturn ────────────────────────────────────
echo "Installing coturn..."
apt-get update -q
apt-get install -y coturn

# Enable the daemon
if [ -f /etc/default/coturn ]; then
    sed -i 's/#TURNSERVER_ENABLED=1/TURNSERVER_ENABLED=1/' /etc/default/coturn
    grep -q 'TURNSERVER_ENABLED=1' /etc/default/coturn || echo 'TURNSERVER_ENABLED=1' >> /etc/default/coturn
fi

# ── Write coturn config ───────────────────────────────
cat > /etc/turnserver.conf << EOF
listening-port=3478
listening-ip=0.0.0.0
external-ip=$VPS_IP
relay-ip=$VPS_IP
realm=telertc
use-auth-secret
static-auth-secret=$TURN_SECRET
fingerprint
no-multicast-peers
no-loopback-peers
min-port=49152
max-port=49300
log-file=/var/log/turnserver.log
simple-log
EOF

echo "coturn config written to /etc/turnserver.conf"

# ── Open firewall ports ───────────────────────────────
if command -v ufw &>/dev/null; then
    ufw allow 3478/tcp
    ufw allow 3478/udp
    ufw allow 49152:49300/udp
    echo "UFW rules added."
elif command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port=3478/tcp
    firewall-cmd --permanent --add-port=3478/udp
    firewall-cmd --permanent --add-port=49152-49300/udp
    firewall-cmd --reload
    echo "firewalld rules added."
fi

# ── Start coturn ──────────────────────────────────────
systemctl enable coturn
systemctl restart coturn
sleep 1
systemctl status coturn --no-pager | head -5

# ── Print environment variables ───────────────────────
echo ""
echo "══════════════════════════════════════════════════"
echo " coturn is running. Set these env vars before"
echo " starting the TeleRTC server:"
echo ""
echo "   export TURN_HOST=$VPS_IP"
echo "   export TURN_SECRET=$TURN_SECRET"
echo ""
echo " Or add them to /etc/environment for persistence:"
echo ""
echo "   echo 'TURN_HOST=$VPS_IP' >> /etc/environment"
echo "   echo 'TURN_SECRET=$TURN_SECRET' >> /etc/environment"
echo "   source /etc/environment"
echo "══════════════════════════════════════════════════"
