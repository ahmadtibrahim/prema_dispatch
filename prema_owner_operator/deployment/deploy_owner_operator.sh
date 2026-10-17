#!/usr/bin/env bash
# Deploy prema_owner_operator to production.
# Run ONLY after the staging suite is green and the agreements exist.
set -euo pipefail
DB=${DB:-Prod-db}
CONF=${CONF:-/etc/odoo18.conf}
STAMP=$(date +%Y%m%d_%H%M)
BK=${BK:-/root/ownerop_backup_$STAMP}
# The odoo18 service runs with HOME=/opt/odoo, so its data_dir is
# /opt/odoo/.local/share/Odoo — NOT /root/.local/share/Odoo.
FILESTORE=${FILESTORE:-/opt/odoo/.local/share/Odoo/filestore/$DB}
CODE_DIR=${CODE_DIR:-/opt/odoo/releases/prema_dispatch}

mkdir -p "$BK"
echo "== 1/5 backup code =="
git -C "$CODE_DIR" rev-parse HEAD > "$BK/code_rev.txt"
git -C "$CODE_DIR" status --short > "$BK/code_status.txt"
# The live tree carries uncommitted changes; a rev alone cannot restore it.
tar czf "$BK/code.tgz" -C "$(dirname "$CODE_DIR")" "$(basename "$CODE_DIR")"

echo "== 2/5 backup database =="
sudo -u postgres pg_dump -Fc "$DB" -f "/tmp/ownerop_$STAMP.dump"
sudo mv "/tmp/ownerop_$STAMP.dump" "$BK/db.dump"

echo "== 3/5 backup filestore =="
tar czf "$BK/filestore.tgz" -C "$(dirname "$FILESTORE")" "$(basename "$FILESTORE")"
# 38 attachments referenced by the DB were written by earlier root-run
# processes and live only in the legacy store. Back it up too.
LEGACY_FILESTORE=${LEGACY_FILESTORE:-/root/.local/share/Odoo/filestore/$DB}
if [ -d "$LEGACY_FILESTORE" ]; then
  tar czf "$BK/filestore_root_extra.tgz" -C "$(dirname "$LEGACY_FILESTORE")" \
    "$(basename "$LEGACY_FILESTORE")"
fi

echo "== 4/5 pause crons =="
IDS=$(sudo -u postgres psql -d "$DB" -A -t -c "SELECT id FROM ir_cron WHERE active ORDER BY id;" | paste -sd, -)
echo "$IDS" > "$BK/cron_ids.txt"
sudo -u postgres psql -d "$DB" -c "UPDATE ir_cron SET active=false;"

echo "== 5/5 install or upgrade =="
cd /opt/odoo/odoo18
ACTION=-u
if [ "$(sudo -u postgres psql -d "$DB" -A -t -c \
    "SELECT state FROM ir_module_module WHERE name='prema_owner_operator';")" \
    != "installed" ]; then
  ACTION=-i
fi
# Run as the service user: HOME=/opt/odoo is what selects the real filestore.
sudo -u odoo18 env HOME=/opt/odoo /opt/odoo/venv-18/bin/python3 odoo-bin \
  -c "$CONF" -d "$DB" --stop-after-init --no-http $ACTION prema_owner_operator \
  --max-cron-threads 0
sudo -u postgres psql -d "$DB" -c "UPDATE ir_cron SET active=true WHERE id IN ($IDS);"
[ -n "${SKIP_RESTART:-}" ] || systemctl restart "${SERVICE:-odoo18}"

echo "Deployed. Verify:"
sudo -u postgres psql -d "$DB" -A -t -c \
  "SELECT latest_version FROM ir_module_module WHERE name='prema_owner_operator';"
echo "Rollback data is in $BK"
