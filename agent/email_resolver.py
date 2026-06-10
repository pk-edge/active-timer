"""First-run email resolution: ask the user, fall back to browser detection.

MUST run on the main thread BEFORE the enrollment thread starts — Qt dialogs
need the main thread and a QApplication (created here if needed; _run_widget
reuses it via QApplication.instance()). Every failure degrades to the next
step: typed email → browser auto-detect → None. Enrollment never blocks on
email resolution.
"""
import logging
import sys

from email_detect import detect_first_signed_in_email

log = logging.getLogger("email_resolver")


def _prompt_for_email() -> str | None:
    try:
        from PySide6.QtWidgets import QApplication, QInputDialog

        QApplication.instance() or QApplication(sys.argv)
        text, ok = QInputDialog.getText(
            None,
            "TimeTracker Setup",
            "Enter your work email\n(leave blank to auto-detect from your browser):",
        )
        if ok:
            email = text.strip().lower()
            if "@" in email:
                return email
        return None
    except Exception as e:
        log.warning("Email prompt unavailable: %s", e)
        return None


def resolve_employee_email() -> str | None:
    email = _prompt_for_email()
    if email:
        log.info("Employee email entered by user")
        return email
    email = detect_first_signed_in_email()
    if email:
        log.info("Employee email auto-detected from browser profile")
        return email
    log.info("No employee email resolved — device will enroll unlinked")
    return None
