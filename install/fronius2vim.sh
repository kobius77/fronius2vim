#!/usr/bin/env bash
source <(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/misc/build.func)
# Copyright (c) 2021-2026 community-scripts
# Author: fronius2vim
# License: MIT | https://github.com/kobius77/fronius2vim/raw/main/LICENSE
# Source: https://github.com/kobius77/fronius2vim

APP="fronius2vim"
var_tags="${var_tags:-monitoring;photovoltaic}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-512}"
var_disk="${var_disk:-4}"
var_os="${var_os:-debian}"
var_version="${var_version:-12}"
var_unprivileged="${var_unprivileged:-1}"

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_container_storage
  check_container_resources
  if [[ ! -f /etc/systemd/system/fronius2vim.service ]]; then
    msg_error "No ${APP} Installation Found!"
    exit
  fi
  
  msg_info "Stopping fronius2vim"
  systemctl stop fronius2vim
  msg_ok "Stopped fronius2vim"

  msg_info "Updating fronius2vim"
  cd /opt/fronius2vim
  git pull
  pip install -r requirements.txt --quiet
  msg_ok "Updated fronius2vim"

  msg_info "Starting fronius2vim"
  systemctl start fronius2vim
  msg_ok "Started fronius2vim"
  msg_ok "Updated successfully!"
  exit
}

start
build_container
description

msg_info "Installing Dependencies"
$STD apt-get update
$STD apt-get install -y git python3-pip python3-venv
msg_ok "Installed Dependencies"

msg_info "Setting up fronius2vim"
mkdir -p /opt/fronius2vim
cd /opt/fronius2vim
git clone https://github.com/kobius77/fronius2vim.git . 2>/dev/null || git pull

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt --quiet
deactivate
msg_ok "Set up fronius2vim"

msg_info "Creating Service"
cat <<EOF >/etc/systemd/system/fronius2vim.service
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
EOF

systemctl daemon-reload
systemctl enable --now fronius2vim.service
msg_ok "Created Service"

motd_ssh
customize

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Access it using the following URL:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:8080${CL}"
echo -e "${INFO}${YW} Configuration:${CL}"
echo -e "${TAB}Edit /etc/systemd/system/fronius2vim.service to change settings"
echo -e "${TAB}Then run: systemctl daemon-reload && systemctl restart fronius2vim"
