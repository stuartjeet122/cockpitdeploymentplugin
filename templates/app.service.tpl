[Unit]
# Managed by Cockpit App Manager
Description=Cockpit App Manager - {{ app_id }}
After=network.target

[Service]
Type=simple
User={{ app_user }}
Group={{ app_user }}
WorkingDirectory={{ app_root }}/current/app
Environment=NODE_ENV=production
Environment=PORT={{ app_port }}
Environment=HOST=127.0.0.1
Environment=APP_BASE_PATH=/{{ app_id }}
ExecStart={{ start_command }}
Restart=on-failure
RestartSec=5
StandardOutput=append:{{ app_log_dir }}/stdout.log
StandardError=append:{{ app_log_dir }}/stderr.log
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths={{ app_root }}/releases
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
LockPersonality=true
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
UMask=0027
ReadWritePaths={{ app_home }} {{ app_config_dir }} {{ app_log_dir }}

[Install]
WantedBy=multi-user.target
