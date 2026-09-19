"""LocalRedact entry point.

Network lockdown is installed before anything else is imported so that no
dependency can open a socket even during its own import. Run with:

    python run_app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running straight from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.safety import enable_network_lockdown  # noqa: E402

enable_network_lockdown()


def main() -> int:
    try:
        from app.gui import run
    except ImportError as exc:
        missing = str(exc)
        if "tkinter" in missing.lower():
            sys.stderr.write(
                "Tkinter is missing. It ships with the python.org Windows "
                "installer; reinstall Python with the 'tcl/tk and IDLE' option "
                "ticked, or use the command line interface:\n"
                "    python -m app.cli input.pdf -o output.pdf\n"
            )
            return 2
        if "pymupdf" in missing.lower() or "fitz" in missing.lower():
            sys.stderr.write(
                "PyMuPDF is missing. Install it with:\n"
                "    pip install -r requirements.txt\n"
            )
            return 2
        raise
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
