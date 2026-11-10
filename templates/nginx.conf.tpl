location = {{ app_route }} {
    return 308 {{ app_route_slash }};
}

{{ app_route_location }}
