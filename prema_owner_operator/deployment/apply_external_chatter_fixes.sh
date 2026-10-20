#!/usr/bin/env bash
# Copy the tracked chatter-rendering fixes into addons that live outside this
# repository. Idempotent: a matching destination is left untouched.
set -euo pipefail
SRC=${SRC:-"$(cd "$(dirname "$0")" && pwd)/external"}

# source-relative-path : destination-path
MAP=(
  "prema_mail_tracking/controllers/main.py:/opt/odoo/custom-addons/prema_mail_tracking/controllers/main.py"
  "prema_ai_auditor/services/crm_operator.py:/opt/odoo/custum-addons/prema_ai_auditor/services/crm_operator.py"
)

for pair in "${MAP[@]}"; do
  rel=${pair%%:*}
  dst=${pair#*:}
  src="$SRC/$rel"
  if [ ! -f "$src" ]; then
    echo "external chatter fix missing from the release: $src" >&2
    exit 1
  fi
  if [ ! -f "$dst" ]; then
    echo "external module not present, skipping: $dst"
    continue
  fi
  if cmp -s "$src" "$dst"; then
    echo "already current: $dst"
    continue
  fi
  backup="$dst.bak-$(date +%Y%m%d_%H%M%S)"
  cp -a "$dst" "$backup"
  install -m 0644 -o odoo18 -g odoo18 "$src" "$dst"
  cmp -s "$src" "$dst" || { echo "apply failed: $dst" >&2; exit 1; }
  echo "applied: $dst (backup $backup)"
done
