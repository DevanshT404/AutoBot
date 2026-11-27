import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from collections import deque
from typing import Dict, List, Any, Optional

from actions import ActionExecutor, STOPPED

MAX_ITERATIONS = 40
MAX_VERIFIER_REJECTIONS = 2
HISTORY_WINDOW = 8
DEBUG = bool(os.getenv("AGENT_DEBUG"))


class ConsoleIO:
    """Default front end: prints progress, asks questions on the terminal."""

    def say(self, kind: str, text: str):
        print(f"  [{kind}] {text}")

    def ask(self, question: str, secret: bool = False) -> Optional[str]:
        print(f"\nAgent needs you: {question}")
        if secret:
            from getpass import getpass
            return getpass("You> ")
        return input("You> ").strip()

    def should_stop(self) -> bool:
        return False


def _target_label(target: Optional[Dict[str, Any]]) -> str:
    if not target:
        return ""
    label = (
        target.get("name") or target.get("text") or target.get("placeholder")
        or target.get("css") or target.get("role") or "element"
    )
    return f"“{label}”"


def describe_call(call: Dict[str, Any]) -> str:
    name = call["name"]
    a = call.get("arguments", {}) or {}
    if name == "click":
        return f"Click {_target_label(a.get('target'))}"
    if name == "type":
        text = str(a.get("text", ""))
        shown = text if text.startswith("@") else f"“{text[:60]}”"
        return f"Type {shown} into {_target_label(a.get('target'))}"
    if name == "press":
        where = _target_label(a.get("target"))
        return f"Press {a.get('key')}" + (f" in {where}" if where else "")
    if name == "scroll":
        return f"Scroll {a.get('direction', 'down')}"
    if name == "wait":
        return f"Wait {a.get('seconds', 0.5)}s"
    if name == "navigate":
        return f"Go to {a.get('url')}"
    if name == "switch_tab":
        return f"Switch to tab {a.get('tab_id')}"
    if name == "open_tab":
        return "Open a new tab" + (f" at {a['url']}" if a.get("url") else "")
    if name == "close_tab":
        return f"Close tab {a.get('tab_id')}"
    if name == "click_seat":
        return f"Pick seat {a.get('seat')}"
    if name == "request_human":
        return f"Ask for your help: {a.get('reason')}"
    if name == "checkpoint":
        return f"Checkpoint: {a.get('description', a.get('id'))}"
    if name == "ask_user":
        return f"Ask you: {a.get('question')}"
    if name == "done":
        return f"Finish: {a.get('message')}"
    return name


def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_")
    return (value[:40] or "task").lower()


@dataclass
class AgentState:
    task: str = ""
    status: str = "idle"
    completed_checkpoints: List[str] = field(default_factory=list)
    last_event: str = ""
    iterations: int = 0
    run_id: str = ""
    log_dir: str = ""


class SecretStore:
    """
    Local-only secret storage.

    The actual values live inside this process's temporary directory.
    The LLM receives only handles such as @username and @password.
    """

    def __init__(self):
        self._temp_dir = tempfile.TemporaryDirectory(prefix="browser_agent_")
        self.path = Path(self._temp_dir.name) / "secrets.json"
        self._write({})

    def _write(self, data: Dict[str, str]):
        self.path.write_text(
            json.dumps(data, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def set_many(self, secrets: Dict[str, str]):
        current = json.loads(self.path.read_text(encoding="utf-8"))
        current.update(secrets)
        self._write(current)

    def has(self, name: str) -> bool:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return name in data

    def get(self, name: str) -> str:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if name not in data:
            raise KeyError(f"Unknown local secret handle: @{name}")
        return data[name]

    def close(self):
        self._temp_dir.cleanup()


class FileLogger:
    """
    Every run gets its own directory.

    Example:

      logs/
        20261005_231900_log_into_instagram/
          state.json
          events.jsonl
          batches/
            001_plan.json
            002_plan.json
          dom/
            001_before_plan.json
            002_after_batch.json
          checkpoints/
            instagram_login_complete.json
          screenshots/
            001_interrupted.png
    """

    def __init__(self, base_dir: str, task: str, run_id: Optional[str] = None):
        stamp = run_id or f"{utc_stamp()}_{slugify(task)}"
        self.run_dir = Path(base_dir) / stamp
        self.dom_dir = self.run_dir / "dom"
        self.batch_dir = self.run_dir / "batches"
        self.checkpoint_dir = self.run_dir / "checkpoints"
        self.screenshot_dir = self.run_dir / "screenshots"

        for folder in (
            self.run_dir,
            self.dom_dir,
            self.batch_dir,
            self.checkpoint_dir,
            self.screenshot_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)

        (self.run_dir / "task.txt").write_text(task, encoding="utf-8")

    def _json_write(self, path: Path, data: Any):
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def event(self, event_type: str, **data):
        record = {
            "timestamp_utc": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            "event": event_type,
            **data,
        }
        with (self.run_dir / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def save_state(self, state: AgentState):
        self._json_write(self.run_dir / "state.json", asdict(state))

    def save_dom(self, sequence: int, label: str, dom_snapshot: str):
        # DOM snapshot is already semantic and does not contain textbox values.
        path = self.dom_dir / f"{sequence:03d}_{slugify(label)}.json"
        try:
            parsed = json.loads(dom_snapshot)
            self._json_write(path, parsed)
        except Exception:
            path.write_text(dom_snapshot, encoding="utf-8")
        return str(path)

    def save_batch(self, sequence: int, calls: List[Dict[str, Any]], event: str):
        # Never persist secrets. Tool arguments are expected to contain @handles,
        # not secret values. Redact defensively anyway.
        safe_calls = []
        for call in calls:
            item = {
                "name": call.get("name"),
                "arguments": call.get("arguments", {}),
            }
            if item["name"] == "type":
                text = item["arguments"].get("text")
                if isinstance(text, str) and text.startswith("@"):
                    pass
                else:
                    item["arguments"] = dict(item["arguments"])
                    item["arguments"]["text"] = "<non-handle text redacted from logs>"
            safe_calls.append(item)

        path = self.batch_dir / f"{sequence:03d}_plan.json"
        self._json_write(
            path,
            {
                "event": event,
                "calls": safe_calls,
            },
        )
        return str(path)

    def checkpoint(self, checkpoint_id: str, description: str, iteration: int):
        path = self.checkpoint_dir / f"{slugify(checkpoint_id)}.json"
        self._json_write(
            path,
            {
                "id": checkpoint_id,
                "description": description,
                "iteration": iteration,
                "timestamp_utc": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
            },
        )
        self.event(
            "checkpoint_reached",
            checkpoint_id=checkpoint_id,
            description=description,
        )
        return str(path)

    def screenshot(self, browser, sequence: int, label: str):
        path = self.screenshot_dir / f"{sequence:03d}_{slugify(label)}.png"
        browser.screenshot(str(path))
        return str(path)


class BrowserAgent:
    def __init__(
        self,
        browser,
        planner,
        logs_dir: str = "logs",
        run_id: Optional[str] = None,
        io: Optional[Any] = None,
    ):
        self.browser = browser
        self.planner = planner
        self.logs_dir = logs_dir
        self.resume_run_id = run_id
        self.io = io or ConsoleIO()
        self.state: Optional[AgentState] = None
        self.logger: Optional[FileLogger] = None

    def _finish(self, status: str, message: str) -> str:
        self.state.status = status
        self.state.last_event = message
        self.logger.event(f"task_{status}", message=message)
        self.logger.save_state(self.state)
        self.io.say("info", f"Run logs: {self.logger.run_dir}")
        return message

    def run(
        self,
        task: str,
        secrets: Dict[str, str],
        prior: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        self.logger = FileLogger(
            self.logs_dir,
            task,
            run_id=self.resume_run_id,
        )

        self.state = AgentState(
            task=task,
            status="running",
            completed_checkpoints=[],
            last_event="New task received.",
            iterations=0,
            run_id=self.logger.run_dir.name,
            log_dir=str(self.logger.run_dir),
        )
        self.logger.save_state(self.state)
        self.logger.event("task_started", task=task)

        secret_store = SecretStore()
        secret_store.set_many(secrets)
        executor = ActionExecutor(self.browser, secret_store, self.io.should_stop)

        history: List[str] = []
        last_signature = None
        repeats = 0
        rejections = 0
        recent = deque(maxlen=6)  # recent batch signatures, for A/B/A/B loops
        helped = 0  # times the human was asked to unstick us
        last_path = None  # origin+path of the last page seen; scroll to top on change

        try:
            self.browser.sync_to_foreground_tab()

            for _ in range(MAX_ITERATIONS):
                if self.io.should_stop():
                    return self._finish("stopped", STOPPED)

                self.state.iterations += 1
                self.logger.save_state(self.state)

                self.browser.wait_stable()

                try:
                    recovered = self.browser.recover_playback_error()
                except Exception:
                    recovered = None
                if recovered:
                    self.io.say("warn", recovered)
                    self.browser.wait_stable()

                notices = [
                    n for n in self.browser.drain_notices()
                    if n not in self.state.last_event
                ]
                if notices:
                    self.state.last_event = (self.state.last_event + " " + " ".join(notices)).strip()

                # New page (origin+path changed): some sites auto-scroll to the
                # bottom on load and hide the pickers above; reset to the top.
                try:
                    from urllib.parse import urlsplit
                    from actions import SCROLL_TOP_JS
                    _u = urlsplit(self.browser.current_page().url)
                    _path = f"{_u.scheme}://{_u.netloc}{_u.path}"
                except Exception:
                    _path = None
                if _path and _path != last_path:
                    last_path = _path
                    try:
                        self.browser.current_page().evaluate(SCROLL_TOP_JS)
                        self.browser.wait_stable()
                    except Exception:
                        pass

                dom = self.browser.semantic_snapshot()
                dom_path = self.logger.save_dom(
                    self.state.iterations,
                    "before_plan",
                    dom,
                )
                self.logger.event(
                    "dom_captured",
                    iteration=self.state.iterations,
                    path=dom_path,
                )

                if DEBUG:
                    print("\n[DOM] Current semantic snapshot:")
                    print(dom)

                plan_args = dict(
                    task=task,
                    dom_snapshot=dom,
                    completed_checkpoints=self.state.completed_checkpoints,
                    event=self.state.last_event,
                    history=history[-HISTORY_WINDOW:],
                    conversation=prior,
                )
                try:
                    calls = self.planner.plan(**plan_args)
                except Exception:
                    # One retry for transient API/network trouble.
                    self.io.say("warn", "Planner call failed; retrying once…")
                    time.sleep(2)
                    calls = self.planner.plan(**plan_args)

                if self.io.should_stop():
                    return self._finish("stopped", STOPPED)

                batch_path = self.logger.save_batch(
                    self.state.iterations,
                    calls,
                    self.state.last_event,
                )
                self.logger.event(
                    "batch_planned",
                    iteration=self.state.iterations,
                    path=batch_path,
                    action_count=len(calls),
                )

                for call in calls:
                    if call["name"] not in ("checkpoint", "done", "ask_user", "request_human"):
                        self.io.say("step", describe_call(call))

                clarification = next(
                    (c for c in calls if c["name"] in ("ask_user", "request_human")),
                    None,
                )
                if clarification:
                    c_args = clarification.get("arguments") or {}
                    needs_human = clarification["name"] == "request_human"
                    kind = c_args.get("kind") or "other"
                    if needs_human:
                        reason = c_args.get("reason") or "I need your help to continue."
                        question = f"I need your help ({kind}): {reason}"
                        if kind == "seat_selection":
                            question += " Please pick the seats in the browser window, then reply 'done'."
                        att = getattr(self.io, "request_attention", None)
                        if att:
                            att(reason)
                    else:
                        question = c_args.get("question") or ""
                    self.logger.event(
                        "clarification_requested",
                        question=question,
                    )
                    answer = self.io.ask(question)
                    if answer is None or self.io.should_stop():
                        return self._finish("stopped", STOPPED)

                    history.append(f"#{self.state.iterations}: asked user “{question}” -> user answered")
                    if needs_human:
                        self.state.last_event = f"Human helped ({kind}): {answer}. Re-inspect the page."
                    else:
                        self.state.last_event = f"User clarification: {answer}"
                    self.logger.event(
                        "clarification_answered",
                        answer=answer,
                    )
                    self.logger.save_state(self.state)
                    continue

                done_call = next(
                    (c for c in calls if c["name"] == "done"),
                    None,
                )
                # done() only counts when it stands alone. Bundled with real
                # actions, those actions' outcome is still unknown.
                done_is_final = bool(done_call) and all(
                    c["name"] in ("checkpoint", "done")
                    for c in calls[:calls.index(done_call)]
                )

                # Stuck detection: identical batch on an unchanged page, or
                # A-B-A-B oscillation between two batches.
                try:
                    fp = self.browser.state_fingerprint()
                except Exception:
                    fp = ""
                signature = (
                    json.loads(dom).get("url"),
                    fp,
                    json.dumps(
                        [c for c in calls if c["name"] != "checkpoint"],
                        sort_keys=True,
                    ),
                )
                repeats = repeats + 1 if signature == last_signature else 0
                oscillating = (
                    len(recent) >= 3
                    and signature == recent[-2]
                    and recent[-1] == recent[-3]
                )
                recent.append(signature)
                last_signature = signature
                if repeats >= 4 or oscillating:
                    if helped >= 2:
                        return self._finish(
                            "failed",
                            "I got stuck repeating the same actions without the page changing, so I stopped.",
                        )
                    attention = getattr(self.io, "request_attention", None)
                    if attention:
                        attention("Agent is stuck and needs help")
                    answer = self.io.ask(
                        "I'm stuck: I keep repeating the same actions and the page isn't changing. "
                        "What should I do? (You can also do the step yourself in the browser and reply 'done'.)"
                    )
                    if answer is None or self.io.should_stop():
                        return self._finish("stopped", STOPPED)
                    if not answer.strip():
                        return self._finish(
                            "failed",
                            "I got stuck repeating the same actions without the page changing, so I stopped.",
                        )
                    helped += 1
                    history.append(f"#{self.state.iterations}: stuck -> user guidance: {answer}")
                    self.state.last_event = (
                        f"User guidance after getting stuck: {answer}. "
                        "Re-inspect the page and do something different."
                    )
                    repeats = 0
                    last_signature = None
                    recent.clear()
                    self.logger.save_state(self.state)
                    continue

                result = executor.execute(calls)

                # Only checkpoints whose call was actually reached are persisted.
                reached_calls = calls[:result.completed_actions]
                for call in reached_calls:
                    if call["name"] == "checkpoint":
                        checkpoint_id = call["arguments"]["id"]
                        description = call["arguments"]["description"]

                        if checkpoint_id not in self.state.completed_checkpoints:
                            self.state.completed_checkpoints.append(checkpoint_id)

                        checkpoint_path = self.logger.checkpoint(
                            checkpoint_id,
                            description,
                            self.state.iterations,
                        )
                        self.io.say("checkpoint", description)

                # Always capture post-batch state, successful or not.
                after_dom = self.browser.semantic_snapshot()
                after_path = self.logger.save_dom(
                    self.state.iterations,
                    "after_batch",
                    after_dom,
                )
                self.logger.event(
                    "dom_captured_after_batch",
                    iteration=self.state.iterations,
                    path=after_path,
                )

                if result.reason == STOPPED:
                    return self._finish("stopped", STOPPED)

                done_args = done_call["arguments"] if done_call else {}
                ran = [
                    describe_call(c)
                    for c in calls[:result.completed_actions]
                    if c["name"] not in ("checkpoint", "done")
                ]
                outcome = "ok" if result.success else f"interrupted ({result.reason})"
                try:
                    now_url = self.browser.current_page().url
                except Exception:
                    now_url = "?"
                history.append(
                    f"#{self.state.iterations}: {'; '.join(ran) or '(no browser actions)'} "
                    f"-> {outcome}; now on tab {self.browser.tab_id()} at {now_url[:120]}"
                )

                if result.success:
                    if done_call and done_is_final:
                        message = done_args.get("message", "Done.")

                        if not done_args.get("success", True):
                            self.io.say("warn", "Agent reports it could not complete the task.")
                            return self._finish("blocked", message)

                        verdict = self.planner.verify(
                            task, after_dom, history[-HISTORY_WINDOW:],
                            message, done_args.get("evidence", ""),
                        )
                        self.logger.event("completion_verified", **verdict)
                        if verdict["achieved"]:
                            return self._finish("done", message)

                        rejections += 1
                        if rejections > MAX_VERIFIER_REJECTIONS:
                            return self._finish(
                                "unverified",
                                f"{message}\n(I could not confirm this is complete: {verdict['reason']})",
                            )
                        self.io.say("warn", f"Not finished yet: {verdict['reason']}")
                        self.state.last_event = (
                            "Your done() was REJECTED by the verifier: "
                            f"{verdict['reason']} The task is NOT complete; keep working on it "
                            "(or call done(success=false) if it is truly impossible)."
                        )
                        self.logger.save_state(self.state)
                        continue

                    if done_call:
                        self.state.last_event = (
                            "You called done() together with other actions, so it was ignored. "
                            "Inspect the new state; if the goal is achieved call done() alone."
                        )
                        self.logger.save_state(self.state)
                        continue

                    self.state.last_event = (
                        f"Batch completed successfully. "
                        f"Completed checkpoints: "
                        f"{self.state.completed_checkpoints}"
                    )
                    if repeats >= 2:
                        self.state.last_event += (
                            f" WARNING: you have now sent the same actions {repeats + 1} times "
                            "and the page did not change. Try a different approach (check `dialogs`/in_popup elements first), use request_human, "
                            "or call done(success=false)."
                        )
                    self.logger.event(
                        "batch_completed",
                        iteration=self.state.iterations,
                        completed_actions=result.completed_actions,
                    )
                    self.logger.save_state(self.state)
                    continue

                self.state.last_event = (
                    f"Batch interrupted after {result.completed_actions} actions. "
                    f"Reason: {result.reason}. "
                    f"Failed action: {result.failed_action}"
                )
                self.logger.event(
                    "batch_interrupted",
                    iteration=self.state.iterations,
                    completed_actions=result.completed_actions,
                    reason=result.reason,
                    failed_action=result.failed_action,
                )

                screenshot_path = self.logger.screenshot(
                    self.browser,
                    self.state.iterations,
                    "interrupted",
                )
                self.logger.event(
                    "screenshot_captured",
                    iteration=self.state.iterations,
                    path=screenshot_path,
                )

                self.logger.save_state(self.state)
                self.io.say("warn", f"Replanning: {result.reason}")

            return self._finish(
                "failed",
                "I ran out of steps before finishing this task.",
            )

        finally:
            secret_store.close()
            # Actual values are destroyed with the temporary directory.


def list_runs(logs_dir: str = "logs") -> List[str]:
    base = Path(logs_dir)
    if not base.exists():
        return []
    return sorted(
        [p.name for p in base.iterdir() if p.is_dir()],
        reverse=True,
    )
