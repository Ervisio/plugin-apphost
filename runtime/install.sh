#!/bin/sh
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run as root'; exit 1; }
command -v systemctl >/dev/null || { echo 'systemd required'; exit 1; }
command -v runuser >/dev/null || { echo 'runuser required'; exit 1; }
id ervisio-apphost >/dev/null 2>&1 || useradd --system --user-group --home-dir /srv/ervisio/apphost --shell /usr/sbin/nologin ervisio-apphost
install -d -m 0755 /opt/ervisio-apphost
install -m 0755 "$(dirname "$0")/apphost.py" /opt/ervisio-apphost/apphost.py
install -m 0755 "$(dirname "$0")/app_runner.py" /opt/ervisio-apphost/app_runner.py
install -m 0755 "$(dirname "$0")/app_shell.py" /opt/ervisio-apphost/app_shell.py
install -d -m 0750 -o root -g ervisio-apphost /srv/ervisio/apphost
cat > /etc/systemd/system/ervisio-apphost.service <<'EOF'
[Unit]
Description=Ervisio AppHost control service
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/ervisio-apphost/apphost.py
Restart=on-failure
RuntimeDirectory=ervisio-apphost
RuntimeDirectoryMode=0700
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/srv/ervisio/apphost /run/ervisio-apphost /etc/systemd/system
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/ervisio-apphost@.service <<'EOF'
[Unit]
Description=Ervisio hosted application %i
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=ervisio-apphost
Group=ervisio-apphost
WorkingDirectory=/srv/ervisio/apphost/%i/work
ExecStart=/usr/bin/python3 /opt/ervisio-apphost/app_runner.py %i
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillMode=control-group
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/srv/ervisio/apphost/%i
PrivateTmp=true
UMask=0077
MemoryAccounting=yes
CPUAccounting=yes
TasksAccounting=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ervisio-apphost.service
echo 'AppHost installed. Open Ervisio > AppHost.'
