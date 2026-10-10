#!/bin/sh
set -eu
attempt=0
until temporal operator cluster health --address temporal:7233 >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  [ "$attempt" -lt 60 ] || exit 1
  sleep 2
done
if temporal operator namespace describe --address temporal:7233 -n education >/dev/null 2>&1; then
  echo 'Namespace education already exists'
else
  temporal operator namespace create --address temporal:7233 -n education --retention 7d
fi
