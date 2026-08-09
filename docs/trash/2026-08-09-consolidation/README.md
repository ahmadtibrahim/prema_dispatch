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

- **prema_dispatch** — Core dispatch execution, Planner, driver app, load plans, GPS tracking (v18.0.2.2.0)
- **prema_logistics_booking** — Corridor pricing, customer booking, departures, capacity (v18.0.5.0.0)

## Test Status

Source compilation, XML parsing, JavaScript syntax, and whitespace validation pass for v18.0.5.0.0. Run the focused `prema_v5` tests and the existing module suites on a disposable production-copy database before deployment.
