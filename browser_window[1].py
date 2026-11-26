"""Chat-style floating window for AutoBot. Talks to the worker thread via AgentService."""

import queue
import sys
import time
import tkinter as tk
from tkinter import font as tkfont

from service import ASSIGNMENT_RE, AgentService

C = {
    "bg": "#f4f5f7",
    "panel": "#ffffff",
    "border": "#dcdfe6",
    "text": "#1c1f26",
    "muted": "#6b7280",
    "accent": "#2f6df6",
    "accent_fg": "#ffffff",
    "ok": "#15803d",
    "warn": "#b45309",
    "err": "#b91c1c",
}

WELCOME = (
    "Hi! Tell me what to do in the browser, for example “open YouTube and play "
    "lofi beats”.\n\n"
    "I work in the tab you’re looking at and can switch between tabs when the "
    "task needs it.\n\n"
    "Private values stay on this computer: write them like "
    "@username = \"me\" @password = \"secret\" and I’ll only pass the handles to the AI."
)


def mask_secrets(text: str) -> str:
    return ASSIGNMENT_RE.sub(lambda m: f"@{m.group(1)} = ••••", text)


class BrowserWindow:
    def __init__(self, service: AgentService):
        self.service = service
        self.busy = False
        self.awaiting = None          # None, or bool "secret?" while the agent asks something
        self.fatal = False
        self.closing = False
        self.activity = None          # frame collecting the current run's step lines
        self.bubbles = []             # labels whose wraplength follows window width
        self._tabs_text = ""
        self._spin = 0

        self.root = tk.Tk()
        self.root.title("AutoBot")
        self.root.configure(bg=C["bg"])
        self.root.minsize(360, 480)
        width, height = 440, 700
        x = max(self.root.winfo_screenwidth() - width - 24, 0)
        self.root.geometry(f"{width}x{height}+{x}+48")
        try:
            self.root.attributes("-topmost", True)   # float above the browser
        except tk.TclError:
            pass

        base = tkfont.nametofont("TkDefaultFont").actual()["family"]
        self.f_body = (base, 13)
        self.f_small = (base, 11)
        self.f_title = (base, 15, "bold")

        self._build_header()
        self._build_footer()
        self._build_chat()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.agent_says(WELCOME)
        self.set_status("Starting browser…")
        self.root.after(80, self._poll)
        self.root.after(400, self._tick)
        self.entry.focus_force()

    # ------------------------------------------------------------------ layout
    def _build_header(self):
        head = tk.Frame(self.root, bg=C["panel"])
        head.pack(side="top", fill="x")
        row = tk.Frame(head, bg=C["panel"])
        row.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(row, text="AutoBot", font=self.f_title, bg=C["panel"], fg=C["text"]).pack(side="left")
        self.tab_badge = tk.Label(row, text="", font=self.f_small, bg=C["panel"], fg=C["muted"])
        self.tab_badge.pack(side="right")
        self.page_label = tk.Label(
            head, text="Starting…", font=self.f_small, bg=C["panel"], fg=C["muted"],
            anchor="w", justify="left",
        )
        self.page_label.pack(fill="x", padx=14, pady=(0, 10))
        tk.Frame(self.root, bg=C["border"], height=1).pack(side="top", fill="x")

    def _build_footer(self):
        footer = tk.Frame(self.root, bg=C["bg"])
        footer.pack(side="bottom", fill="x")

        self.status_row = tk.Frame(footer, bg=C["bg"])
        self.status_row.pack(fill="x", padx=14, pady=(6, 0))
        self.status = tk.Label(self.status_row, text="", font=self.f_small, bg=C["bg"], fg=C["muted"], anchor="w")
        self.status.pack(side="left")
        self.stop_btn = tk.Label(
            self.status_row, text="■ Stop", font=(self.f_small[0], 11, "bold"),
            bg=C["bg"], fg=C["err"], cursor="hand2",
        )
        self.stop_btn.bind("<Button-1>", lambda _e: self.stop())

        box = tk.Frame(footer, bg=C["panel"], highlightthickness=1, highlightbackground=C["border"])
        box.pack(fill="x", padx=12, pady=(6, 12))
        self.entry = tk.Entry(
            box, font=self.f_body, bg=C["panel"], fg=C["text"], insertbackground=C["text"],
            relief="flat", highlightthickness=0, bd=0,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(12, 6), pady=10)
        self.entry.bind("<Return>", lambda _e: self.send())
        self.send_btn = tk.Label(
            box, text="Send", font=(self.f_body[0], 12, "bold"), bg=C["accent"], fg=C["accent_fg"],
            padx=14, pady=6, cursor="hand2",
        )
        self.send_btn.pack(side="right", padx=6, pady=6)
        self.send_btn.bind("<Button-1>", lambda _e: self.send())

    def _build_chat(self):
        wrap = tk.Frame(self.root, bg=C["bg"])
        wrap.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=C["bg"], highlightthickness=0, bd=0)
        bar = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.inner = tk.Frame(self.canvas, bg=C["bg"])
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.root.bind_all("<MouseWheel>", self._on_wheel)

    def _on_canvas_resize(self, event):
        self.canvas.itemconfigure(self.inner_id, width=event.width)
        wrap = max(event.width - 90, 160)
        for label in self.bubbles:
            try:
                label.configure(wraplength=wrap)
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
    def _wrap_width(self) -> int:
        return max(self.canvas.winfo_width() - 90, 260)

    def _bubble(self, text, user: bool, color=None):
        self.activity = None
        row = tk.Frame(self.inner, bg=C["bg"])
        row.pack(fill="x", padx=12, pady=5)
        if user:
            label = tk.Label(row, text=text, font=self.f_body, bg=C["accent"], fg=C["accent_fg"],
                             justify="left", anchor="w", padx=12, pady=8, wraplength=self._wrap_width())
            label.pack(side="right")
        else:
            label = tk.Label(row, text=text, font=self.f_body, bg=C["panel"], fg=color or C["text"],
                             justify="left", anchor="w", padx=12, pady=8, wraplength=self._wrap_width(),
                             highlightthickness=1, highlightbackground=C["border"])
            label.pack(side="left")
        self.bubbles.append(label)
        self._scroll_bottom()

    def user_says(self, text: str):
        self._bubble(text, user=True)

    def agent_says(self, text: str, color=None):
        self._bubble(text, user=False, color=color)

    def activity_line(self, text: str, color=None):
        if self.activity is None:
            self.activity = tk.Frame(self.inner, bg=C["bg"])
            self.activity.pack(fill="x", padx=18, pady=(0, 4))
        label = tk.Label(self.activity, text=text, font=self.f_small, bg=C["bg"], fg=color or C["muted"],
                         justify="left", anchor="w", wraplength=self._wrap_width() + 40)
        label.pack(fill="x")
        self.bubbles.append(label)
        self._scroll_bottom()

    # ------------------------------------------------------------------ state
    def set_status(self, text: str):
        self.status.configure(text=text)

    def set_busy(self, busy: bool):
        self.busy = busy
        if busy:
            self.stop_btn.pack(side="right")
        else:
            self.stop_btn.pack_forget()
            self.set_status("Ask me to do something in the browser.")

    def _tick(self):
        if self.busy and self.awaiting is None:
            self._spin = (self._spin + 1) % 4
            self.set_status("Working" + "." * self._spin)
        elif self.busy:
            self.set_status("Waiting for your answer…")
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
            return

        if self.busy:
            self.activity_line("Still working — press Stop to cancel first.", C["warn"])
            return

        self.entry.delete(0, "end")
        self.user_says(mask_secrets(text))
        self.set_busy(True)
        self.service.submit(text)

    def stop(self):
        self.service.request_stop()
        self.set_status("Stopping…")

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
            self.set_status("Ask me to do something in the browser.")
        elif kind == "fatal":
            self.fatal = True
            self.agent_says(f"I couldn’t start the browser.\n\n{event[1]}", C["err"])
            self.set_status("Browser failed to start.")
        elif kind == "busy":
            self.set_busy(event[1])
        elif kind == "say":
            _, level, text = event
            if level == "step":
                self.activity_line("•  " + text)
            elif level == "checkpoint":
                self.activity_line("✓  " + text, C["ok"])
            elif level == "warn":
                self.activity_line("!  " + text, C["warn"])
        elif kind == "ask":
            _, question, secret = event
            self.awaiting = bool(secret)
            self.entry.configure(show="•" if secret else "")
            self.agent_says(question)
            self._grab_attention()
            self.entry.focus_force()
        elif kind == "result":
            _, text, status = event
            color = {"done": None, "blocked": C["warn"], "unverified": C["warn"],
                     "stopped": C["muted"], "failed": C["err"]}.get(status)
            prefix = {"done": "✓ ", "blocked": "", "unverified": "", "stopped": "", "failed": ""}.get(status, "")
            self.agent_says(prefix + text, color)
            self.awaiting = None
            self.entry.configure(show="")
        elif kind == "attention":
            self._grab_attention()
            self.activity_line("!  " + event[1], C["warn"])
        elif kind == "tabs":
            info = event[1]
            badge = f"Tab {info['index']} of {info['count']}" if info["count"] > 1 else "1 tab"
            self.tab_badge.configure(text=badge)
            title = info["title"] or info["url"]
            text = f"{title}  ·  {info['url'][:60]}" if info["title"] else info["url"][:80]
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
