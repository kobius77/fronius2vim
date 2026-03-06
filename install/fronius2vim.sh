#!/usr/bin/env bash

# fronius2vim Proxmox LXC Installer
# Source: https://github.com/kobius77/fronius2vim

set -e

# --- UI Colors ---
YW=$(echo "\033[33m")
BL=$(echo "\033[36m")
RD=$(echo "\033[01;31m")
BGN=$(echo "\033[4;92m")
GN=$(echo "\033[1;92m")
DGN=$(echo "\033[32m")
CL=$(echo "\033[m")
BFR="\\r\\033[K"
HOLD="-"
CM="${GN}✓${CL}"
CROSS="${RD}✗${CL}"

echo -e "${BL}Starting fronius2vim LXC creation...${CL}"

function msg_info() {
    local msg="$1"
    echo -ne " ${HOLD} ${YW}${msg}..."
}

function msg_ok() {
    local msg="$1"
    echo -e "${BFR} ${CM} ${GN}${msg}${CL}"
}

function msg_error() {
    local msg="$1"
    echo -e "${BFR} ${CROSS} ${RD}${msg}${CL}"
}

# 1. Check Root
if [[ "$(id -u)" -ne 0 ]]; then
    msg_error "This script must be run as root."
    exit 1
fi

# 2. Basic WHIPTAIL UI for Settings
NEXTID=$(pvesh get /cluster/nextid)

set +e
CTID=$(whiptail --title "fronius2vim LXC" --inputbox "Enter Container ID" 10 58 $NEXTID 3>&1 1>&2 2>&3)
exitstatus=$?
if [ $exitstatus != 0 ]; then
    echo "Cancelled."
    exit 1
fi

STORAGES=$(pvesm status -content rootdir | awk 'NR>1 {print $1}')
DEFAULT_STORAGE=$(echo "$STORAGES" | grep -E "local-lvm|local-zfs" | head -n 1 || true)
if [ -z "$DEFAULT_STORAGE" ]; then DEFAULT_STORAGE=$(echo "$STORAGES" | head -n 1); fi

STORAGE=$(whiptail --title "fronius2vim LXC" --inputbox "Enter Storage Pool for Container" 10 58 $DEFAULT_STORAGE 3>&1 1>&2 2>&3)
exitstatus=$?
if [ $exitstatus != 0 ]; then
    echo "Cancelled."
    exit 1
fi
set -e

# 3. Download Debian 12 Template
msg_info "Updating container templates"
pveam update >/dev/null
msg_ok "Updated container templates"

TEMPLATE=$(pveam available -section system | grep "debian-12-standard" | head -1 | awk '{print $2}')
if [ -z "$TEMPLATE" ]; then
    msg_error "Could not find Debian 12 template."
    exit 1
fi
TEMPLATE_FILE=$(basename "$TEMPLATE")

if ! pveam list local | grep -q "$TEMPLATE_FILE"; then
    msg_info "Downloading $TEMPLATE_FILE"
    pveam download local $TEMPLATE >/dev/null
    msg_ok "Downloaded $TEMPLATE_FILE"
fi

# 4. Create the container
msg_info "Creating LXC Container"
pct create $CTID local:vztmpl/$TEMPLATE_FILE \
    --arch amd64 \
    --hostname fronius2vim \
    --cores 1 \
    --memory 512 \
    --swap 0 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1 \
    --features nesting=1 \
    --rootfs ${STORAGE}:4 >/dev/null
msg_ok "Created LXC Container"

# 5. Start the container
msg_info "Starting LXC Container"
pct start $CTID
msg_ok "Started LXC Container"

msg_info "Waiting for network"
IP=""
for i in {1..20}; do
    sleep 2
    IP=$(pct exec $CTID -- ip -4 addr show eth0 2>/dev/null | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n 1 || true)
    if [ -n "$IP" ]; then
        break
    fi
done

if [ -z "$IP" ]; then
    msg_error "Failed to get IP address from DHCP."
    exit 1
fi
msg_ok "Network ready (IP: $IP)"

# 6. Install Python and dependencies inside the container
msg_info "Installing Dependencies"
pct exec $CTID -- bash -c "apt-get update >/dev/null && apt-get install -y --no-install-recommends git python3 python3-pip python3-venv >/dev/null"
msg_ok "Installed Dependencies"

# 7. Setup fronius2vim
msg_info "Setting up fronius2vim"
pct exec $CTID -- bash -c "git clone https://github.com/kobius77/fronius2vim.git /opt/fronius2vim >/dev/null 2>&1"
pct exec $CTID -- bash -c "cd /opt/fronius2vim && python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt >/dev/null 2>&1"
msg_ok "Set up fronius2vim"

# 8. Create Service
msg_info "Creating Systemd Service"
pct exec $CTID -- bash -c 'cat <<EOF >/etc/systemd/system/fronius2vim.service
[Unit]
Description=fronius2vim - Fronius to VictoriaMetrics Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/fronius2vim
Environment=FRONIUS_HOST=172.20.203.100
Environment=VICTORIAMETRICS_URL=http://172.20.204.22:8428
Environment=REALTIME_INTERVAL=10
Environment=ENERGY_INTERVAL=900
Environment=WEB_PORT=8080
Environment=LOG_LEVEL=INFO
Environment=PATH=/opt/fronius2vim/venv/bin
ExecStart=/opt/fronius2vim/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF'

pct exec $CTID -- bash -c "systemctl daemon-reload && systemctl enable --now fronius2vim.service >/dev/null 2>&1"
msg_ok "Started Systemd Service"

echo -e "\n${GN}Successfully created fronius2vim LXC!${CL}"
echo -e "${YW}Dashboard is accessible at:${CL} ${BGN}http://${IP}:8080${CL}\n"
echo -e "${BL}To configure environment variables (Fronius IP, etc.):${CL}"
echo -e "1. Run: ${YW}pct enter $CTID${CL}"
echo -e "2. Edit: ${YW}nano /etc/systemd/system/fronius2vim.service${CL}"
echo -e "3. Apply: ${YW}systemctl daemon-reload && systemctl restart fronius2vim${CL}\n"
