"""API-key check for the ingest/read endpoints (set PULSE_API_KEY; header X-API-Key)."""
import hmac


def key_ok(provided, expected):
    if not expected:                    # no key configured -> open (dev mode)
        return True
    return provided is not None and hmac.compare_digest(str(provided), str(expected))
