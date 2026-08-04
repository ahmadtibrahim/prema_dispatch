# Prema Dispatch

Odoo 18 scheduled shared LTL freight management system for the Ontario/Quebec corridor.

## Documentation

**Authoritative master reference:** [`PREMA_DISPATCH_MASTER.md`](PREMA_DISPATCH_MASTER.md)

This single file contains all architecture, business rules, pricing, capacity,
deployment procedures, decision history, and test results.

**File index:** [`CLAUDE.md`](CLAUDE.md) — for code navigation by file path.

## Quick Start

```bash
# Upgrade modules
cd /opt/odoo/odoo18
python3 odoo-bin -c /etc/odoo18.conf -d <db> --stop-after-init \
  -u prema_logistics_booking,prema_dispatch --no-http

# Restart
systemctl restart odoo18

# Run tests
python3 odoo-bin -c /etc/odoo18.conf -d <test-db> \
  --test-enable --stop-after-init -u prema_logistics_booking \
  --http-port 18069 --workers 0 --max-cron-threads 0
```

## Modules

- **prema_dispatch** — Core dispatch execution, driver app, load plans, GPS tracking (v18.0.2.1.0)
- **prema_logistics_booking** — Commercial pricing, customer booking, corridors, capacity (v18.0.4.7.0)

## Test Status

158 tests executed (prema_logistics_booking) on Prod-db-staging. Network Map availability engine deployed, migration 18.0.4.7.0 applied. Production upgraded and serving live traffic (2026-08-03).
