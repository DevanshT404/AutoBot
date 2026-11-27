"""
Background worker that owns Chromium + the agent, so the chat window never blocks.

Playwright's sync API is thread-affine: everything that touches the browser
(start, agent runs, tab polling, shutdown) happens on this one thread. The GUI
talks to it only through queues:

  GUI  -> worker : submit(text), answer(text), request_stop(), shutdown()
  worker -> GUI  : events queue of tuples:
      ("ready",)                      browser is up
      ("fatal", message)              could not start
      ("busy", bool)                  a task started / ended
      ("say", kind, text)             progress line (step / checkpoint / warn / info)
      ("ask", question, secret)       agent needs an answer
      ("result", text, status)        final reply for the task
      ("tabs", {count, index, title, url})  idle tab info for the header
      ("attention", reason)            agent needs the human: raise/flash the window
"""

import queue
import re
import threading
from typing import Callable, Dict, List, Optional

from agent import BrowserAgent

ASSIGNMENT_RE = re.compile(
    r"""@([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s]+))"""
)


def parse_user_input(raw: str):
    """
    Extract local variables such as:
        @username = "my_instagram_name"
        @password = "super_secret"

    Values are stored locally and NEVER placed into the LLM prompt.
    The task sent to the LLM contains only the @variable handles.
    """
    secrets = {}
    cleaned = raw

    for match in ASSIGNMENT_RE.finditer(raw):
        name = match.group(1)
        value = next(group for group in match.groups()[1:] if group is not None)
        secrets[name] = value
        cleaned = cleaned.replace(match.group(0), " ")

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned, secrets


class _ServiceIO:
    """The agent's window to the outside world, backed by the service queues."""

    def __init__(self, service: "AgentService"):
        self.service = service

    def say(self, kind: str, text: str):
        self.service.emit("say", kind, text)

    def ask(self, question: str, secret: bool = False) -> Optional[str]:
        self.service.emit("ask", question, secret)
        while not self.service.stop_flag.is_set():
            try:
                return self.service.answers.get(timeout=0.2)
            except queue.Empty:
                continue
        return None

    def should_stop(self) -> bool:
        return self.service.stop_flag.is_set()

    def request_attention(self, reason: str):
        self.service.emit("attention", reason)


class AgentService(threading.Thread):
    def __init__(self, make_browser: Callable[[], object], planner, logs_dir: str):
        super().__init__(daemon=True, name="browserbot-worker")
        self.make_browser = make_browser
        self.planner = planner
        self.logs_dir = logs_dir

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.commands: "queue.Queue[tuple]" = queue.Queue()
        self.answers: "queue.Queue[str]" = queue.Queue()
        self.stop_flag = threading.Event()      # abort current task
        self.quit_flag = threading.Event()      # shut the worker down
        self.browser = None
        self.conversation: List[Dict[str, str]] = []

    # ---- called from the GUI thread -------------------------------------
    def submit(self, text: str):
        self.stop_flag.clear()
        self.commands.put(("task", text))

    def answer(self, text: str):
        self.answers.put(text)

    def request_stop(self):
        self.stop_flag.set()

    def shutdown(self):
        self.quit_flag.set()
        self.stop_flag.set()
        self.commands.put(("quit",))

    # ---- worker thread ---------------------------------------------------
    def emit(self, *event):
        self.events.put(event)

    def run(self):
        try:
            self.browser = self.make_browser()
            self.browser.start()
        except Exception as exc:
            self.emit("fatal", f"{type(exc).__name__}: {exc}")
            return
        self.emit("ready")

        try:
            while not self.quit_flag.is_set():
                try:
                    command = self.commands.get(timeout=1.0)
                except queue.Empty:
                    try:
                        message = self.browser.recover_playback_error()
                        if message:
                            self.emit("say", "warn", message)
                    except Exception:
                        pass
                    self._emit_tabs()
                    continue
                if command[0] == "quit":
                    break
                if command[0] == "task":
                    self._run_task(command[1])
                    self._emit_tabs()
        finally:
            try:
                self.browser.stop()
            except Exception:
                pass

    def _emit_tabs(self):
        try:
            page = self.browser.current_page()
            tabs = self.browser.live_pages()
            self.emit("tabs", {
                "count": len(tabs),
                "index": tabs.index(page) + 1 if page in tabs else 1,
                "title": page.title()[:60],
                "url": page.url,
            })
        except Exception:
            pass

    def _run_task(self, raw: str):
        task, secrets = parse_user_input(raw)
        if not task:
            return

        io = _ServiceIO(self)
        self.emit("busy", True)
        try:
            # Passwords the user didn't provide inline are asked for privately.
            for handle in re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", task):
                if handle.lower() in {"password", "pass", "secret"} and handle not in secrets:
                    value = io.ask(f"Enter @{handle} (stays on this computer, never sent to the AI):", secret=True)
                    if value is None:
                        self.emit("result", "Stopped.", "stopped")
                        return
                    secrets[handle] = value

            agent = BrowserAgent(
                browser=self.browser,
                planner=self.planner,
                logs_dir=self.logs_dir,
                io=io,
            )
            result = agent.run(task, secrets, prior=self.conversation[-5:])
            status = agent.state.status if agent.state else "done"
            self.conversation.append({"task": task, "result": result})
            self.emit("result", result, status)
        except Exception as exc:
            self.emit("result", f"Something went wrong: {type(exc).__name__}: {exc}", "failed")
        finally:
            # Drain any answer typed after the task ended.
            while not self.answers.empty():
                self.answers.get_nowait()
            self.stop_flag.clear()
            self.emit("busy", False)
