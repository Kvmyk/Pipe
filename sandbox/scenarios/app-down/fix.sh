#!/bin/sh
sed -i 's/^port = .*/port = 8080/' /etc/shop/app.conf
shopctl restart
