"""RawPrint - preview and print documents straight to a raw 9100 port.

Exists because every job routed through the Windows print spooler wedges in
"Printing, Retained" on the shul's Xerox WorkCentre 3215, while raw TCP to
port 9100 prints reliably. This app never calls a Windows print API.
"""

import json
import os
import queue
import sys
import threading
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import raster

APP_NAME = "RawPrint"
DEFAULTS = {
    "host": "192.168.9.70",
    "port": 9100,
    "copies": 1,
    "x_offset": 0.0,
    "y_offset": 0.0,
    "dither": True,
    "dpi": 300,
    "paper": "Letter",
    "landscape": False,
    "last_dir": "",
}

BG = "#0f172a"
CARD = "#1e293b"
FG = "#e2e8f0"
MUTED = "#94a3b8"
ACCENT = "#38bdf8"


def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k in DEFAULTS:
            if k in saved:
                s[k] = saved[k]
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load_settings()
        self.doc = None
        self.page_index = 0
        self.preview_cache = {}
        self.current_bw = None
        self._last_cov = (0, 0)
        self._photo = None
        self._render_token = 0
        self.events = queue.Queue()

        root.title(f"{APP_NAME} - raw port 9100 printing")
        root.minsize(940, 620)
        root.configure(bg=BG)
        # Size to the screen so the whole sidebar fits without scrolling where
        # there is room; the sidebar scrolls as a fallback on short displays.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1220, sw - 80), min(920, sh - 70)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")

        self._build_style()
        self._build_ui()
        self._poll_events()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- chrome ----------

    def _build_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=FG, fieldbackground=CARD)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=CARD, foreground=FG)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        st.configure("Head.TLabel", background=CARD, foreground=ACCENT,
                     font=("Segoe UI", 9, "bold"))
        st.configure("TButton", background="#334155", foreground=FG,
                     borderwidth=0, padding=6, focuscolor=CARD)
        st.map("TButton", background=[("active", "#475569"), ("disabled", "#1e293b")],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background="#0284c7", foreground="#ffffff",
                     padding=8, font=("Segoe UI", 10, "bold"))
        st.map("Accent.TButton", background=[("active", "#0369a1"), ("disabled", "#334155")])
        st.configure("TCheckbutton", background=CARD, foreground=FG)
        st.map("TCheckbutton", background=[("active", CARD)])
        st.configure("TEntry", fieldbackground="#0b1220", foreground=FG,
                     insertcolor=FG, borderwidth=1)
        st.configure("TSpinbox", fieldbackground="#0b1220", foreground=FG,
                     insertcolor=FG, arrowsize=12)
        st.configure("TCombobox", fieldbackground="#0b1220", foreground=FG,
                     background="#334155", arrowsize=12, selectbackground="#0b1220",
                     selectforeground=FG)
        # Readonly comboboxes otherwise render dark text on the dark field.
        st.map("TCombobox",
               fieldbackground=[("readonly", "#0b1220"), ("disabled", CARD)],
               foreground=[("readonly", FG), ("disabled", MUTED)],
               selectbackground=[("readonly", "#0b1220")],
               selectforeground=[("readonly", FG)],
               background=[("readonly", "#334155"), ("active", "#475569")])
        self.root.option_add("*TCombobox*Listbox.background", "#0b1220")
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#0284c7")
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        st.configure("TScrollbar", background="#334155", troughcolor=CARD,
                     borderwidth=0, arrowcolor=MUTED)
        st.map("TScrollbar", background=[("active", "#475569")])
        st.configure("TProgressbar", background=ACCENT, troughcolor="#0b1220",
                     borderwidth=0)
        st.configure("TLabelframe", background=CARD, foreground=ACCENT)
        st.configure("TLabelframe.Label", background=CARD, foreground=ACCENT,
                     font=("Segoe UI", 9, "bold"))

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        # The sidebar scrolls: on a short screen the Print button must never
        # be clipped off the bottom.
        side = tk.Frame(outer, bg=CARD, width=348)
        side.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        side.grid_propagate(False)
        side.rowconfigure(0, weight=1)
        side.columnconfigure(0, weight=1)

        scan = tk.Canvas(side, bg=CARD, highlightthickness=0, bd=0)
        scan.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(side, orient="vertical", command=scan.yview)
        vs.grid(row=0, column=1, sticky="ns")
        scan.configure(yscrollcommand=vs.set)

        inner = ttk.Frame(scan, style="Card.TFrame", padding=12)
        win = scan.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: scan.configure(scrollregion=scan.bbox("all")))
        scan.bind("<Configure>", lambda e: scan.itemconfigure(win, width=e.width))

        def wheel(e):
            scan.yview_scroll(-1 * (e.delta // 120), "units")
        for w in (scan, inner):
            w.bind("<MouseWheel>", wheel)
        self._sidebar_canvas = scan
        self._build_sidebar(inner)
        for child in self._walk(inner):
            child.bind("<MouseWheel>", wheel)

        # Copies + Print stay pinned below the scroll area so they are always
        # reachable, however short the display is.
        pinned = ttk.Frame(side, style="Card.TFrame", padding=(12, 8, 12, 12))
        pinned.grid(row=1, column=0, columnspan=2, sticky="ew")
        self._build_print_box(pinned)

        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self._build_preview(right)

        bar = ttk.Frame(outer, style="Card.TFrame", padding=(10, 8))
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        bar.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Ready. Open a PDF or image to begin.")
        ttk.Label(bar, textvariable=self.status_var, style="Card.TLabel").grid(
            row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(bar, length=220, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="e", padx=(10, 0))

    def _walk(self, w):
        for c in w.winfo_children():
            yield c
            for g in self._walk(c):
                yield g

    def _build_sidebar(self, p):
        r = 0
        ttk.Label(p, text="RawPrint", style="Head.TLabel",
                  font=("Segoe UI", 15, "bold")).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(p, text="Bypasses the Windows spooler entirely.",
                  style="Muted.TLabel", wraplength=300).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=(0, 10))
        r += 1

        ttk.Button(p, text="Open file...", command=self.open_file).grid(
            row=r, column=0, columnspan=2, sticky="ew")
        r += 1
        self.file_var = tk.StringVar(value="No file loaded")
        ttk.Label(p, textvariable=self.file_var, style="Muted.TLabel",
                  wraplength=300).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 12))
        r += 1

        # --- output settings ---
        box = ttk.Labelframe(p, text=" Output ", padding=8)
        box.grid(row=r, column=0, columnspan=2, sticky="ew")
        box.columnconfigure(1, weight=1)
        r += 1
        br = 0

        ttk.Label(box, text="Paper", style="Card.TLabel").grid(row=br, column=0, sticky="w", pady=3)
        self.paper_var = tk.StringVar(value=self.cfg["paper"])
        cb = ttk.Combobox(box, textvariable=self.paper_var, state="readonly",
                          values=list(raster.PAPERS.keys()), width=12)
        cb.grid(row=br, column=1, sticky="ew", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview())
        br += 1

        ttk.Label(box, text="Orientation", style="Card.TLabel").grid(row=br, column=0, sticky="w", pady=3)
        self.orient_var = tk.StringVar(value="Landscape" if self.cfg["landscape"] else "Portrait")
        cb = ttk.Combobox(box, textvariable=self.orient_var, state="readonly",
                          values=["Portrait", "Landscape"], width=12)
        cb.grid(row=br, column=1, sticky="ew", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview())
        br += 1

        ttk.Label(box, text="Resolution", style="Card.TLabel").grid(row=br, column=0, sticky="w", pady=3)
        self.dpi_var = tk.StringVar(value=str(self.cfg["dpi"]))
        cb = ttk.Combobox(box, textvariable=self.dpi_var, state="readonly",
                          values=["300", "600"], width=12)
        cb.grid(row=br, column=1, sticky="ew", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview())
        br += 1

        self.dither_var = tk.BooleanVar(value=bool(self.cfg["dither"]))
        ttk.Checkbutton(box, text="Floyd-Steinberg dither", variable=self.dither_var,
                        command=self.refresh_preview).grid(
            row=br, column=0, columnspan=2, sticky="w", pady=(6, 0))
        br += 1
        ttk.Label(box, text="Off = hard threshold; background tints vanish.",
                  style="Muted.TLabel", wraplength=280).grid(
            row=br, column=0, columnspan=2, sticky="w")

        # --- registration ---
        box = ttk.Labelframe(p, text=" Registration ", padding=8)
        box.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        box.columnconfigure(1, weight=1)
        r += 1

        ttk.Label(box, text="Horizontal in", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=3)
        self.xoff_var = tk.StringVar(value=str(self.cfg["x_offset"]))
        ttk.Spinbox(box, from_=-2.0, to=2.0, increment=0.05, width=10,
                    textvariable=self.xoff_var, command=self.refresh_preview).grid(
            row=0, column=1, sticky="ew", pady=3)

        ttk.Label(box, text="Vertical in", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.yoff_var = tk.StringVar(value=str(self.cfg["y_offset"]))
        ttk.Spinbox(box, from_=-2.0, to=2.0, increment=0.05, width=10,
                    textvariable=self.yoff_var, command=self.refresh_preview).grid(
            row=1, column=1, sticky="ew", pady=3)
        ttk.Label(box, text="Negative moves left / up.",
                  style="Muted.TLabel", wraplength=280).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # --- destination ---
        box = ttk.Labelframe(p, text=" Printer ", padding=8)
        box.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        box.columnconfigure(1, weight=1)
        r += 1

        ttk.Label(box, text="Host", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=3)
        self.host_var = tk.StringVar(value=self.cfg["host"])
        ttk.Entry(box, textvariable=self.host_var, width=16).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(box, text="Port", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=3)
        self.port_var = tk.StringVar(value=str(self.cfg["port"]))
        ttk.Entry(box, textvariable=self.port_var, width=16).grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Button(box, text="Check printer status", command=self.check_status).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.printer_var = tk.StringVar(value="Status not checked yet.")
        ttk.Label(box, textvariable=self.printer_var, style="Muted.TLabel",
                  wraplength=280, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _build_print_box(self, p):
        p.columnconfigure(0, weight=1)
        box = ttk.Labelframe(p, text=" Print ", padding=8)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Copies", style="Card.TLabel").grid(row=0, column=0, sticky="w", pady=3)
        self.copies_var = tk.StringVar(value=str(self.cfg["copies"]))
        ttk.Spinbox(box, from_=1, to=999, width=10, textvariable=self.copies_var).grid(
            row=0, column=1, sticky="ew", pady=3)

        self.print_btn = ttk.Button(box, text="Print", style="Accent.TButton",
                                    command=self.do_print, state="disabled")
        self.print_btn.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _build_preview(self, p):
        head = ttk.Frame(p, style="Card.TFrame", padding=(10, 6))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(2, weight=1)

        self.prev_btn = ttk.Button(head, text="<", width=3, command=lambda: self.turn(-1),
                                   state="disabled")
        self.prev_btn.grid(row=0, column=0)
        self.next_btn = ttk.Button(head, text=">", width=3, command=lambda: self.turn(1),
                                   state="disabled")
        self.next_btn.grid(row=0, column=1, padx=(4, 10))
        self.page_var = tk.StringVar(value="No document")
        ttk.Label(head, textvariable=self.page_var, style="Card.TLabel").grid(
            row=0, column=2, sticky="w")
        self.info_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.info_var, style="Muted.TLabel").grid(
            row=0, column=3, sticky="e")

        wrap = tk.Frame(p, bg="#020617", highlightthickness=1, highlightbackground="#334155")
        wrap.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.canvas = tk.Canvas(wrap, bg="#020617", highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self._last_canvas_size = (0, 0)
        self._draw_placeholder()

    def _draw_placeholder(self):
        self.canvas.delete("all")
        w = self.canvas.winfo_width() or 600
        h = self.canvas.winfo_height() or 400
        self.canvas.create_text(w // 2, h // 2, fill=MUTED, font=("Segoe UI", 11),
                                text="Preview of the dithered 1-bit output appears here.\n"
                                     "Open a PDF, PNG or JPG to start.",
                                justify="center")

    # ---------- state ----------

    def collect(self):
        def f(var, default):
            try:
                return float(var.get())
            except ValueError:
                return default
        return {
            "paper": self.paper_var.get(),
            "landscape": self.orient_var.get() == "Landscape",
            "dpi": int(self.dpi_var.get()),
            "dither": bool(self.dither_var.get()),
            "x_offset": max(-2.0, min(2.0, f(self.xoff_var, 0.0))),
            "y_offset": max(-2.0, min(2.0, f(self.yoff_var, 0.0))),
            "host": self.host_var.get().strip() or DEFAULTS["host"],
            "port": int(self.port_var.get()) if self.port_var.get().strip().isdigit() else 9100,
            "copies": max(1, min(999, int(f(self.copies_var, 1)))),
        }

    def persist(self):
        try:
            c = self.collect()
        except Exception:
            return
        c["last_dir"] = self.cfg.get("last_dir", "")
        self.cfg.update(c)
        save_settings(self.cfg)

    def on_close(self):
        self.persist()
        if self.doc:
            self.doc.close()
        self.root.destroy()

    # ---------- document ----------

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Open a document",
            initialdir=self.cfg.get("last_dir") or os.path.expanduser("~"),
            filetypes=[("Printable files", "*.pdf *.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff"),
                       ("PDF", "*.pdf"), ("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"),
                       ("All files", "*.*")])
        if not path:
            return
        self.load(path)

    def load(self, path):
        try:
            if self.doc:
                self.doc.close()
            self.doc = raster.Document(path)
        except Exception as e:
            self.doc = None
            messagebox.showerror(APP_NAME, f"Could not open the file:\n\n{e}")
            return
        self.cfg["last_dir"] = os.path.dirname(path)
        self.page_index = 0
        self.preview_cache.clear()
        self.file_var.set(os.path.basename(path))
        self.print_btn.configure(state="normal")
        self.status_var.set(f"Loaded {os.path.basename(path)} ({self.doc.page_count} page(s)).")
        self.refresh_preview()

    def turn(self, delta):
        if not self.doc:
            return
        n = self.doc.page_count
        self.page_index = max(0, min(n - 1, self.page_index + delta))
        self.render_current()

    def _nav_state(self):
        if not self.doc:
            self.prev_btn.configure(state="disabled")
            self.next_btn.configure(state="disabled")
            self.page_var.set("No document")
            return
        n = self.doc.page_count
        self.page_var.set(f"Page {self.page_index + 1} of {n}")
        self.prev_btn.configure(state="normal" if self.page_index > 0 else "disabled")
        self.next_btn.configure(state="normal" if self.page_index < n - 1 else "disabled")

    def refresh_preview(self):
        self.preview_cache.clear()
        self.render_current()

    def render_current(self):
        if not self.doc:
            return
        self._nav_state()
        try:
            opts = self.collect()
        except Exception:
            return
        key = (self.page_index, opts["dpi"], opts["paper"], opts["landscape"], opts["dither"])
        if key in self.preview_cache:
            bw, cov = self.preview_cache[key]
            self.current_bw = bw
            self._show(bw, cov, opts)
            return

        self._render_token += 1
        token = self._render_token
        self.status_var.set("Rendering...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def work():
            try:
                gray = self.doc.render_gray(self.page_index, opts["dpi"],
                                            opts["paper"], opts["landscape"])
                bw = raster.halftone(gray, opts["dither"])
                cov = raster.coverage(bw)
                self.events.put(("render", token, key, bw, cov, opts))
            except Exception:
                self.events.put(("error", "Render failed:\n\n" + traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def _on_canvas_resize(self, event):
        size = (event.width, event.height)
        if abs(size[0] - self._last_canvas_size[0]) < 8 and \
           abs(size[1] - self._last_canvas_size[1]) < 8:
            return
        self._last_canvas_size = size
        if self.current_bw is None:
            self._draw_placeholder()
        else:
            try:
                self._show(self.current_bw, self._last_cov, self.collect())
            except Exception:
                pass

    def _show(self, bw, cov, opts):
        self._last_cov = cov
        self.canvas.delete("all")
        cw = self.canvas.winfo_width() or 700
        ch = self.canvas.winfo_height() or 500
        pad = 18

        code, pw_in, ph_in = raster.PAPERS[opts["paper"]]
        if opts["landscape"]:
            pw_in, ph_in = ph_in, pw_in
        scale = min((cw - 2 * pad) / pw_in, (ch - 2 * pad) / ph_in)
        sheet_w, sheet_h = int(pw_in * scale), int(ph_in * scale)
        x0 = (cw - sheet_w) // 2
        y0 = (ch - sheet_h) // 2
        self.canvas.create_rectangle(x0 + 3, y0 + 3, x0 + sheet_w + 3, y0 + sheet_h + 3,
                                     fill="#000000", outline="")
        self.canvas.create_rectangle(x0, y0, x0 + sheet_w, y0 + sheet_h,
                                     fill="#ffffff", outline="#475569")

        # Downscale through L so the dither pattern reads as gray rather than
        # disappearing into aliasing - this is what the user is judging.
        img_w = max(1, int(bw.width / opts["dpi"] * scale))
        img_h = max(1, int(bw.height / opts["dpi"] * scale))
        disp = bw.convert("L").resize((img_w, img_h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(disp)

        margin_px = raster.MARGIN_IN * scale
        ix = x0 + margin_px + opts["x_offset"] * scale
        iy = y0 + margin_px + opts["y_offset"] * scale
        self.canvas.create_image(ix, iy, image=self._photo, anchor="nw")
        self.canvas.create_rectangle(x0, y0, x0 + sheet_w, y0 + sheet_h, outline="#475569")

        ink, pct = cov
        self.info_var.set(f"{bw.width}x{bw.height} px - {pct:.1f}% ink coverage")
        self.status_var.set(
            f"Preview: {opts['paper']} {'landscape' if opts['landscape'] else 'portrait'}, "
            f"{opts['dpi']} dpi, dither {'on' if opts['dither'] else 'off'}, "
            f"{ink:,} ink pixels ({pct:.1f}%).")

    # ---------- printer ----------

    def check_status(self):
        try:
            opts = self.collect()
        except Exception:
            return
        self.status_var.set("Querying printer...")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)

        def work():
            try:
                st = raster.printer_status(opts["host"])
                self.events.put(("status", st))
            except Exception as e:
                self.events.put(("statuserr", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def do_print(self):
        if not self.doc:
            return
        try:
            opts = self.collect()
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Invalid settings: {e}")
            return

        n = self.doc.page_count
        sheets = n * opts["copies"]
        if not messagebox.askyesno(
                APP_NAME,
                f"Send to {opts['host']}:{opts['port']} on the raw port?\n\n"
                f"Pages: {n}\nCopies: {opts['copies']}\nSheets: {sheets}\n"
                f"{opts['paper']} {'landscape' if opts['landscape'] else 'portrait'} "
                f"at {opts['dpi']} dpi\n\nThis bypasses the Windows spooler."):
            return

        self.print_btn.configure(state="disabled")
        self.progress.configure(mode="determinate", value=0, maximum=100)
        self.status_var.set("Rasterising all pages...")

        def work():
            try:
                pages = []
                for i in range(n):
                    gray = self.doc.render_gray(i, opts["dpi"], opts["paper"], opts["landscape"])
                    pages.append(raster.halftone(gray, opts["dither"]))
                    self.events.put(("prog", (i + 1) / n * 30.0,
                                     f"Rasterising page {i + 1} of {n}..."))
                job = raster.build_pcl(
                    pages, dpi=opts["dpi"], paper=opts["paper"],
                    landscape=opts["landscape"], copies=opts["copies"],
                    x_offset_in=opts["x_offset"], y_offset_in=opts["y_offset"],
                    job_name=os.path.basename(self.doc.path)[:60])
                self.events.put(("prog", 35.0,
                                 f"Sending {len(job) / 1024 / 1024:.1f} MB to "
                                 f"{opts['host']}:{opts['port']}..."))

                def prog(sent, total):
                    self.events.put(("prog", 35.0 + sent / total * 65.0, None))

                raster.send_raw(job, opts["host"], opts["port"], progress=prog)
                self.events.put(("done", len(job), sheets, opts))
            except Exception as e:
                self.events.put(("printerr", f"{type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    # ---------- event pump ----------

    def _poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]

                if kind == "render":
                    _, token, key, bw, cov, opts = ev
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    if token == self._render_token:
                        self.preview_cache[key] = (bw, cov)
                        self.current_bw = bw
                        self._show(bw, cov, opts)

                elif kind == "status":
                    st = ev[1]
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    txt = (f"{st.get('model', '?')}  (s/n {st.get('serial', '?')})\n"
                           f"Status: {st.get('status', '?')}\n"
                           f"Toner: {st.get('toner_pct', '?')}%  "
                           f"({st.get('toner_id', '')})\n"
                           f"Pages on toner: {st.get('page_count', '?')}\n"
                           f"Drum: {st.get('drum_pct', '?')}%")
                    self.printer_var.set(txt)
                    self.status_var.set(
                        f"{st.get('model', '?')}: {st.get('status', '?')} - "
                        f"toner {st.get('toner_pct', '?')}%, "
                        f"page counter {st.get('page_count', '?')}.")

                elif kind == "statuserr":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.printer_var.set(f"Could not reach the printer:\n{ev[1]}")
                    self.status_var.set("Printer status check failed.")

                elif kind == "prog":
                    _, pct, msg = ev
                    self.progress.configure(value=pct)
                    if msg:
                        self.status_var.set(msg)

                elif kind == "done":
                    _, nbytes, sheets, opts = ev
                    self.progress.configure(value=100)
                    self.print_btn.configure(state="normal")
                    self.status_var.set(
                        f"Sent {nbytes:,} bytes to {opts['host']}:{opts['port']}. "
                        f"{sheets} sheet(s) queued on the printer itself.")
                    self.persist()
                    messagebox.showinfo(
                        APP_NAME,
                        f"Job sent successfully.\n\n"
                        f"{nbytes:,} bytes to {opts['host']}:{opts['port']}\n"
                        f"{sheets} sheet(s) - {opts['copies']} copy/copies\n\n"
                        f"Nothing was placed in the Windows print queue.")

                elif kind == "printerr":
                    self.progress.configure(value=0)
                    self.print_btn.configure(state="normal")
                    self.status_var.set("Print failed.")
                    messagebox.showerror(APP_NAME, f"Could not send the job:\n\n{ev[1]}")

                elif kind == "error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.status_var.set("Render failed.")
                    messagebox.showerror(APP_NAME, ev[1])
        except queue.Empty:
            pass
        self.root.after(60, self._poll_events)


def main():
    root = tk.Tk()
    app = App(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(200, lambda: app.load(sys.argv[1]))
    root.mainloop()


if __name__ == "__main__":
    main()
