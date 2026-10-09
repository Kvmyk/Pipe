#!/bin/sh
sed -i 's#proxy_pass http://127.0.0.1:8081;#proxy_pass http://127.0.0.1:8080;#' /etc/nginx/sites-enabled/shop
nginx -t && nginx -s reload
