# Minimal config and selectors. Adjust per target site.

LOGIN_URL = "https://luna.compal-health.com/login"  # replace
SCHEDULE_URL_TEMPLATE = "https://example.com/schedule?date={yyyy}-{mm}-{dd}"  # replace

SELECTOR_USERNAME = "#username"  # replace
SELECTOR_PASSWORD = "#password"  # replace
SELECTOR_CAPTCHA_IMG = "#captcha-img"  # replace
SELECTOR_CAPTCHA_INPUT = "#captcha"  # replace
SELECTOR_LOGIN_BUTTON = "#login"  # replace

# Example schedule selectors. Replace with real ones.
ROW_SELECTOR = "table#shifts tbody tr"
COL_EMP_NAME = "td.emp"
COL_START = "td.start"
COL_END = "td.end"

# Slot granularity (minutes) used by solver
SLOT_MIN = 30
