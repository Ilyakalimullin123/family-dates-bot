#!/bin/sh
set -eu
umask 077
backup_dir=/var/backups/family-dates-bot
mkdir -p "$backup_dir"
backup_file="$backup_dir/family_dates_$(date -u +%Y%m%dT%H%M%SZ).dump"
runuser -u postgres -- pg_dump -Fc family_dates_db > "$backup_file.tmp"
mv "$backup_file.tmp" "$backup_file"
find "$backup_dir" -name 'family_dates_*.dump' -mtime +14 -delete
