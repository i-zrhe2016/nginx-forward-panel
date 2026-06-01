#!/bin/sh
set -eu

: "${DB_PATH:=/data/panel.db}"
: "${DB_BACKUP_DIR:=/backups}"
: "${DB_BACKUP_KEEP_DAYS:=7}"
: "${DB_BACKUP_PREFIX:=nginx-forward-panel}"
: "${DB_BACKUP_CRON_SCHEDULE:=0 3 * * *}"

mkdir -p /etc/cron.d "$DB_BACKUP_DIR" /var/log

cat >/etc/cron.d/nginx-forward-panel-db-backup <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
DB_PATH=$DB_PATH
DB_BACKUP_DIR=$DB_BACKUP_DIR
DB_BACKUP_KEEP_DAYS=$DB_BACKUP_KEEP_DAYS
DB_BACKUP_PREFIX=$DB_BACKUP_PREFIX
$DB_BACKUP_CRON_SCHEDULE root python3 /app/scripts/backup_db.py --db-path "$DB_PATH" --backup-dir "$DB_BACKUP_DIR" --keep-days "$DB_BACKUP_KEEP_DAYS" --prefix "$DB_BACKUP_PREFIX" >> /var/log/nginx-forward-panel-db-backup.log 2>&1
EOF

chmod 0644 /etc/cron.d/nginx-forward-panel-db-backup
touch /var/log/nginx-forward-panel-db-backup.log

exec cron -f
