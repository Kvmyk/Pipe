#!/bin/sh
sleep 1
curl -fsS --max-time 5 http://127.0.0.1/ | grep -q "Sklep dziala"
