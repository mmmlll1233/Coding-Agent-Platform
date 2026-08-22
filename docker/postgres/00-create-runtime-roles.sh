#!/bin/sh
set -eu

api_password="$(cat /run/secrets/api_db_password)"
worker_password="$(cat /run/secrets/worker_db_password)"

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=api_password="$api_password" \
  --set=worker_password="$worker_password" <<-'EOSQL'
CREATE ROLE mewcode_api LOGIN PASSWORD :'api_password';
CREATE ROLE mewcode_worker LOGIN PASSWORD :'worker_password';
EOSQL
