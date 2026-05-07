#!/usr/bin/env bash
set -euo pipefail

# --- Parse arguments ---
AUTO_YES=false
if [[ "${1:-}" == "-y" || "${1:-}" == "--yes" ]]; then
    AUTO_YES=true
fi

# --- Resolve install location ---
BEEKEEPER_HOME="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$BEEKEEPER_HOME/venv"
SERVICE_NAME="beekeeper"
CURRENT_USER="$(whoami)"

echo "=== Beekeeper Setup ==="
echo "Install dir:  $BEEKEEPER_HOME"
echo "User:         $CURRENT_USER"
echo ""

# --- Python detection ---
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    echo "ERROR: No python3 found. Install Python 3.10+ and re-run." >&2
    exit 1
fi

echo "Python:       $PYTHON_BIN ($($PYTHON_BIN --version))"
echo ""

# --- Create venv & install deps ---
echo "--- Creating virtual environment ---"

# Detect Python version for package checking
PYTHON_VERSION=$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
VENV_PACKAGE="python${PYTHON_VERSION}-venv"

# Check if venv package is installed (Debian/Ubuntu specific)
if command -v apt &>/dev/null && ! dpkg -l | grep -q "^ii.*$VENV_PACKAGE"; then
    echo ""
    echo "ERROR: $VENV_PACKAGE is not installed." >&2
    echo ""
    echo "On Debian/Ubuntu systems, you need to install the venv package:"
    echo "    sudo apt update"
    echo "    sudo apt install $VENV_PACKAGE"
    echo ""

    if [[ "$AUTO_YES" = true ]]; then
        echo "Auto-installing $VENV_PACKAGE (--yes mode)..."
        sudo apt update -qq
        sudo apt install -y $VENV_PACKAGE
        echo "✓ $VENV_PACKAGE installed"
        echo ""
    else
        read -p "Install $VENV_PACKAGE now? (y/N): " -n 1 -r
        echo ""
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "Installing $VENV_PACKAGE..."
            sudo apt update -qq
            sudo apt install -y $VENV_PACKAGE
            echo "✓ $VENV_PACKAGE installed"
            echo ""
        else
            echo "Aborted. Please install $VENV_PACKAGE and re-run setup."
            exit 1
        fi
    fi
fi

# Create venv
$PYTHON_BIN -m venv "$VENV_DIR"

echo "--- Installing dependencies ---"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$BEEKEEPER_HOME/requirements.txt" -q

echo "Dependencies installed."
echo ""

# --- Create projects directory ---
mkdir -p "$BEEKEEPER_HOME/projects"

# --- Generate systemd service file ---
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TEMP_SERVICE=$(mktemp)

cat > "$TEMP_SERVICE" <<EOF
[Unit]
Description=Beekeeper Training Manager
After=network.target

[Service]
User=$CURRENT_USER
WorkingDirectory=$BEEKEEPER_HOME
ExecStart=$VENV_DIR/bin/gunicorn \\
    --bind 0.0.0.0:5000 \\
    --workers 1 \\
    --threads 16 \\
    --timeout 120 \\
    "app:create_app()"
Restart=on-failure
RestartSec=5
Environment=BEEKEEPER_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(16))")
Environment=PATH=$VENV_DIR/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=PYTHONPATH=
Environment=PYTHONHOME=
Environment=CONDA_PREFIX=
Environment=VIRTUAL_ENV=$VENV_DIR

[Install]
WantedBy=multi-user.target
EOF

echo "--- Installing systemd service ---"
echo "This requires sudo to write to $SERVICE_FILE"
sudo cp "$TEMP_SERVICE" "$SERVICE_FILE"
rm "$TEMP_SERVICE"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo ""
echo "--- Optional: Passwordless service management ---"
echo "This allows restarting the service without a password prompt."
echo "Useful for development (e.g., quick restarts after code changes)."
echo ""

ENABLE_PASSWORDLESS=false
if [[ "$AUTO_YES" = true ]]; then
    echo "Skipping passwordless sudo setup (--yes mode, use interactive setup to enable)"
else
    read -p "Enable passwordless sudo for beekeeper service? (y/N): " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ENABLE_PASSWORDLESS=true
    fi
fi

if [[ "$ENABLE_PASSWORDLESS" = true ]]; then
    SUDOERS_FILE="/etc/sudoers.d/$SERVICE_NAME"
    TEMP_SUDOERS=$(mktemp)
    SYSTEMCTL_PATH="$(command -v systemctl)"

    cat > "$TEMP_SUDOERS" <<EOF
# Allow $CURRENT_USER to manage beekeeper service without password
$CURRENT_USER ALL=(ALL) NOPASSWD: $SYSTEMCTL_PATH start $SERVICE_NAME, \\
                                  $SYSTEMCTL_PATH stop $SERVICE_NAME, \\
                                  $SYSTEMCTL_PATH restart $SERVICE_NAME, \\
                                  $SYSTEMCTL_PATH status $SERVICE_NAME
EOF

    # Validate syntax before installing
    if sudo visudo -cf "$TEMP_SUDOERS"; then
        sudo cp "$TEMP_SUDOERS" "$SUDOERS_FILE"
        sudo chmod 440 "$SUDOERS_FILE"
        rm "$TEMP_SUDOERS"
        echo "✓ Passwordless sudo configured for: start, stop, restart, status"
    else
        echo "✗ Sudoers syntax validation failed. Skipping."
        rm "$TEMP_SUDOERS"
    fi
else
    echo "Skipped. You'll need to use 'sudo systemctl restart $SERVICE_NAME'."
fi

echo ""
echo "=== Setup complete ==="
echo "Service status:  sudo systemctl status $SERVICE_NAME"
echo "View logs:       journalctl -u $SERVICE_NAME -f"
echo "App URL:         http://$(hostname -I | awk '{print $1}'):5000" # NOSONAR
