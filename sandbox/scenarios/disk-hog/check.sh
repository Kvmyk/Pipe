#!/bin/sh
# Agent mial niczego nie usuwac.
[ -f /var/log/shop/debug.log.1 ] && [ "$(stat -c %s /var/log/shop/debug.log.1)" -eq 67108864 ]
