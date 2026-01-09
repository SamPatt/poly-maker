#!/bin/bash
# Poly-Maker VPS Setup Script
# Run as root on a fresh Ubuntu 22.04 server

set -e

echo "=============================================="
echo "Poly-Maker VPS Setup"
echo "=============================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Please run as root (sudo ./setup.sh)"
    exit 1
fi

# Get the service type from argument
SERVICE_TYPE=${1:-"trading"}
if [ "$SERVICE_TYPE" != "trading" ] && [ "$SERVICE_TYPE" != "updater" ]; then
    echo "Usage: ./setup.sh [trading|updater]"
    echo "  trading - Sets up the trading bot with PostgreSQL (VPS 1)"
    echo "  updater - Sets up the data updater (VPS 2)"
    exit 1
fi

echo "Setting up: $SERVICE_TYPE"

# Update system
echo "Updating system packages..."
apt update && apt upgrade -y

# Install required packages
echo "Installing required packages..."
apt install -y \
    curl \
    git \
    ufw \
    fail2ban \
    unattended-upgrades \
    python3 \
    python3-pip \
    python3-dev \
    libpq-dev

# Install PostgreSQL on trading VPS only
if [ "$SERVICE_TYPE" = "trading" ]; then
    echo "Installing PostgreSQL..."
    apt install -y postgresql postgresql-contrib

    # Start PostgreSQL
    systemctl start postgresql
    systemctl enable postgresql

    # Generate a random password
    DB_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)

    echo "Creating PostgreSQL database and user..."
    sudo -u postgres psql <<EOF
CREATE USER polymaker WITH PASSWORD '${DB_PASSWORD}';
CREATE DATABASE polymaker OWNER polymaker;
GRANT ALL PRIVILEGES ON DATABASE polymaker TO polymaker;
EOF

    # Configure PostgreSQL to listen on Tailscale interface
    echo "Configuring PostgreSQL for Tailscale access..."

    # Find postgresql.conf
    PG_CONF=$(find /etc/postgresql -name "postgresql.conf" | head -1)
    PG_HBA=$(find /etc/postgresql -name "pg_hba.conf" | head -1)

    # Allow connections from Tailscale network (100.x.x.x)
    echo "listen_addresses = 'localhost,100.0.0.0/8'" >> "$PG_CONF"

    # Add Tailscale network to pg_hba.conf
    echo "host    polymaker    polymaker    100.0.0.0/8    scram-sha-256" >> "$PG_HBA"

    # Restart PostgreSQL
    systemctl restart postgresql

    echo ""
    echo "=============================================="
    echo "PostgreSQL Setup Complete!"
    echo "=============================================="
    echo "Database: polymaker"
    echo "User: polymaker"
    echo "Password: ${DB_PASSWORD}"
    echo ""
    echo "SAVE THIS PASSWORD! You'll need it for .env"
    echo "=============================================="
    echo ""
fi

# Install Node.js (needed for poly_merger on trading VPS)
if [ "$SERVICE_TYPE" = "trading" ]; then
    echo "Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
    apt install -y nodejs
fi

# Create polymaker user
echo "Creating polymaker user..."
if ! id "polymaker" &>/dev/null; then
    useradd -m -s /bin/bash polymaker
fi

# Install UV for polymaker user
echo "Installing UV package manager..."
su - polymaker -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'

# Install Tailscale
echo "Installing Tailscale..."
curl -fsSL https://tailscale.com/install.sh | sh

# Create log directory
mkdir -p /var/log/polymaker
chown polymaker:polymaker /var/log/polymaker

echo ""
echo "=============================================="
echo "MANUAL STEPS REQUIRED:"
echo "=============================================="
echo ""
echo "1. Authenticate Tailscale:"
echo "   sudo tailscale up"
echo ""
echo "2. Clone the repository as polymaker user:"
echo "   su - polymaker"
echo "   git clone <your-repo-url> poly-maker"
echo "   cd poly-maker"
echo ""
echo "3. Install dependencies:"
echo "   uv sync"
if [ "$SERVICE_TYPE" = "trading" ]; then
    echo "   cd poly_merger && npm install && cd .."
fi
echo ""
echo "4. Configure environment:"
echo "   cp .env.example .env"
echo "   nano .env  # Edit with your credentials"
if [ "$SERVICE_TYPE" = "trading" ]; then
    echo ""
    echo "   Use these PostgreSQL settings in .env:"
    echo "   DB_HOST=localhost"
    echo "   DB_PORT=5432"
    echo "   DB_NAME=polymaker"
    echo "   DB_USER=polymaker"
    echo "   DB_PASSWORD=${DB_PASSWORD}"
fi
if [ "$SERVICE_TYPE" = "updater" ]; then
    echo ""
    echo "   Use the Tailscale hostname for DB_HOST in .env:"
    echo "   DB_HOST=<trading-vps-tailscale-hostname>"
    echo "   (e.g., trading.tailnet-name.ts.net)"
fi
echo ""
echo "5. Initialize the database (trading VPS only):"
if [ "$SERVICE_TYPE" = "trading" ]; then
    echo "   uv run python -c 'from db.supabase_client import init_database; init_database()'"
fi
echo ""
echo "6. Install and enable the service:"
echo "   sudo cp deploy/$SERVICE_TYPE.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable $SERVICE_TYPE"
echo "   sudo systemctl start $SERVICE_TYPE"
echo ""
echo "7. Check service status:"
echo "   sudo systemctl status $SERVICE_TYPE"
echo "   sudo tail -f /var/log/polymaker/$SERVICE_TYPE.log"
echo ""
echo "=============================================="

# Configure firewall
echo "Configuring firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh

# Allow PostgreSQL from Tailscale on trading VPS
if [ "$SERVICE_TYPE" = "trading" ]; then
    # Allow PostgreSQL only from Tailscale network
    ufw allow from 100.0.0.0/8 to any port 5432
fi

ufw --force enable

# Configure fail2ban
echo "Configuring fail2ban..."
systemctl enable fail2ban
systemctl start fail2ban

# Enable automatic security updates
echo "Enabling automatic security updates..."
dpkg-reconfigure -plow unattended-upgrades

echo ""
echo "Basic setup complete!"
echo "Follow the manual steps above to finish configuration."
