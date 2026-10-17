#!/usr/bin/env bash
# Roll back prema_owner_operator: CODE + DATABASE + FILESTORE together.
# Uninstalling the module alone is NOT a rollback (it leaves the fleet columns
# and the stage link behind, and drops the application data).
set -euo pipefail
BK=${1:?usage: $0 <backup-dir>}
DB=${DB:-Prod-db}
FILESTORE=${FILESTORE:-/root/.local/share/Odoo/filestore/$DB}

systemctl stop odoo18

echo "== code =="
git -C /opt/odoo/releases/prema_dispatch checkout "$(cat "$BK/code_rev.txt")"

echo "== database =="
sudo -u postgres dropdb --if-exists "$DB"
sudo -u postgres createdb -O odoo18 "$DB"
sudo -u postgres pg_restore -d "$DB" --no-owner --no-privileges --role=odoo18 "$BK/db.dump"

echo "== filestore =="
rm -rf "$FILESTORE"
tar xzf "$BK/filestore.tgz" -C "$(dirname "$FILESTORE")"
chown -R odoo18:odoo18 "$FILESTORE" 2>/dev/null || true

systemctl start odoo18
echo "Rolled back. Restore backup dir: $BK"
