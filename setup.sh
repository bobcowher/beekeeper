#!/usr/bin/env bash
set -euo pipefail

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

if [ -z "$PYTHON_BIN" ]; then
    echo "ERROR: No python3 found. Install Python 3.10+ and re-run."
    exit 1
fi

echo "Python:       $PYTHON_BIN ($($PYTHON_BIN --version))"
echo ""

# --- Create venv & install deps ---
echo "--- Creating virtual environment ---"
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
echo "=== Setup complete ==="
echo "Service status:  sudo systemctl status $SERVICE_NAME"
echo "View logs:       journalctl -u $SERVICE_NAME -f"
echo "App URL:         http://$(hostname -I | awk '{print $1}'):5000"
