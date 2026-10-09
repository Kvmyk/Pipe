#!/bin/sh
# Literowka w porcie: aplikacja konczy sie bledem przy starcie, nginx daje 502.
sed -i 's/^port = 8080$/port = 80800O/' /etc/shop/app.conf
