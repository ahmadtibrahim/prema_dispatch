#!/usr/bin/env bash
# Deploy prema_owner_operator to production.
# Run ONLY after the staging suite is green and the agreements exist.
set -euo pipefail
DB=${DB:-Prod-db}
CONF=${CONF:-/etc/odoo18.conf}
STAMP=$(date +%Y%m%d_%H%M)
BK=${BK:-/root/ownerop_backup_$STAMP}
FILESTORE=${FILESTORE:-/root/.local/share/Odoo/filestore/$DB}

mkdir -p "$BK"
echo "== 1/5 backup code =="
git -C /opt/odoo/releases/prema_dispatch rev-parse HEAD > "$BK/code_rev.txt"
git -C /opt/odoo/releases/prema_dispatch status --short > "$BK/code_status.txt"

echo "== 2/5 backup database =="
sudo -u postgres pg_dump -Fc "$DB" -f "/tmp/ownerop_$STAMP.dump"
sudo mv "/tmp/ownerop_$STAMP.dump" "$BK/db.dump"

echo "== 3/5 backup filestore =="
tar czf "$BK/filestore.tgz" -C "$(dirname "$FILESTORE")" "$(basename "$FILESTORE")"

echo "== 4/5 pause crons =="
IDS=$(sudo -u postgres psql -d "$DB" -A -t -c "SELECT id FROM ir_cron WHERE active ORDER BY id;" | paste -sd, -)
echo "$IDS" > "$BK/cron_ids.txt"
sudo -u postgres psql -d "$DB" -c "UPDATE ir_cron SET active=false;"

echo "== 5/5 upgrade =="
cd /opt/odoo/odoo18
/opt/odoo/venv-18/bin/python3 odoo-bin -c "$CONF" -d "$DB" \
  --stop-after-init --no-http -u prema_owner_operator --max-cron-threads 0
sudo -u postgres psql -d "$DB" -c "UPDATE ir_cron SET active=true WHERE id IN ($IDS);"
systemctl restart odoo18

echo "Deployed. Verify:"
sudo -u postgres psql -d "$DB" -A -t -c \
  "SELECT latest_version FROM ir_module_module WHERE name='prema_owner_operator';"
echo "Rollback data is in $BK"
