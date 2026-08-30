# Odoo auto-discovery finds only modules imported here — each test file
# MUST be listed (Phase 2 gotcha).
from . import test_inbox_notifications
from . import test_inbox_email
from . import test_inbox_business_links
from . import test_inbox_ai_pricing
from . import test_inbox_rules
from . import test_inbox_security
from . import test_inbox_badge_route
from . import test_inbox_gateway
from . import test_inbox_fixes
