"""Tkinter desktop GUI for confluence-exporter.

This is an *adapter* on top of the same domain services the CLI uses
(``SpaceExporter``, ``OutputConverter``, ``PDFMerger``). It runs every
long-running action on a worker thread so the window stays responsive,
and streams log lines + progress updates back to the UI through a
thread-safe queue.

Launch points:

    confluence-exporter-gui        # entry point script installed by pip
    cfx-gui                        # short alias
    python -m confluence_exporter.gui

Tkinter is part of the Python standard library, so the GUI adds **no**
runtime dependency. On Linux some distros split it into a separate
package (``python3-tk``); on Windows and macOS it ships with Python.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from confluence_exporter import __version__
from confluence_exporter.auth import parse_cookie_header
from confluence_exporter.client import ConfluenceClient, ConfluenceError
from confluence_exporter.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    load_config,
    save_config,
)
from confluence_exporter.converter import OutputConverter
from confluence_exporter.exporter import SpaceExporter
from confluence_exporter.merger import PDFMerger
from confluence_exporter.pdf_engines import detect_engine, engine_names

# ---------------------------------------------------------------------------
# Thread <-> UI bridge
# ---------------------------------------------------------------------------


class UIQueue:
    """Thread-safe fan-in for log lines and progress events."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, object]] = queue.Queue()

    def log(self, text: str) -> None:
        self._q.put(("log", text))

    def progress(self, current: int, total: int, label: str) -> None:
        self._q.put(("progress", (current, total, label)))

    def done(self, summary: str) -> None:
        self._q.put(("done", summary))

    def fail(self, err: str) -> None:
        self._q.put(("fail", err))

    def drain(self):
        while True:
            try:
                yield self._q.get_nowait()
            except queue.Empty:
                return


class _QueueLogHandler(logging.Handler):
    """Pipe logging records into the UIQueue so we can show them live."""

    def __init__(self, uiq: UIQueue) -> None:
        super().__init__()
        self._uiq = uiq
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover
            self._uiq.log(self.format(record))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_folder(entry: ttk.Entry, title: str) -> None:
    initial = entry.get().strip() or str(Path.cwd())
    folder = filedialog.askdirectory(initialdir=initial, title=title)
    if folder:
        entry.delete(0, tk.END)
        entry.insert(0, folder)


def _pick_file(entry: ttk.Entry, title: str, filetypes) -> None:
    initial = entry.get().strip() or str(Path.cwd())
    path = filedialog.askopenfilename(initialdir=initial, title=title, filetypes=filetypes)
    if path:
        entry.delete(0, tk.END)
        entry.insert(0, path)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class App(tk.Tk):
    """Root Tk window holding the notebook and the log pane."""

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.title(f"Confluence Exporter  v{__version__}")
        self.geometry("980x720")
        self.minsize(820, 600)

        self._config_path = config_path
        self._uiq = UIQueue()
        self._worker: threading.Thread | None = None

        # Load (or create) the config
        try:
            self._cfg = load_config(config_path)
        except Exception:
            self._cfg = AppConfig()

        # Install the log handler so the library's logger streams into our UI
        root_logger = logging.getLogger("confluence_exporter")
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(_QueueLogHandler(self._uiq))
        # Also forward root logger
        logging.getLogger().addHandler(_QueueLogHandler(self._uiq))

        self._build_ui()
        # Poll the queue at 20 Hz for log/progress updates
        self.after(50, self._poll_queue)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        # ----- Top bar: config file + save/load -----
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill=tk.X, side=tk.TOP)
        ttk.Label(top, text="Config file:").pack(side=tk.LEFT)
        self._config_entry = ttk.Entry(top)
        self._config_entry.insert(0, str(self._config_path))
        self._config_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(
            top, text="Browse…",
            command=lambda: _pick_file(
                self._config_entry,
                "Select config file",
                [("JSON", "*.json"), ("All files", "*.*")],
            ),
        ).pack(side=tk.LEFT)
        ttk.Button(top, text="Load", command=self._load_config).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="Save", command=self._save_config).pack(side=tk.LEFT, padx=(6, 0))

        # ----- Notebook (tabs) -----
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 4))

        self._tab_auth = AuthTab(self._nb, self)
        self._tab_export = ExportTab(self._nb, self)
        self._tab_convert = ConvertTab(self._nb, self)
        self._tab_merge = MergeTab(self._nb, self)
        self._tab_diag = DiagnoseTab(self._nb, self)

        self._nb.add(self._tab_auth, text="  1. Connection  ")
        self._nb.add(self._tab_export, text="  2. Export  ")
        self._nb.add(self._tab_convert, text="  3. Convert  ")
        self._nb.add(self._tab_merge, text="  4. Merge  ")
        self._nb.add(self._tab_diag, text="  Diagnose  ")

        # ----- Progress bar -----
        prog = ttk.Frame(self, padding=(10, 4))
        prog.pack(fill=tk.X)
        self._progress_label = ttk.Label(prog, text="Idle")
        self._progress_label.pack(side=tk.LEFT)
        self._progress = ttk.Progressbar(prog, mode="determinate")
        self._progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        # ----- Log pane -----
        logframe = ttk.Labelframe(self, text="Log", padding=(6, 4))
        logframe.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self._log = scrolledtext.ScrolledText(
            logframe, height=10, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9) if sys.platform == "win32" else ("Menlo", 10),
        )
        self._log.pack(fill=tk.BOTH, expand=True)

        # Clear log button
        clear = ttk.Frame(self, padding=(10, 0, 10, 10))
        clear.pack(fill=tk.X)
        ttk.Button(clear, text="Clear log", command=self._clear_log).pack(side=tk.RIGHT)

    # ------------------------------------------------------------------
    # Config I/O
    # ------------------------------------------------------------------
    def _load_config(self) -> None:
        path = Path(self._config_entry.get().strip())
        try:
            self._cfg = load_config(path) if path.exists() else AppConfig()
            self._config_path = path
            self._refresh_tabs_from_config()
            self._log_line(f"Loaded config from {path}")
        except Exception as e:
            messagebox.showerror("Load failed", str(e))

    def _save_config(self) -> None:
        self._collect_config_from_tabs()
        path = Path(self._config_entry.get().strip())
        try:
            save_config(self._cfg, path)
            self._config_path = path
            self._log_line(f"Saved config to {path}")
            messagebox.showinfo("Saved", f"Config saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def _refresh_tabs_from_config(self) -> None:
        self._tab_auth.refresh_from(self._cfg)
        self._tab_export.refresh_from(self._cfg)
        self._tab_convert.refresh_from(self._cfg)
        self._tab_merge.refresh_from(self._cfg)

    def _collect_config_from_tabs(self) -> None:
        self._tab_auth.write_into(self._cfg)
        self._tab_export.write_into(self._cfg)
        self._tab_convert.write_into(self._cfg)
        self._tab_merge.write_into(self._cfg)

    @property
    def cfg(self) -> AppConfig:
        return self._cfg

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------
    def run_worker(self, label: str, target) -> None:
        """Run ``target()`` in a thread. Disables action buttons while running."""
        if self._worker and self._worker.is_alive():
            messagebox.showwarning("Busy", "A task is already running.")
            return
        self._progress["value"] = 0
        self._progress["maximum"] = 1
        self._progress_label.config(text=label + " …")
        self._log_line(f"▶ {label}")

        def run() -> None:
            try:
                target()
            except ConfluenceError as e:
                self._uiq.fail(str(e))
            except Exception as e:
                self._uiq.fail(f"{e}\n{traceback.format_exc()}")
            else:
                self._uiq.done(label + " finished.")

        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _poll_queue(self) -> None:
        for kind, payload in self._uiq.drain():
            if kind == "log":
                self._log_line(str(payload))
            elif kind == "progress":
                current, total, label = payload  # type: ignore[misc]
                if total > 0:
                    self._progress["maximum"] = total
                    self._progress["value"] = current
                self._progress_label.config(text=f"{label}  ({current}/{total})")
            elif kind == "done":
                self._progress_label.config(text=str(payload))
                self._log_line(f"✓ {payload}")
            elif kind == "fail":
                self._progress_label.config(text="Failed")
                self._log_line(f"✗ {payload}")
                messagebox.showerror("Task failed", str(payload))
        self.after(50, self._poll_queue)

    # ------------------------------------------------------------------
    # Log pane helpers
    # ------------------------------------------------------------------
    def _log_line(self, text: str) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.insert(tk.END, text + "\n")
        self._log.see(tk.END)
        self._log.configure(state=tk.DISABLED)

    def _clear_log(self) -> None:
        self._log.configure(state=tk.NORMAL)
        self._log.delete("1.0", tk.END)
        self._log.configure(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


class _BaseTab(ttk.Frame):
    def __init__(self, master, app: App) -> None:
        super().__init__(master, padding=12)
        self.app = app
        self._build()
        self.refresh_from(app.cfg)

    # Subclasses override these:
    def _build(self) -> None: ...
    def refresh_from(self, cfg: AppConfig) -> None: ...
    def write_into(self, cfg: AppConfig) -> None: ...


class AuthTab(_BaseTab):
    """Confluence target + authentication."""

    def _build(self) -> None:
        grid = ttk.Frame(self)
        grid.pack(fill=tk.X)
        for i in range(2):
            grid.columnconfigure(i, weight=1 if i == 1 else 0)

        ttk.Label(grid, text="Base URL").grid(row=0, column=0, sticky="w", pady=4)
        self.base_url = ttk.Entry(grid)
        self.base_url.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(grid, text="Space key").grid(row=1, column=0, sticky="w", pady=4)
        self.space_key = ttk.Entry(grid)
        self.space_key.grid(row=1, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(grid, text="Auth mode").grid(row=2, column=0, sticky="w", pady=(12, 4))
        self.auth_mode = tk.StringVar(value="api_token")
        modes = ttk.Frame(grid)
        modes.grid(row=2, column=1, sticky="w", padx=(8, 0))
        for text, val in [
            ("API token (Cloud)", "api_token"),
            ("Browser cookie (SSO)", "browser_cookie"),
            ("Personal Access Token (Server/DC)", "pat"),
        ]:
            ttk.Radiobutton(
                modes, text=text, value=val, variable=self.auth_mode,
                command=self._sync_fields,
            ).pack(side=tk.LEFT, padx=(0, 12))

        # API token fields
        self._api_frame = ttk.Labelframe(self, text="API token", padding=10)
        ttk.Label(self._api_frame, text="Email").grid(row=0, column=0, sticky="w", pady=2)
        self.email = ttk.Entry(self._api_frame, width=40)
        self.email.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(self._api_frame, text="API token").grid(row=1, column=0, sticky="w", pady=2)
        self.api_token = ttk.Entry(self._api_frame, show="*", width=40)
        self.api_token.grid(row=1, column=1, sticky="ew", padx=8)
        self._api_frame.columnconfigure(1, weight=1)

        # PAT fields
        self._pat_frame = ttk.Labelframe(self, text="Personal Access Token", padding=10)
        ttk.Label(self._pat_frame, text="PAT").grid(row=0, column=0, sticky="w", pady=2)
        self.pat_token = ttk.Entry(self._pat_frame, show="*", width=60)
        self.pat_token.grid(row=0, column=1, sticky="ew", padx=8)
        self._pat_frame.columnconfigure(1, weight=1)

        # Cookie fields
        self._cookie_frame = ttk.Labelframe(self, text="Browser cookie", padding=10)
        help_txt = (
            "1) Log in to Confluence in your browser.\n"
            "2) Open DevTools (F12) → Network tab → refresh → click any request.\n"
            "3) In Request Headers, copy the full 'Cookie:' value and paste it below.\n"
            "Accepted formats: full 'Cookie:' header, semicolon-separated name=value, "
            "one per line, or a JSON object."
        )
        ttk.Label(self._cookie_frame, text=help_txt, foreground="#555").pack(anchor="w")
        self.cookie_text = scrolledtext.ScrolledText(self._cookie_frame, height=6, wrap=tk.WORD)
        self.cookie_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        # Test connection
        btns = ttk.Frame(self)
        btns.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(btns, text="Test connection", command=self._on_test).pack(side=tk.LEFT)
        ttk.Button(btns, text="Save to config", command=self.app._save_config).pack(side=tk.LEFT, padx=8)

        # Layout the three conditional frames in the same spot
        self._auth_holder = ttk.Frame(self)
        self._auth_holder.pack(fill=tk.BOTH, expand=True, pady=(14, 0))
        self._api_frame.pack(in_=self._auth_holder, fill=tk.X)
        # Others are hidden until selected — we use pack_forget
        self._sync_fields()

    def _sync_fields(self) -> None:
        for f in (self._api_frame, self._pat_frame, self._cookie_frame):
            with contextlib.suppress(Exception):
                f.pack_forget()
        mode = self.auth_mode.get()
        if mode == "api_token":
            self._api_frame.pack(in_=self._auth_holder, fill=tk.X)
        elif mode == "pat":
            self._pat_frame.pack(in_=self._auth_holder, fill=tk.X)
        else:
            self._cookie_frame.pack(in_=self._auth_holder, fill=tk.BOTH, expand=True)

    def refresh_from(self, cfg: AppConfig) -> None:
        self.base_url.delete(0, tk.END)
        self.base_url.insert(0, cfg.confluence.base_url or "")
        self.space_key.delete(0, tk.END)
        self.space_key.insert(0, cfg.confluence.space_key or "")
        self.auth_mode.set(cfg.confluence.auth_mode or "api_token")
        self.email.delete(0, tk.END)
        self.email.insert(0, cfg.confluence.email or "")
        self.api_token.delete(0, tk.END)
        self.api_token.insert(0, cfg.confluence.api_token or "")
        self.pat_token.delete(0, tk.END)
        self.pat_token.insert(0, cfg.confluence.personal_access_token or "")
        self.cookie_text.delete("1.0", tk.END)
        if cfg.confluence.cookies:
            self.cookie_text.insert("1.0", "\n".join(f"{k}={v}" for k, v in cfg.confluence.cookies.items()))
        self._sync_fields()

    def write_into(self, cfg: AppConfig) -> None:
        cfg.confluence.base_url = self.base_url.get().strip().rstrip("/")
        cfg.confluence.space_key = self.space_key.get().strip()
        cfg.confluence.auth_mode = self.auth_mode.get()
        cfg.confluence.email = self.email.get().strip()
        cfg.confluence.api_token = self.api_token.get().strip()
        cfg.confluence.personal_access_token = self.pat_token.get().strip()
        raw = self.cookie_text.get("1.0", tk.END).strip()
        cfg.confluence.cookies = parse_cookie_header(raw) if raw else {}

    def _on_test(self) -> None:
        self.write_into(self.app.cfg)
        errs = self.app.cfg.validate()
        if errs:
            messagebox.showerror("Missing fields", "\n".join(errs))
            return

        def target() -> None:
            client = ConfluenceClient.from_config(self.app.cfg.confluence)
            user = client.test_connection()
            self.app._uiq.log(
                f"Authenticated as {user.get('displayName') or user.get('username') or '?'}"
            )
            sp = client.get_space(self.app.cfg.confluence.space_key)
            self.app._uiq.log(f"Space OK: {sp.get('name')} ({sp.get('key')})")

        self.app.run_worker("Testing connection", target)


class ExportTab(_BaseTab):
    def _build(self) -> None:
        grid = ttk.Frame(self)
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Output format").grid(row=0, column=0, sticky="w", pady=4)
        self.fmt = tk.StringVar(value="pdf")
        ttk.Combobox(
            grid, textvariable=self.fmt,
            values=["pdf", "docx", "md", "html"], state="readonly", width=10,
        ).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(grid, text="Output folder").grid(row=1, column=0, sticky="w", pady=4)
        row = ttk.Frame(grid)
        row.grid(row=1, column=1, sticky="ew", padx=8)
        self.output = ttk.Entry(row)
        self.output.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Browse…",
                   command=lambda: _pick_folder(self.output, "Pick output folder")
                   ).pack(side=tk.LEFT, padx=(6, 0))

        self.include_attachments = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            grid, text="Download attachments", variable=self.include_attachments,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(8, 0))

        self.skip_unchanged = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            grid, text="Skip pages unchanged since last run",
            variable=self.skip_unchanged,
        ).grid(row=3, column=1, sticky="w", padx=8)

        ttk.Button(self, text="▶  Run export", command=self._on_run).pack(pady=(18, 0))

        ttk.Label(
            self,
            text=(
                "Tip: configure connection in tab 1, then come back here.\n"
                "The export reads its credentials from the Connection tab / config file."
            ),
            foreground="#555",
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

    def refresh_from(self, cfg: AppConfig) -> None:
        self.fmt.set(cfg.export.format)
        self.output.delete(0, tk.END)
        self.output.insert(0, cfg.export.output_path)
        self.include_attachments.set(cfg.export.include_attachments)
        self.skip_unchanged.set(cfg.export.skip_unchanged)

    def write_into(self, cfg: AppConfig) -> None:
        cfg.export.format = self.fmt.get()
        cfg.export.output_path = self.output.get().strip()
        cfg.export.include_attachments = bool(self.include_attachments.get())
        cfg.export.skip_unchanged = bool(self.skip_unchanged.get())

    def _on_run(self) -> None:
        self.app._collect_config_from_tabs()
        errs = self.app.cfg.validate()
        if errs:
            messagebox.showerror("Missing fields", "\n".join(errs))
            return

        def target() -> None:
            client = ConfluenceClient.from_config(
                self.app.cfg.confluence,
                request_delay_seconds=self.app.cfg.export.request_delay_seconds,
            )

            def cb(title: str, i: int, total: int) -> None:
                self.app._uiq.progress(i, total, title[:70])

            exporter = SpaceExporter(self.app.cfg, client, progress=cb)
            written, skipped, failed = exporter.run()
            self.app._uiq.log(
                f"Export done — written: {written}, skipped: {skipped}, failed: {failed}"
            )

        self.app.run_worker("Exporting space", target)


class ConvertTab(_BaseTab):
    def _build(self) -> None:
        grid = ttk.Frame(self)
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Source folder (HTML export)").grid(row=0, column=0, sticky="w", pady=4)
        row = ttk.Frame(grid)
        row.grid(row=0, column=1, sticky="ew", padx=8)
        self.src = ttk.Entry(row)
        self.src.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Browse…",
                   command=lambda: _pick_folder(self.src, "Pick source folder")
                   ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(grid, text="Target format").grid(row=1, column=0, sticky="w", pady=4)
        self.fmt = tk.StringVar(value="pdf")
        ttk.Combobox(grid, textvariable=self.fmt,
                     values=["pdf", "docx"], state="readonly", width=10,
                     ).grid(row=1, column=1, sticky="w", padx=8)

        ttk.Label(grid, text="PDF engine").grid(row=2, column=0, sticky="w", pady=4)
        self.engine = tk.StringVar(value="auto")
        ttk.Combobox(
            grid, textvariable=self.engine,
            values=["auto", *engine_names()], state="readonly", width=14,
        ).grid(row=2, column=1, sticky="w", padx=8)

        self.merge_pdf = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            grid, text="Merge PDF attachments into each document as an appendix",
            variable=self.merge_pdf,
        ).grid(row=3, column=1, sticky="w", padx=8, pady=(8, 0))

        ttk.Button(self, text="▶  Run convert", command=self._on_run).pack(pady=(18, 0))

    def refresh_from(self, cfg: AppConfig) -> None:
        self.src.delete(0, tk.END)
        self.src.insert(0, cfg.export.output_path)
        self.engine.set(cfg.convert.engine)

    def write_into(self, cfg: AppConfig) -> None:
        cfg.convert.engine = self.engine.get()

    def _on_run(self) -> None:
        src = Path(self.src.get().strip())
        if not src.exists():
            messagebox.showerror("Source missing", f"Folder does not exist:\n{src}")
            return
        fmt = self.fmt.get()
        engine = self.engine.get()
        merge_flag = bool(self.merge_pdf.get())

        def target() -> None:
            converter = OutputConverter(
                output_root=src,
                target_format=fmt,
                engine=engine,
                merge_pdf_attachments=merge_flag,
            )

            def cb(name: str, i: int, total: int) -> None:
                self.app._uiq.progress(i, total, name[:70])
            converter._progress = cb
            ok_n, fail_n = converter.run()
            self.app._uiq.log(
                f"Convert done — ok: {ok_n}, failed: {fail_n}. "
                f"Output: {converter.converted_root}"
            )

        self.app.run_worker(f"Converting to {fmt.upper()}", target)


class MergeTab(_BaseTab):
    def _build(self) -> None:
        grid = ttk.Frame(self)
        grid.pack(fill=tk.X)
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="Source folder (per-page PDFs)").grid(row=0, column=0, sticky="w", pady=4)
        row = ttk.Frame(grid)
        row.grid(row=0, column=1, sticky="ew", padx=8)
        self.src = ttk.Entry(row)
        self.src.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="Browse…",
                   command=lambda: _pick_folder(self.src, "Pick source folder")
                   ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(grid, text="Destination folder").grid(row=1, column=0, sticky="w", pady=4)
        row2 = ttk.Frame(grid)
        row2.grid(row=1, column=1, sticky="ew", padx=8)
        self.dst = ttk.Entry(row2)
        self.dst.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row2, text="Browse…",
                   command=lambda: _pick_folder(self.dst, "Pick destination folder")
                   ).pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(grid, text="Merge mode").grid(row=2, column=0, sticky="w", pady=(10, 4))
        self.mode = tk.StringVar(value="per_section")
        frm = ttk.Frame(grid)
        frm.grid(row=2, column=1, sticky="w", padx=8)
        for label, val in [
            ("per_section (NotebookLM-friendly)", "per_section"),
            ("per_space", "per_space"),
            ("single (one PDF)", "single"),
        ]:
            ttk.Radiobutton(frm, text=label, value=val, variable=self.mode).pack(anchor="w")

        ttk.Label(grid, text="TOC engine").grid(row=3, column=0, sticky="w", pady=4)
        self.engine = tk.StringVar(value="auto")
        ttk.Combobox(
            grid, textvariable=self.engine,
            values=["auto", *engine_names()], state="readonly", width=14,
        ).grid(row=3, column=1, sticky="w", padx=8)

        ttk.Button(self, text="▶  Run merge", command=self._on_run).pack(pady=(18, 0))

    def refresh_from(self, cfg: AppConfig) -> None:
        self.src.delete(0, tk.END)
        self.src.insert(0, cfg.export.output_path.rstrip("/\\") + "_converted")
        self.dst.delete(0, tk.END)
        self.dst.insert(0, cfg.merge.destination or (cfg.export.output_path.rstrip("/\\") + "_volumes"))
        self.mode.set(cfg.merge.mode or "per_section")

    def write_into(self, cfg: AppConfig) -> None:
        cfg.merge.mode = self.mode.get()
        cfg.merge.destination = self.dst.get().strip()

    def _on_run(self) -> None:
        src = Path(self.src.get().strip())
        dst = Path(self.dst.get().strip())
        if not src.exists():
            messagebox.showerror("Source missing", f"Folder does not exist:\n{src}")
            return
        mode = self.mode.get()
        engine = self.engine.get()

        def target() -> None:
            merger = PDFMerger(source_root=src, dest_root=dst, mode=mode, engine=engine)
            ok_n, fail_n = merger.run()
            self.app._uiq.log(f"Merge done — volumes: {ok_n}, failed: {fail_n}. Output: {dst}")

        self.app.run_worker(f"Merging ({mode})", target)


class DiagnoseTab(_BaseTab):
    def _build(self) -> None:
        self.text = scrolledtext.ScrolledText(self, height=20, wrap=tk.WORD, state=tk.DISABLED)
        self.text.pack(fill=tk.BOTH, expand=True)
        ttk.Button(self, text="Run diagnostics", command=self._on_run).pack(pady=(10, 0))

    def _on_run(self) -> None:
        from confluence_exporter.pdf_engines import _ENGINES

        lines: list[str] = []
        lines.append("PDF engines:")
        for name, engine in _ENGINES.items():
            if engine.is_available():
                lines.append(f"  ✓  {name}  — available")
            else:
                lines.append(f"  ✗  {name}  — missing: {engine.explain_unavailable()}")
        best = detect_engine("auto")
        lines.append("")
        if best == "none":
            lines.append("⚠  No usable PDF engine. Install playwright (recommended) or weasyprint.")
        else:
            lines.append(f"Best available engine: {best}")

        self.text.configure(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", "\n".join(lines))
        self.text.configure(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point used by the ``confluence-exporter-gui`` console script."""
    config_path = Path(DEFAULT_CONFIG_PATH)
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])
    app = App(config_path)
    app.mainloop()


if __name__ == "__main__":  # pragma: no cover
    main()
