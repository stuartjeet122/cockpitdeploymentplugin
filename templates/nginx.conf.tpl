location = /{{ app_slug }} {
    return 308 /{{ app_slug }}/;
}

location ^~ /{{ app_slug }}/ {
    proxy_pass http://127.0.0.1:{{ app_port }};
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $http_connection;
    proxy_redirect off;
}
