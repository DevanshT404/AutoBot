"""Chat-style floating window for BrowserBot. Talks to the worker thread via AgentService."""

from __future__ import annotations

import os
import queue
import sys
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from service import ASSIGNMENT_RE, AgentService

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ICON_ICO = os.path.join(ASSETS, "browserbot.ico")
ICON_PNG = os.path.join(ASSETS, "browserbot.png")


def _enable_dpi_awareness():
    """Render at the real screen DPI instead of letting Windows bitmap-stretch (blurry) the window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _rgb(color: str) -> tuple:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


def _render_button(master, size: int, mode: str, fill: str, fg: str, bg: str) -> tk.PhotoImage:
    """Anti-aliased round send/stop button as a PhotoImage (Tk canvas shapes have no AA)."""
    k = size / 32.0                       # shapes are designed on a 32x32 grid
    bg_c, fill_c, fg_c = _rgb(bg), _rgb(fill), _rgb(fg)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            px, py = (x + 0.5) / k, (y + 0.5) / k
            d_circle = ((px - 16) ** 2 + (py - 16) ** 2) ** 0.5 - 14
            col = _mix(bg_c, fill_c, max(0.0, min(1.0, 0.5 - d_circle * k)))
            if mode == "stop":
                qx, qy = abs(px - 16) - 5 + 1.5, abs(py - 16) - 5 + 1.5
                d_glyph = (max(qx, 0) ** 2 + max(qy, 0) ** 2) ** 0.5 + min(max(qx, qy), 0) - 1.5
            else:
                d_glyph = min(_seg_dist(px, py, 16, 22, 16, 10),
                              _seg_dist(px, py, 11, 15, 16, 10),
                              _seg_dist(px, py, 21, 15, 16, 10)) - 1.1
            col = _mix(col, fg_c, max(0.0, min(1.0, 0.5 - d_glyph * k)))
            row.append("#%02x%02x%02x" % tuple(int(round(v)) for v in col))
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(master=master, width=size, height=size)
    img.put(" ".join(rows))
    return img


# Dark palette, close to the Claude / modern chatbot look.
C = {
    "bg": "#262624",          # chat background
    "header": "#1f1e1d",      # top bar
    "line": "#3a3936",        # hairlines
    "input": "#30302e",       # composer
    "input_border": "#46453f",
    "input_focus": "#6b6a63",
    "user": "#3a3936",        # user bubble
    "text": "#ececea",
    "muted": "#9c9a92",
    "faint": "#6b6a63",
    "accent": "#34302f",
    "accent_dim": "#7a4a3a",
    "accent_fg": "#ffffff",
    "ok": "#7fb77e",
    "warn": "#e0a458",
    "err": "#e5786d",
    "scroll": "#46453f",
    "scroll_hover": "#5c5b55",
}

WELCOME = (
    "Hi! Tell me what to do in the browser, for example “open YouTube and play "
    "lofi beats”.\n\n"
    "I work in the tab you’re looking at and can switch between tabs when the "
    "task needs it.\n\n"
    "Private values stay on this computer: write them like "
    "@username = \"me\" @password = \"secret\" and I’ll only pass the handles to the AI."
)

IDLE_STATUS = "Ask me to do something in the browser."


def mask_secrets(text: str) -> str:
    return ASSIGNMENT_RE.sub(lambda m: f"@{m.group(1)} = ••••", text)


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


class StepsBlock:
    """Collapsible “Working…” block. Steps are hidden until the header is clicked."""

    def __init__(self, win: "BrowserWindow"):
        self.win = win
        self.count = 0
        self.expanded = False
        self.running = True
        self.last = ""

        self.frame = tk.Frame(win.inner, bg=C["bg"])
        self.frame.pack(fill="x", padx=18, pady=(4, 8), anchor="w")

        self.head = tk.Frame(self.frame, bg=C["bg"], cursor="hand2")
        self.head.pack(fill="x", anchor="w")
        self.dot = tk.Label(self.head, text="●", font=win.f_small, bg=C["bg"], fg=C["accent"])
        self.dot.pack(side="left", padx=(0, 8))
        self.title = tk.Label(self.head, text="Working", font=win.f_small_b, bg=C["bg"], fg=C["text"])
        self.title.pack(side="left")
        self.chev = tk.Label(self.head, text="›", font=win.f_small_b, bg=C["bg"], fg=C["muted"])
        self.chev.pack(side="left", padx=(6, 0))
        self.sub = tk.Label(self.frame, text="", font=win.f_small, bg=C["bg"], fg=C["faint"],
                            anchor="w", justify="left")
        self.sub.pack(fill="x", padx=(22, 0), pady=(1, 0))

        for w in (self.head, self.dot, self.title, self.chev):
            w.bind("<Button-1>", self.toggle)
            w.bind("<Enter>", lambda _e: self._hover(True))
            w.bind("<Leave>", lambda _e: self._hover(False))

        # Body: thin vertical rule + list of steps, hidden by default.
        self.body = tk.Frame(self.frame, bg=C["bg"])
        tk.Frame(self.body, bg=C["line"], width=2).pack(side="left", fill="y", padx=(5, 12))
        self.lines = tk.Frame(self.body, bg=C["bg"])
        self.lines.pack(side="left", fill="x", expand=True)
        self._render_title()

    def _hover(self, on: bool):
        self.chev.configure(fg=C["text"] if on else C["muted"])

    def _render_title(self, dots: int = 3):
        n = f"  ·  {self.count} step{'s' if self.count != 1 else ''}" if self.count else ""
        if self.running:
            self.title.configure(text="Working" + "." * dots + " " * (3 - dots) + n)
        self.chev.configure(text="⌄" if self.expanded else "›")
        if self.running and not self.expanded and self.last:
            self.sub.configure(text=_clip(self.last, 70))
            if not self.sub.winfo_ismapped():
                opts = dict(fill="x", padx=(22, 0), pady=(1, 0))
                if self.body.winfo_ismapped():
                    opts["before"] = self.body
                self.sub.pack(**opts)
        else:
            self.sub.pack_forget()

    def toggle(self, _e=None):
        self.expanded = not self.expanded
        if self.expanded:
            self.body.pack(fill="x", pady=(6, 0))
        else:
            self.body.pack_forget()
        self._render_title()
        self.win._scroll_bottom()

    def add(self, text: str, icon: str, color=None):
        self.count += 1
        self.last = text
        row = tk.Frame(self.lines, bg=C["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=icon, font=self.win.f_small, bg=C["bg"], fg=color or C["faint"],
                 width=2, anchor="nw").pack(side="left", anchor="n")
        label = tk.Label(row, text=text, font=self.win.f_small, bg=C["bg"], fg=color or C["muted"],
                         justify="left", anchor="w", wraplength=self.win._wrap("step"))
        label.pack(side="left", fill="x")
        self.win.bubbles.append((label, "step"))
        self._render_title()
        self.win._scroll_bottom()

    def animate(self, frame: int):
        if not self.running:
            return
        self.dot.configure(fg=C["accent"] if frame % 2 == 0 else C["accent_dim"])
        self._render_title(frame % 4)

    def finish(self, status: str = "done"):
        if not self.running:
            return
        self.running = False
        if self.count == 0:
            self.frame.destroy()
            return
        steps = f"{self.count} step{'s' if self.count != 1 else ''}"
        icon, color, text = {
            "done": ("✓", C["ok"], f"Completed {steps}"),
            "stopped": ("■", C["muted"], f"Stopped after {steps}"),
            "failed": ("✕", C["err"], f"Failed after {steps}"),
            "blocked": ("!", C["warn"], f"Paused after {steps}"),
            "unverified": ("!", C["warn"], f"Ran {steps}"),
        }.get(status, ("•", C["muted"], f"Ran {steps}"))
        self.dot.configure(text=icon, fg=color)
        self.title.configure(text=text, fg=C["muted"])
        self._render_title()


class BrowserWindow:
    def __init__(self, service: AgentService):
        self.service = service
        self.busy = False
        self.awaiting = None          # None, or bool "secret?" while the agent asks something
        self.fatal = False
        self.closing = False
        self.activity: StepsBlock | None = None
        self.bubbles = []             # (label, kind) whose wraplength follows window width
        self._tabs_text = ""
        self._spin = 0
        self._status_after = None

        _enable_dpi_awareness()
        if sys.platform == "win32":
            try:
                # Own taskbar identity so Windows shows our icon, not python.exe's.
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BrowserBot")
            except Exception:
                pass
        self.root = tk.Tk()
        self.scale = max(self.root.winfo_fpixels("1i") / 96.0, 1.0)
        sc = lambda v: int(round(v * self.scale))
        self.root.title("BrowserBot")
        try:
            self.root.iconbitmap(default=ICON_ICO)
        except Exception:
            try:
                self._icon_img = tk.PhotoImage(file=ICON_PNG)
                self.root.iconphoto(True, self._icon_img)
            except Exception:
                pass
        self.root.configure(bg=C["bg"])
        self.root.minsize(sc(360), sc(480))
        width, height = sc(440), sc(700)
        x = max(self.root.winfo_screenwidth() - width - sc(24), 0)
        self.root.geometry(f"{width}x{height}+{x}+{sc(48)}")
        try:
            self.root.attributes("-topmost", True)   # float above the browser
        except tk.TclError:
            pass
        self._dark_titlebar()
        self._style_scrollbar()

        if sys.platform == "win32":
            base = "Segoe UI"
        else:
            base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f_body = (base, 11)
        self.f_small = (base, 9)
        self.f_small_b = (base, 9, "bold")
        self.f_title = (base, 12, "bold")

        self._build_header()
        self._build_footer()
        self._build_chat()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.agent_says(WELCOME)
        self.set_status("Starting browser…")
        self.root.after(80, self._poll)
        self.root.after(400, self._tick)
        self.entry.focus_force()

    # ------------------------------------------------------------------ chrome
    def _dark_titlebar(self):
        """Ask Windows 10/11 for a dark title bar so it matches the window."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass

    def _style_scrollbar(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            return
        style.layout("Dark.Vertical.TScrollbar", [
            ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
                ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        style.configure("Dark.Vertical.TScrollbar", background=C["scroll"], troughcolor=C["bg"],
                        bordercolor=C["bg"], lightcolor=C["scroll"], darkcolor=C["scroll"],
                        gripcount=0, arrowsize=8, relief="flat")
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", C["scroll_hover"]), ("pressed", C["scroll_hover"])],
                  lightcolor=[("active", C["scroll_hover"])], darkcolor=[("active", C["scroll_hover"])])

    # ------------------------------------------------------------------ layout
    def _build_header(self):
        head = tk.Frame(self.root, bg=C["header"])
        head.pack(side="top", fill="x")
        row = tk.Frame(head, bg=C["header"])
        row.pack(fill="x", padx=16, pady=(12, 2))
        tk.Label(row, text="BrowserBot", font=self.f_title, bg=C["header"], fg=C["text"]).pack(side="left")
        self.tab_badge = tk.Label(row, text="", font=self.f_small, bg=C["input"], fg=C["muted"], padx=8, pady=2)
        self.page_label = tk.Label(
            head, text="Starting…", font=self.f_small, bg=C["header"], fg=C["faint"],
            anchor="w", justify="left",
        )
        self.page_label.pack(fill="x", padx=16, pady=(0, 12))
        tk.Frame(self.root, bg=C["line"], height=1).pack(side="top", fill="x")

    def _build_footer(self):
        footer = tk.Frame(self.root, bg=C["bg"])
        footer.pack(side="bottom", fill="x")

        self.box = tk.Frame(footer, bg=C["input"], highlightthickness=1,
                            highlightbackground=C["input_border"], highlightcolor=C["input_border"])
        self.box.pack(fill="x", padx=14, pady=(8, 4))
        self.entry = tk.Entry(
            self.box, font=self.f_body, bg=C["input"], fg=C["text"], insertbackground=C["text"],
            relief="flat", highlightthickness=0, bd=0,
            disabledbackground=C["input"], selectbackground=C["accent_dim"],
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(14, 6), pady=12)
        self.entry.bind("<Return>", lambda _e: self.send())
        self.entry.bind("<FocusIn>", lambda _e: self.box.configure(highlightbackground=C["input_focus"]))
        self.entry.bind("<FocusOut>", lambda _e: self.box.configure(highlightbackground=C["input_border"]))

        # Round send / stop button drawn on a canvas.
        self._btn_size = int(round(32 * self.scale))
        self._btn_cache = {}
        self.btn = tk.Canvas(self.box, width=self._btn_size, height=self._btn_size, bg=C["input"],
                             highlightthickness=0, cursor="hand2")
        self.btn.pack(side="right", padx=(0, 8), pady=6)
        self.btn.bind("<Button-1>", lambda _e: self._on_button())
        self.btn.bind("<Enter>", lambda _e: self._draw_button(hover=True))
        self.btn.bind("<Leave>", lambda _e: self._draw_button(hover=False))
        self._draw_button()

        self.status = tk.Label(footer, text="", font=self.f_small, bg=C["bg"], fg=C["faint"], anchor="center")
        self.status.pack(fill="x", padx=14, pady=(0, 10))

    def _button_mode(self) -> str:
        return "stop" if self.busy and self.awaiting is None else "send"

    def _draw_button(self, hover: bool = False):
        mode = self._button_mode()
        key = (mode, hover)
        if key not in self._btn_cache:
            if mode == "stop":
                fill, fg = (C["text"] if hover else C["muted"]), C["input"]
            else:
                fill, fg = ("#9c9a92" if hover else C["accent"]), C["accent_fg"]
            self._btn_cache[key] = _render_button(self.root, self._btn_size, mode, fill, fg, C["input"])
        self._btn_img = self._btn_cache[key]
        self.btn.delete("all")
        self.btn.create_image(0, 0, anchor="nw", image=self._btn_img)

    def _on_button(self):
        if self._button_mode() == "stop":
            self.stop()
        else:
            self.send()

    def _build_chat(self):
        wrap = tk.Frame(self.root, bg=C["bg"])
        wrap.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=C["bg"], highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview,
                            style="Dark.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y", padx=(0, 2), pady=4)
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=C["bg"])
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        tk.Frame(self.inner, bg=C["bg"], height=8).pack(fill="x")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.root.bind_all("<MouseWheel>", self._on_wheel)

    def _wrap(self, kind: str, width: int | None = None) -> int:
        w = width if width is not None else self.canvas.winfo_width()
        if w <= 1:
            w = 420
        if kind == "user":
            return max(int(w * 0.72), 180)
        if kind == "step":
            return max(w - 90, 160)
        return max(w - 44, 200)

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.inner_id, width=event.width)
        for label, kind in self.bubbles:
            try:
                label.configure(wraplength=self._wrap(kind, event.width))
            except tk.TclError:
                pass

    def _on_wheel(self, event):
        step = -event.delta if sys.platform == "darwin" else -int(event.delta / 120)
        if step:
            self.canvas.yview_scroll(step, "units")

    def _scroll_bottom(self):
        self.root.update_idletasks()
        self.canvas.yview_moveto(1.0)

    # ---------------------------------------------------------------- messages
    def _close_activity(self, status: str = "paused"):
        if self.activity is not None:
            self.activity.finish(status)
            self.activity = None

    def _bubble(self, text, user: bool, color=None):
        self._close_activity()
        row = tk.Frame(self.inner, bg=C["bg"])
        if user:
            row.pack(fill="x", padx=16, pady=(10, 6))
            label = tk.Label(row, text=text, font=self.f_body, bg=C["user"], fg=C["text"],
                             justify="left", anchor="w", padx=14, pady=9, wraplength=self._wrap("user"))
            label.pack(side="right")
            self.bubbles.append((label, "user"))
        else:
            row.pack(fill="x", padx=18, pady=(6, 8))
            label = tk.Label(row, text=text, font=self.f_body, bg=C["bg"], fg=color or C["text"],
                             justify="left", anchor="w", wraplength=self._wrap("agent"))
            label.pack(side="left", fill="x")
            self.bubbles.append((label, "agent"))
        self._scroll_bottom()

    def user_says(self, text: str):
        self._bubble(text, user=True)

    def agent_says(self, text: str, color=None):
        self._bubble(text, user=False, color=color)

    def _ensure_activity(self) -> StepsBlock:
        if self.activity is None:
            self.activity = StepsBlock(self)
            self._scroll_bottom()
        return self.activity

    def activity_line(self, text: str, icon: str = "•", color=None):
        self._ensure_activity().add(text, icon, color)

    # ------------------------------------------------------------------ state
    def set_status(self, text: str, color=None):
        if self._status_after is not None:
            self.root.after_cancel(self._status_after)
            self._status_after = None
        self.status.configure(text=text, fg=color or C["faint"])

    def flash_status(self, text: str, color=None, ms: int = 2500):
        self.set_status(text, color)
        self._status_after = self.root.after(ms, self._refresh_status)

    def _refresh_status(self):
        self._status_after = None
        if self.closing:
            return
        if not self.busy:
            self.set_status(IDLE_STATUS)
        elif self.awaiting is not None:
            self.set_status("Waiting for your answer…", C["warn"])
        else:
            self.set_status("Working in the browser — press ■ to stop")
        self._draw_button()

    def set_busy(self, busy: bool):
        was = self.busy
        self.busy = busy
        if busy and not was:
            self._ensure_activity()
        if not busy:
            self._close_activity("done")
        self._refresh_status()

    def _tick(self):
        if self.busy and self.awaiting is None and self.activity is not None:
            self._spin = (self._spin + 1) % 4
            self.activity.animate(self._spin)
        self.root.after(400, self._tick)

    def send(self):
        text = self.entry.get().strip()
        if not text or self.fatal or self.closing:
            return

        if self.awaiting is not None:
            secret = self.awaiting
            self.awaiting = None
            self.entry.configure(show="")
            self.entry.delete(0, "end")
            self.user_says("••••••••" if secret else text)
            self.service.answer(text)
            if self.busy:
                self._ensure_activity()
            self._refresh_status()
            return

        if self.busy:
            self.flash_status("Still working — press ■ to stop first.", C["warn"])
            return

        self.entry.delete(0, "end")
        self.user_says(mask_secrets(text))
        self.set_busy(True)
        self.service.submit(text)

    def stop(self):
        self.service.request_stop()
        self.set_status("Stopping…", C["warn"])

    # ------------------------------------------------------------ worker events
    def _poll(self):
        try:
            while True:
                self._handle(self.service.events.get_nowait())
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(80, self._poll)

    def _handle(self, event):
        kind = event[0]
        if kind == "ready":
            self.set_status(IDLE_STATUS)
        elif kind == "fatal":
            self.fatal = True
            self.agent_says(f"I couldn’t start the browser.\n\n{event[1]}", C["err"])
            self.set_status("Browser failed to start.", C["err"])
        elif kind == "busy":
            self.set_busy(event[1])
        elif kind == "say":
            _, level, text = event
            if level == "step":
                self.activity_line(text, "•")
            elif level == "checkpoint":
                self.activity_line(text, "✓", C["ok"])
            elif level == "warn":
                self.activity_line(text, "!", C["warn"])
        elif kind == "ask":
            _, question, secret = event
            self.awaiting = bool(secret)
            self.entry.configure(show="•" if secret else "")
            self.agent_says(question)
            self._refresh_status()
            self._grab_attention()
            self._notify("BrowserBot needs your input",
                         "Enter the requested value in BrowserBot" if secret else _clip(question, 200))
            self.entry.focus_force()
        elif kind == "result":
            _, text, status = event
            self._close_activity(status)
            color = {"done": None, "blocked": C["warn"], "unverified": C["warn"],
                     "stopped": C["muted"], "failed": C["err"]}.get(status)
            self.agent_says(text, color)
            self.awaiting = None
            self.entry.configure(show="")
            self._refresh_status()
            title = {"done": "Task done", "failed": "Task failed",
                     "stopped": "Task stopped"}.get(status, "Task needs attention")
            self._notify(title, _clip(text, 200))
        elif kind == "attention":
            self._grab_attention()
            self._notify("BrowserBot needs you", _clip(event[1], 200))
            self.activity_line(event[1], "!", C["warn"])
            self.flash_status(_clip(event[1], 60), C["warn"], 6000)
        elif kind == "tabs":
            info = event[1]
            badge = f"Tab {info['index']} of {info['count']}" if info["count"] > 1 else "1 tab"
            self.tab_badge.configure(text=badge)
            if not self.tab_badge.winfo_ismapped():
                self.tab_badge.pack(side="right")
            title = info["title"] or info["url"]
            text = f"{_clip(title, 40)}  ·  {info['url'][:50]}" if info["title"] else info["url"][:80]
            if text != self._tabs_text:
                self._tabs_text = text
                self.page_label.configure(text=text)

    def _grab_attention(self):
        """Raise the chat window and flash its taskbar button."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.after(1500, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                class FLASHWINFO(ctypes.Structure):
                    _fields_ = [
                        ("cbSize", wintypes.UINT),
                        ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD),
                    ]

                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                # FLASHW_ALL (3) | FLASHW_TIMERNOFG (12): flash until focused
                info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 3 | 12, 5, 0)
                ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
            except Exception:
                pass

    def _notify(self, title: str, message: str):
        """Show a Windows toast without blocking the Tk thread. Never pass secrets here."""
        if sys.platform != "win32":
            return
        import threading

        def show():
            try:
                # Register the app id so Windows shows "BrowserBot" as the sender.
                import winreg
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId\BrowserBot") as key:
                    winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "BrowserBot")
                    if os.path.exists(ICON_PNG):
                        winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, ICON_PNG)
            except Exception:
                pass
            icon = ICON_PNG if os.path.exists(ICON_PNG) else ""
            try:
                from winotify import Notification  # optional: pip install winotify
                Notification(app_id="BrowserBot", title=title, msg=message, icon=icon).show()
                return
            except Exception:
                pass
            try:
                import subprocess
                from xml.sax.saxutils import escape

                def ps(text):
                    return escape(text).replace("'", "''")

                script = (
                    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null;"
                    "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
                    f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{ps(title)}</text><text>{ps(message)}</text></binding></visual></toast>');"
                    "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
                    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
                    "[Windows.UI.Notifications.ToastNotification]::new($x))"
                )
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                    creationflags=0x08000000,  # CREATE_NO_WINDOW
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

        threading.Thread(target=show, daemon=True, name="browserbot-toast").start()

    # ---------------------------------------------------------------- lifecycle
    def on_close(self):
        if self.closing:
            return
        self.closing = True
        self.set_status("Closing browser…")
        self.service.shutdown()
        started = time.time()

        def wait():
            if self.service.is_alive() and time.time() - started < 10:
                self.root.after(150, wait)
            else:
                self.root.destroy()

        wait()

    def run(self):
        self.root.mainloop()
