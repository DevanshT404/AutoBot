import json
import os
import re
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

from actions import ActionExecutor


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
    ):
        self.browser = browser
        self.planner = planner
        self.logs_dir = logs_dir
        self.resume_run_id = run_id
        self.state: Optional[AgentState] = None
        self.logger: Optional[FileLogger] = None

    def run(self, task: str, secrets: Dict[str, str]) -> str:
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
        executor = ActionExecutor(self.browser, secret_store)

        try:
            for _ in range(40):
                self.state.iterations += 1
                self.logger.save_state(self.state)

                self.browser.wait_stable()

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

                print("\n[DOM] Current semantic snapshot:")
                print(dom)

                calls = self.planner.plan(
                    task=task,
                    dom_snapshot=dom,
                    completed_checkpoints=self.state.completed_checkpoints,
                    event=self.state.last_event,
                )

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

                print("\n[PLAN]")
                for i, call in enumerate(calls, start=1):
                    print(f"  {i}. {call['name']} {call.get('arguments', {})}")

                clarification = next(
                    (c for c in calls if c["name"] == "ask_user"),
                    None,
                )
                if clarification:
                    question = clarification["arguments"]["question"]
                    self.logger.event(
                        "clarification_requested",
                        question=question,
                    )
                    print(f"\nAgent needs you: {question}")
                    answer = input("You> ").strip()

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
                        print(
                            f"[checkpoint] {checkpoint_id} -> {checkpoint_path}"
                        )

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

                if result.success:
                    if done_call:
                        self.state.status = "done"
                        self.state.last_event = done_call["arguments"]["message"]
                        self.logger.event(
                            "task_completed",
                            message=self.state.last_event,
                        )
                        self.logger.save_state(self.state)
                        return (
                            f"{self.state.last_event}\n"
                            f"Run logs: {self.logger.run_dir}"
                        )

                    self.state.last_event = (
                        f"Batch completed successfully. "
                        f"Completed checkpoints: "
                        f"{self.state.completed_checkpoints}"
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
                print(f"[replan] {self.state.last_event}")
                print(f"[logs] {self.logger.run_dir}")

            self.state.status = "failed"
            self.state.last_event = "Iteration limit reached."
            self.logger.event(
                "task_failed",
                reason=self.state.last_event,
            )
            self.logger.save_state(self.state)
            return f"{self.state.last_event}\nRun logs: {self.logger.run_dir}"

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
