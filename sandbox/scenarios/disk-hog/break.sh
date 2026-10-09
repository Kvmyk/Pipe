#!/bin/sh
# Zapomniany log debugowania: 64 MB w /var/log/shop.
head -c 67108864 /dev/zero | tr '\0' 'x' > /var/log/shop/debug.log.1
