"""LocalRedact - fully offline PDF PII detection and true redaction.

No module in this package may perform network I/O. ``app.safety`` installs a
process-wide lockdown that turns any outbound connection attempt into an
exception, so an accidental import of a networked library fails loudly instead
of silently leaking a document.
"""

__version__ = "1.0.0"
APP_NAME = "LocalRedact"
