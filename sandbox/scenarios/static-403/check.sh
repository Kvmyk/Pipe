#!/bin/sh
curl -fsS --max-time 5 http://127.0.0.1/static/logo.txt | grep -q "logo sklepu"
