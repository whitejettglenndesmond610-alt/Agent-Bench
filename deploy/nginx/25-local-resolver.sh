#!/bin/sh
set -eu

resolver=""
while read -r key value _; do
    if [ "$key" = "nameserver" ]; then
        resolver="$value"
        break
    fi
done < /etc/resolv.conf

if [ -z "$resolver" ]; then
    echo "No container DNS resolver found" >&2
    exit 1
fi

sed "s/__NGINX_RESOLVER__/$resolver/" \
    /etc/nginx/templates/default.conf.template \
    > /etc/nginx/conf.d/default.conf
