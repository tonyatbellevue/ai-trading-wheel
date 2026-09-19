"""Headless smoke test for app/gui.py.

Tkinter is not importable on a headless build machine, and a GUI cannot be
clicked in CI anyway. This substitutes a permissive stub for ``tkinter`` and
``tkinter.ttk`` so the window's construction and its pure-logic paths
(refresh_table, selection toggling, preview geometry, the export summary text)
actually execute. It catches wrong attribute names, bad signatures and broken
cross-module calls - not visual layout.

On Windows with a real Tkinter, run the app instead: ``python run_app.py``.

    python tests/smoke_gui_headless.py
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Stub:
    """A widget stand-in that tolerates any call, attribute or subscript.

    Deliberately not MagicMock: the app subclasses ``tk.Tk``, and MagicMock's
    auto-speccing tries to instantiate the subclass when a child mock is
    created, which fails.
    """

    def __init__(self, *_args, **_kwargs):
        pass

    def __call__(self, *_args, **_kwargs):
        return _Stub()

    def __getattr__(self, _name):
        return _Stub()

    def __getitem__(self, _key):
        return 0

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True


class _Var:
    def __init__(self, value=None, **_kwargs):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


def _install_tk_stubs() -> None:
    tk = types.ModuleType("tkinter")
    for name in (
        "LEFT", "RIGHT", "TOP", "BOTTOM", "BOTH", "X", "Y", "NW", "W", "END",
        "VERTICAL", "HORIZONTAL", "SUNKEN", "CENTER", "DISABLED", "NORMAL",
    ):
        setattr(tk, name, name.lower())
    tk.BooleanVar = _Var
    tk.StringVar = _Var
    tk.Tk = _Stub
    tk.Canvas = _Stub
    tk.PhotoImage = _Stub

    ttk = types.ModuleType("tkinter.ttk")
    for widget in (
        "Frame", "Label", "Button", "Entry", "Checkbutton", "Combobox",
        "Treeview", "Scrollbar", "Progressbar", "Labelframe", "Panedwindow",
        "Style", "Notebook",
    ):
        setattr(ttk, widget, _Stub)

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = lambda **_k: ""
    filedialog.asksaveasfilename = lambda **_k: ""
    messagebox = types.ModuleType("tkinter.messagebox")
    for fn in ("showinfo", "showwarning", "showerror"):
        setattr(messagebox, fn, lambda *_a, **_k: None)

    tk.ttk = ttk
    tk.filedialog = filedialog
    tk.messagebox = messagebox
    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.filedialog"] = filedialog
    sys.modules["tkinter.messagebox"] = messagebox


_install_tk_stubs()

from app.gui import RedactorApp                     # noqa: E402
from app.models import Category                     # noqa: E402
from app.pdfcompat import fitz                      # noqa: E402
from app.scanner import PdfSession                  # noqa: E402

CHECKS = []


def check(label, condition):
    CHECKS.append((label, bool(condition)))
    print(f"{'PASS' if condition else 'FAIL'}  {label}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="guismoke-"))
    src = tmp / "sample.pdf"
    doc = fitz.open()
    page = doc.new_page()
    for i, line in enumerate(
        ["Name: John Anderson", "NRIC No: S1234567D", "Email: a@b.com"]
    ):
        page.insert_text((60, 90 + i * 20), line, fontsize=11, fontname="helv")
    doc.save(str(src))
    doc.close()

    app = RedactorApp()
    check("window constructs", app is not None)
    check("all categories have a checkbox", len(app.category_vars) == len(list(Category)))

    app.session = PdfSession(src)
    result = app.session.scan()
    app.detections = list(result.detections)
    check("scan produced detections", len(app.detections) >= 3)

    app.refresh_table()
    check("every detection got a table row", len(app._row_for_uid) == len(app.detections))
    check("row -> uid map is complete", len(app._uid_for_row_map) == len(app.detections))

    uid = app.detections[0].uid
    row = app._row_for_uid[uid]
    check("row lookup round-trips", app._uid_for_row(row) == uid)
    check("detection lookup round-trips", app._detection(uid) is app.detections[0])

    app.set_all(False)
    check("set_all(False) unticks everything", not any(d.selected for d in app.detections))
    app.set_all(True)
    check("set_all(True) ticks everything", all(d.selected for d in app.detections))

    app.select_high()
    check(
        "high-confidence filter unticks the REVIEW items",
        all(d.selected != d.needs_review for d in app.detections),
    )

    app.set_all(True)
    app.refresh_preview()
    check("preview drew a box for every rect on page 1",
          len(app._rect_items) == sum(len(d.rects) for d in app.detections if d.page == 0))

    masked = app.detections[0].preview(reveal=False)
    full = app.detections[0].preview(reveal=True)
    check("values are masked until revealed", masked != full and "*" in masked)

    app.current_page = 0
    app.on_next_page()
    check("page navigation is clamped to the document", app.current_page == 0)

    app._on_close()
    check("close tears down the scratch directory", app._tempdir.path is None)

    failed = [label for label, ok in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("failed:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
