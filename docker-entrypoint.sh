#!/bin/sh
set -eu

seed_db="/app/seed/tracker.db"
db_path="${DATABASE_URL:-/app/data/tracker.db}"

if [ -z "$db_path" ]; then
    db_path="/app/data/tracker.db"
fi

case "$db_path" in
    /*) ;;
    *) db_path="/app/$db_path" ;;
esac

if [ -s "$seed_db" ] && [ ! -s "$db_path" ]; then
    mkdir -p "$(dirname "$db_path")"
    cp "$seed_db" "$db_path"
    echo "Seeded database from $seed_db to $db_path"
fi

exec "$@"
