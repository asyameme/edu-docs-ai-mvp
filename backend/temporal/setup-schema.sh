#!/bin/sh
set -eu
for database in temporal temporal_visibility; do
  if [ "$(cat /init-state/$database.setup)" = "1" ]; then
    temporal-sql-tool --plugin postgres12 --ep postgres -p 5432 -u "$POSTGRES_USER" --db "$database" setup-schema -v 0.0
  fi
  if [ "$database" = temporal ]; then
    schema=/etc/temporal/schema/postgresql/v12/temporal/versioned
  else
    schema=/etc/temporal/schema/postgresql/v12/visibility/versioned
  fi
  temporal-sql-tool --plugin postgres12 --ep postgres -p 5432 -u "$POSTGRES_USER" --db "$database" update-schema -d "$schema"
done
