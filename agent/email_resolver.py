"""First-run email resolution: silently detect from browser profiles.

No user interaction. Reads the first signed-in Chrome/Edge profile email.
Every failure degrades gracefully — enrollment never blocks on email resolution.
"""
import logging

from email_detect import detect_first_signed_in_email

log = logging.getLogger("email_resolver")


def resolve_employee_email() -> str | None:
    email = detect_first_signed_in_email()
    if email:
        log.info("Employee email auto-detected from browser profile: %s", email)
        return email
    log.info("No employee email resolved — device will enroll unlinked")
    return None
