"""Tkinter desktop UI: choose PDF -> scan -> review -> export.

Tkinter is deliberate: it ships with CPython, so the packaged .exe needs no Qt
runtime and stays small, and there is no chance of a UI toolkit pulling in a
telemetry or auto-update component.

Nothing here writes document content to disk or to the log. Detected values live
in memory for the lifetime of the review and are shown masked until the user
explicitly asks to reveal them.
"""

from __future__ import annotations

import base64
import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from . import ocr as ocr_mod
from .models import (
    CATEGORY_LABELS,
    CATEGORY_SHORT_LABELS,
    DEFAULT_ENABLED,
    Category,
    Detection,
    ScanResult,
    Source,
)
from .redactor import DEFAULT_PADDING_PT, RedactionError, redact_pdf
from .safety import SecureTempDir, get_logger, network_lockdown_active
from .scanner import PdfOpenError, PdfSession

log = get_logger()

ZOOM_CHOICES = ("50%", "75%", "100%", "125%", "150%", "200%")
DEFAULT_ZOOM_INDEX = 2

COLOUR_SELECTED = "#d92b2b"
COLOUR_DESELECTED = "#8a8a8a"
COLOUR_REVIEW = "#e08a00"


class RedactorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} {__version__} - offline PDF redaction")
        self.geometry("1360x860")
        self.minsize(1080, 680)

        self.session: Optional[PdfSession] = None
        self.result: Optional[ScanResult] = None
        self.detections: List[Detection] = []
        self.current_page = 0
        self.reveal_text = tk.BooleanVar(value=False)
        self.use_ocr = tk.BooleanVar(value=False)
        self.ocr_language = tk.StringVar(value="eng")
        self.zoom_var = tk.StringVar(value=ZOOM_CHOICES[DEFAULT_ZOOM_INDEX])
        self.status_var = tk.StringVar(value="Ready. Choose a PDF to begin.")
        self.custom_terms_var = tk.StringVar(value="")
        self.category_vars: Dict[Category, tk.BooleanVar] = {}
        self._photo: Optional[tk.PhotoImage] = None
        self._rect_items: Dict[int, int] = {}   # canvas item id -> detection uid
        self._row_for_uid: Dict[int, str] = {}  # detection uid -> treeview iid
        self._uid_for_row_map: Dict[str, int] = {}  # treeview iid -> detection uid
        self._det_by_uid: Dict[int, Detection] = {}
        self._scan_queue: "queue.Queue" = queue.Queue()
        self._scan_thread: Optional[threading.Thread] = None
        self._cancel_scan = threading.Event()
        self._tempdir = SecureTempDir()
        self._tempdir.__enter__()

        self._build_ui()
        self._set_window_icon()
        self.after(150, self._apply_initial_layout)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _icon_dir(self):
        """Where the icon files live, in a source tree and inside the .exe."""
        if getattr(sys, "frozen", False):
            return Path(getattr(sys, "_MEIPASS", ".")) / "packaging"
        return Path(__file__).resolve().parent.parent / "packaging"

    def _set_window_icon(self) -> None:
        """Title-bar and taskbar icon. Never fatal - the app runs without it."""
        icons = self._icon_dir()
        try:
            ico = icons / "localredact.ico"
            if os.name == "nt" and ico.is_file():
                self.iconbitmap(default=str(ico))
                return
            png = icons / "localredact_256.png"
            if png.is_file():
                self._icon_image = tk.PhotoImage(file=str(png))
                self.iconphoto(True, self._icon_image)
        except Exception:  # pragma: no cover - platform dependent
            log.info("Window icon could not be set; continuing without it.")

    def _apply_initial_layout(self) -> None:
        """Give the preview the space it needs.

        A ttk.Panedwindow places its sashes from the panes' *requested* widths,
        which made the preview the narrowest pane even though it has the largest
        weight, and clipped the review table's last column. Setting the sashes
        explicitly once the window has a real width fixes both; the user can
        still drag them afterwards.
        """
        try:
            total = self._body.winfo_width()
            if total < 400:
                self.after(150, self._apply_initial_layout)
                return
            left = 300
            review = max(452, min(500, int(total * 0.33)))
            self._body.sashpos(0, left)
            self._body.sashpos(1, max(left + 320, total - review))
        except Exception:  # pragma: no cover - geometry not ready
            pass

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        self._build_toolbar()
        body = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        self._body = body
        body.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))
        body.add(self._build_left_panel(body), weight=0)
        body.add(self._build_preview(body), weight=4)
        body.add(self._build_review_panel(body), weight=2)
        self._build_statusbar()

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self, padding=(8, 8))
        bar.pack(fill=tk.X)

        self.btn_open = ttk.Button(
            bar, text="1. Choose PDF  选择 PDF", command=self.on_open
        )
        self.btn_open.pack(side=tk.LEFT)

        self.btn_scan = ttk.Button(
            bar, text="2. Scan for PII  扫描敏感信息",
            command=self.on_scan, state=tk.DISABLED,
        )
        self.btn_scan.pack(side=tk.LEFT, padx=(8, 0))

        self.btn_export = ttk.Button(
            bar, text="3. Export redacted PDF  导出脱敏 PDF",
            command=self.on_export, state=tk.DISABLED,
        )
        self.btn_export.pack(side=tk.LEFT, padx=(8, 0))

        self.file_label = ttk.Label(bar, text="No file selected", foreground="#555")
        self.file_label.pack(side=tk.LEFT, padx=(16, 0))

        # Packed only while a scan runs; an empty trough sitting in the toolbar
        # reads as a broken widget.
        self.progress = ttk.Progressbar(bar, mode="determinate", length=170)

    def _build_left_panel(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(0, 4, 8, 4))

        box = ttk.Labelframe(frame, text="Detect  检测类别", padding=8)
        box.pack(fill=tk.X)
        for category in Category:
            var = tk.BooleanVar(value=category in DEFAULT_ENABLED)
            self.category_vars[category] = var
            ttk.Checkbutton(
                box, text=CATEGORY_LABELS[category], variable=var
            ).pack(anchor=tk.W)

        custom = ttk.Labelframe(frame, text="Also redact  自定义词", padding=8)
        custom.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(
            custom, text="Comma separated, e.g. your name", foreground="#555"
        ).pack(anchor=tk.W)
        ttk.Entry(custom, textvariable=self.custom_terms_var, width=28).pack(
            fill=tk.X, pady=(4, 0)
        )

        ocr_box = ttk.Labelframe(frame, text="Scanned pages  扫描件", padding=8)
        ocr_box.pack(fill=tk.X, pady=(10, 0))
        engines = ocr_mod.available_engines()
        self.ocr_check = ttk.Checkbutton(
            ocr_box,
            text="Use local OCR (offline)",
            variable=self.use_ocr,
            state=tk.NORMAL if engines else tk.DISABLED,
        )
        self.ocr_check.pack(anchor=tk.W)
        ttk.Label(
            ocr_box,
            text=ocr_mod.ocr_status_text(),
            foreground="#2c6e2c" if engines else "#a33",
            wraplength=210,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(4, 0))
        if engines:
            lang = ttk.Frame(ocr_box)
            lang.pack(fill=tk.X, pady=(6, 0))
            ttk.Label(lang, text="Languages:").pack(side=tk.LEFT)
            ttk.Entry(lang, textvariable=self.ocr_language, width=12).pack(
                side=tk.LEFT, padx=(4, 0)
            )
            ttk.Label(
                ocr_box,
                text="Tesseract codes, e.g. eng+chi_sim",
                foreground="#555",
                wraplength=210,
            ).pack(anchor=tk.W)

        privacy = ttk.Labelframe(frame, text="Privacy  隐私", padding=8)
        privacy.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(
            privacy,
            text=(
                "Network: BLOCKED\n"
                "Nothing leaves this computer.\n"
                "No cloud API, no telemetry.\n"
                "Detected values are never logged."
            ),
            foreground="#2c6e2c",
            justify=tk.LEFT,
        ).pack(anchor=tk.W)
        return frame

    def _build_preview(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent)

        nav = ttk.Frame(frame)
        nav.pack(fill=tk.X, pady=(0, 4))
        self.btn_prev = ttk.Button(nav, text="<", width=3, command=self.on_prev_page)
        self.btn_prev.pack(side=tk.LEFT)
        self.page_label = ttk.Label(nav, text="- / -", width=12, anchor=tk.CENTER)
        self.page_label.pack(side=tk.LEFT, padx=4)
        self.btn_next = ttk.Button(nav, text=">", width=3, command=self.on_next_page)
        self.btn_next.pack(side=tk.LEFT)
        ttk.Label(nav, text="Zoom").pack(side=tk.LEFT, padx=(16, 4))
        zoom = ttk.Combobox(
            nav, textvariable=self.zoom_var, values=ZOOM_CHOICES,
            width=6, state="readonly",
        )
        zoom.pack(side=tk.LEFT)
        zoom.bind("<<ComboboxSelected>>", lambda _e: self.refresh_preview())
        ttk.Label(nav, text="Click a box to toggle", foreground="#555").pack(
            side=tk.RIGHT
        )

        wrap = ttk.Frame(frame, relief=tk.SUNKEN, borderwidth=1)
        wrap.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(wrap, background="#4a4a4a", highlightthickness=0)
        vbar = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self.canvas.yview)
        hbar = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
        self.canvas.bind("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
        return frame

    def _build_review_panel(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(8, 0, 0, 0))

        head = ttk.Frame(frame)
        head.pack(fill=tk.X)
        ttk.Label(head, text="Review  预览 / 勾选", font=("", 10, "bold")).pack(
            side=tk.LEFT
        )
        ttk.Checkbutton(
            head, text="Show full values", variable=self.reveal_text,
            command=self.refresh_table,
        ).pack(side=tk.RIGHT)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(6, 6))
        ttk.Button(buttons, text="All", width=6,
                   command=lambda: self.set_all(True)).pack(side=tk.LEFT)
        ttk.Button(buttons, text="None", width=6,
                   command=lambda: self.set_all(False)).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="High confidence only", command=self.select_high).pack(
            side=tk.LEFT
        )

        columns = ("sel", "page", "type", "value", "conf", "src")
        self.tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="extended"
        )
        headings = {
            "sel": ("[x]", 30), "page": ("Pg", 30), "type": ("Type", 92),
            "value": ("Value", 176), "conf": ("Conf", 46), "src": ("Source", 60),
        }
        for key, (title, width) in headings.items():
            self.tree.heading(key, text=title)
            self.tree.column(
                key,
                width=width,
                minwidth=110 if key == "value" else width,
                anchor=tk.W,
                stretch=(key == "value"),
            )
        tbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tbar.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.tag_configure("review", foreground=COLOUR_REVIEW)
        self.tree.tag_configure("off", foreground="#999")
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<space>", self.on_tree_space)
        return frame

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self, relief=tk.SUNKEN, padding=(8, 3))
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(bar, textvariable=self.status_var).pack(side=tk.LEFT)
        net = "Network: BLOCKED" if network_lockdown_active() else "Network: not locked"
        ttk.Label(
            bar, text=net,
            foreground="#2c6e2c" if network_lockdown_active() else "#a33",
        ).pack(side=tk.RIGHT)

    # ------------------------------------------------------------- actions
    def on_open(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a PDF", filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")]
        )
        if not path:
            return
        self._close_session()
        try:
            self.session = PdfSession(path)
        except PdfOpenError as exc:
            messagebox.showerror("Cannot open PDF", str(exc))
            return
        self.result = None
        self.detections = []
        self.current_page = 0
        self.file_label.config(text=Path(path).name)
        self.btn_scan.config(state=tk.NORMAL)
        self.btn_export.config(state=tk.DISABLED)
        self.refresh_table()
        self.refresh_preview()
        meta = self.session.source_metadata()
        extra = f"  Metadata present ({len(meta)} field(s)) - it will be removed." if meta else ""
        self.set_status(f"Loaded {self.session.page_count} page(s).{extra}")

    def on_scan(self) -> None:
        if self.session is None or self._scan_thread is not None:
            return
        categories = {c for c, v in self.category_vars.items() if v.get()}
        if not categories:
            messagebox.showwarning("Nothing to detect", "Select at least one category.")
            return
        terms = [t.strip() for t in self.custom_terms_var.get().split(",") if t.strip()]
        self._cancel_scan.clear()
        self.btn_scan.config(state=tk.DISABLED)
        self.btn_open.config(state=tk.DISABLED)
        self.btn_export.config(state=tk.DISABLED)
        self.progress.config(value=0, maximum=max(1, self.session.page_count))
        self.progress.pack(side=tk.RIGHT)
        self.set_status("Scanning...")

        session = self.session
        use_ocr = self.use_ocr.get()
        languages = self.ocr_language.get().strip() or "eng"

        def worker() -> None:
            try:
                result = session.scan(
                    categories=categories,
                    custom_terms=terms,
                    use_ocr=use_ocr,
                    ocr_languages=languages,
                    progress=lambda i, total, msg: self._scan_queue.put(
                        ("progress", (i, total, msg))
                    ),
                    cancelled=self._cancel_scan.is_set,
                )
                self._scan_queue.put(("done", result))
            except Exception as exc:  # pragma: no cover - surfaced in the UI
                log.error("Scan failed: %s", exc.__class__.__name__)
                self._scan_queue.put(("error", traceback.format_exc(limit=3)))

        self._scan_thread = threading.Thread(target=worker, daemon=True)
        self._scan_thread.start()
        self.after(60, self._poll_scan)

    def _poll_scan(self) -> None:
        try:
            while True:
                kind, payload = self._scan_queue.get_nowait()
                if kind == "progress":
                    index, total, message = payload
                    self.progress.config(value=index, maximum=max(1, total))
                    self.set_status(message)
                elif kind == "done":
                    self._scan_finished(payload)
                    return
                elif kind == "error":
                    self._scan_thread = None
                    self.progress.pack_forget()
                    self.btn_scan.config(state=tk.NORMAL)
                    self.btn_open.config(state=tk.NORMAL)
                    messagebox.showerror("Scan failed", str(payload))
                    self.set_status("Scan failed.")
                    return
        except queue.Empty:
            pass
        self.after(60, self._poll_scan)

    def _scan_finished(self, result: ScanResult) -> None:
        self._scan_thread = None
        self.result = result
        self.detections = list(result.detections)
        self.btn_scan.config(state=tk.NORMAL)
        self.btn_open.config(state=tk.NORMAL)
        self.btn_export.config(
            state=tk.NORMAL if self.detections else tk.DISABLED
        )
        self.progress.pack_forget()
        self.refresh_table()
        self.refresh_preview()
        review = sum(1 for d in self.detections if d.needs_review)
        self.set_status(
            f"{len(self.detections)} candidate(s); {review} need review. "
            "Untick anything that should stay."
        )
        if result.notes:
            messagebox.showinfo("Scan notes", "\n\n".join(result.notes))

    def on_export(self) -> None:
        if self.session is None or not self.detections:
            return
        chosen = [d for d in self.detections if d.selected]
        if not chosen:
            messagebox.showwarning("Nothing selected", "Tick at least one item.")
            return
        default = self.session.path.with_name(self.session.path.stem + "_redacted.pdf")
        out = filedialog.asksaveasfilename(
            title="Save redacted PDF", defaultextension=".pdf",
            initialfile=default.name, initialdir=str(default.parent),
            filetypes=[("PDF files", "*.pdf")],
        )
        if not out:
            return
        self.set_status("Redacting...")
        self.update_idletasks()
        try:
            report = redact_pdf(
                self.session.path, out, self.detections, padding=DEFAULT_PADDING_PT
            )
        except RedactionError as exc:
            messagebox.showerror("Export failed", str(exc))
            self.set_status("Export failed.")
            return
        except Exception as exc:  # pragma: no cover - surfaced in the UI
            log.error("Export failed: %s", exc.__class__.__name__)
            messagebox.showerror("Export failed", f"{exc.__class__.__name__}: {exc}")
            self.set_status("Export failed.")
            return

        skipped = len(self.detections) - len(chosen)
        lines = [
            f"Saved: {report.output_path}",
            "",
            f"Redacted areas: {report.applied} on {report.pages_touched} page(s)",
            f"Items excluded by you: {skipped}",
            f"Annotations / form fields removed: {report.annotations_removed}",
            f"Embedded files removed: {report.embedded_files_removed}",
            "Metadata and XMP: cleared",
            "",
        ]
        if report.verification_passed:
            lines.append(
                "VERIFIED: the output was reopened and no text could be "
                "extracted from any redacted area. The content was removed, "
                "not merely covered."
            )
        else:
            lines.append("WARNING - verification found problems:")
            lines.extend(f"  - {f}" for f in report.verification_failures[:10])
            lines.append("")
            lines.append("Do not share this file until it has been checked manually.")
        if self.result and self.result.pages_without_text:
            missed = [
                p + 1 for p in self.result.pages_without_text
                if p not in (self.result.ocr_pages or [])
            ]
            if missed:
                lines.append("")
                lines.append(
                    "NOTE: page(s) "
                    + ", ".join(str(p) for p in missed[:15])
                    + " had no readable text and were not scanned for PII."
                )
        message = "\n".join(lines)
        if report.verification_passed:
            messagebox.showinfo("Export complete", message)
            self.set_status(f"Exported and verified: {Path(out).name}")
        else:
            messagebox.showwarning("Export completed with warnings", message)
            self.set_status("Exported, but verification failed - check manually.")

    # ------------------------------------------------------------ preview
    def refresh_preview(self) -> None:
        self.canvas.delete("all")
        self._rect_items.clear()
        if self.session is None:
            self.page_label.config(text="- / -")
            return
        total = self.session.page_count
        self.current_page = max(0, min(self.current_page, total - 1))
        self.page_label.config(text=f"{self.current_page + 1} / {total}")
        zoom = int(self.zoom_var.get().rstrip("%")) / 100.0

        try:
            png = self.session.render(self.current_page, zoom=zoom)
        except Exception as exc:  # pragma: no cover - broken page
            self.canvas.create_text(
                20, 20, anchor=tk.NW, fill="white",
                text=f"Could not render this page: {exc.__class__.__name__}",
            )
            return
        self._photo = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        self.canvas.config(
            scrollregion=(0, 0, self._photo.width(), self._photo.height())
        )

        for det in self.detections:
            if det.page != self.current_page:
                continue
            outline = COLOUR_SELECTED if det.selected else COLOUR_DESELECTED
            if det.selected and det.needs_review:
                outline = COLOUR_REVIEW
            for rect in det.rects:
                item = self.canvas.create_rectangle(
                    rect[0] * zoom, rect[1] * zoom, rect[2] * zoom, rect[3] * zoom,
                    outline=outline, width=2,
                    fill=outline if det.selected else "",
                    stipple="gray25" if det.selected else "",
                )
                self._rect_items[item] = det.uid

    def on_canvas_click(self, event) -> None:
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x - 1, y - 1, x + 1, y + 1)):
            uid = self._rect_items.get(item)
            if uid is None:
                continue
            det = self._detection(uid)
            if det is not None:
                det.selected = not det.selected
                self.refresh_table()
                self.refresh_preview()
                self._focus_row(uid)
            return

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(-1 * (event.delta // 120), "units")

    def on_prev_page(self) -> None:
        if self.session and self.current_page > 0:
            self.current_page -= 1
            self.refresh_preview()

    def on_next_page(self) -> None:
        if self.session and self.current_page < self.session.page_count - 1:
            self.current_page += 1
            self.refresh_preview()

    # -------------------------------------------------------------- table
    def refresh_table(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._row_for_uid.clear()
        self._uid_for_row_map.clear()
        self._det_by_uid = {d.uid: d for d in self.detections}
        reveal = self.reveal_text.get()
        for det in self.detections:
            tags = []
            if det.needs_review:
                tags.append("review")
            if not det.selected:
                tags.append("off")
            source = "OCR" if det.source is Source.OCR else "text"
            if det.source is Source.OCR and det.ocr_confidence is not None:
                source = f"OCR {det.ocr_confidence:.0%}"
            conf = f"{det.confidence:.0%}"
            if det.needs_review:
                conf += "!"
            row = self.tree.insert(
                "", tk.END,
                values=(
                    "[x]" if det.selected else "[ ]",
                    det.page + 1,
                    CATEGORY_SHORT_LABELS[det.category],
                    det.preview(reveal),
                    conf,
                    source,
                ),
                tags=tuple(tags),
            )
            self._row_for_uid[det.uid] = row
            self._uid_for_row_map[row] = det.uid
        selected = sum(1 for d in self.detections if d.selected)
        if self.detections:
            self.btn_export.config(state=tk.NORMAL if selected else tk.DISABLED)

    def _uid_for_row(self, row: str) -> Optional[int]:
        return self._uid_for_row_map.get(row)

    def _detection(self, uid: int) -> Optional[Detection]:
        return self._det_by_uid.get(uid)

    def on_tree_click(self, event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        row = self.tree.identify_row(event.y)
        uid = self._uid_for_row(row) if row else None
        if uid is None:
            return
        det = self._detection(uid)
        if det is not None:
            det.selected = not det.selected
            self.refresh_table()
            self.refresh_preview()
            self._focus_row(uid)

    def on_tree_space(self, _event) -> str:
        for row in self.tree.selection():
            uid = self._uid_for_row(row)
            det = self._detection(uid) if uid is not None else None
            if det is not None:
                det.selected = not det.selected
        self.refresh_table()
        self.refresh_preview()
        return "break"

    def on_tree_select(self, _event) -> None:
        rows = self.tree.selection()
        if not rows:
            return
        uid = self._uid_for_row(rows[0])
        det = self._detection(uid) if uid is not None else None
        if det is not None and det.page != self.current_page:
            self.current_page = det.page
            self.refresh_preview()

    def _focus_row(self, uid: int) -> None:
        row = self._row_for_uid.get(uid)
        if row:
            self.tree.selection_set(row)
            self.tree.see(row)

    def set_all(self, value: bool) -> None:
        for det in self.detections:
            det.selected = value
        self.refresh_table()
        self.refresh_preview()

    def select_high(self) -> None:
        for det in self.detections:
            det.selected = not det.needs_review
        self.refresh_table()
        self.refresh_preview()

    # ------------------------------------------------------------ plumbing
    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _close_session(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def _on_close(self) -> None:
        self._cancel_scan.set()
        self._close_session()
        self.detections = []
        self.result = None
        self._photo = None
        self._tempdir.cleanup()
        self.destroy()


def run() -> None:
    app = RedactorApp()
    app.mainloop()
