#!/bin/sh
# Ktos "poprawil" port w nginx — aplikacja dziala na 8080, nginx wysyla na 8081.
sed -i 's#proxy_pass http://127.0.0.1:8080;#proxy_pass http://127.0.0.1:8081;#' /etc/nginx/sites-enabled/shop
