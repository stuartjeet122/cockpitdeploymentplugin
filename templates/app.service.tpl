[Unit]
Description=App Manager service for {{ app_name }}
After=network.target
Wants=network.target

[Service]
Type=simple
WorkingDirectory={{ app_root }}/current
Environment=NODE_ENV=production
ExecStart={{ start_command }}
Restart=on-failure
RestartSec=5
User={{ app_user }}
Group={{ app_group }}
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
