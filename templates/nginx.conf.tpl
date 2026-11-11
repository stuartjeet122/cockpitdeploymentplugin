location = {{ app_route }} {
    return 308 {{ app_route_slash }}$is_args$args;
}

{{ app_route_location }}
