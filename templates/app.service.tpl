[Unit]
# Managed by Cockpit App Manager
Description=Cockpit App Manager - {{ app_id }}
After=network.target

[Service]
Type=simple
User={{ app_user }}
Group={{ app_user }}
WorkingDirectory={{ app_root }}/current/app
EnvironmentFile=-{{ app_env_file }}
Environment=NODE_ENV=production
Environment=PORT={{ app_port }}
Environment=HOST=127.0.0.1
Environment=APP_BASE_PATH=/{{ app_id }}
Environment=APP_DATA_DIR={{ app_root }}/shared/data
Environment=APP_STORAGE_DIR={{ app_root }}/shared
{{ app_storage_environment }}
ExecStart={{ start_command }}
Restart=on-failure
RestartSec=5
CPUAccounting=true
MemoryAccounting=true
TasksAccounting=true
MemoryMax={{ memory_mb }}M
CPUQuota={{ cpu_percent }}%
TasksMax={{ tasks_max }}
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
ReadWritePaths={{ app_home }} {{ app_config_dir }} {{ app_log_dir }} {{ app_storage_paths }}

[Install]
WantedBy=multi-user.target
