import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ExecutionResult:
    success: bool
    completed_actions: int
    failed_action: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None
    interrupted: bool = False


class ActionExecutor:
    """
    Executes one model-produced batch without making model calls.
    The browser layer checks each next target before running it.
    """

    def __init__(self, browser, secret_store):
        self.browser = browser
        self.secret_store = secret_store

    def _resolve_text(self, text: str) -> Tuple[str, bool]:
        if not isinstance(text, str):
            return str(text), False

        if text.startswith("@") and self.secret_store.has(text[1:]):
            return self.secret_store.get(text[1:]), True

        return text, False

    def execute(self, calls: List[Dict[str, Any]]) -> ExecutionResult:
        completed = 0
        baseline_dialog = self.browser.dialog_signature()

        for index, call in enumerate(calls):
            name = call["name"]
            args = call.get("arguments", {})

            if name == "checkpoint":
                # The agent handles checkpoint bookkeeping outside this executor.
                completed += 1
                continue

            if name in {"ask_user", "done"}:
                return ExecutionResult(
                    success=True,
                    completed_actions=completed,
                )

            try:
                if name == "click":
                    target = args["target"]
                    if not self.browser.target_available(target):
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=call,
                            reason="Target is not visible/enabled anymore.",
                            interrupted=True,
                        )
                    self.browser.resolve_target(target).click()

                elif name == "type":
                    target = args["target"]
                    if not self.browser.target_available(target):
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=call,
                            reason="Typing target is not visible/enabled anymore.",
                            interrupted=True,
                        )

                    text, secret = self._resolve_text(args["text"])
                    locator = self.browser.resolve_target(target)
                    locator.fill(text)

                    # Never print the actual value, even in debug mode.
                    if secret:
                        print("[action] type <secret>")
                    else:
                        print(f"[action] type {text!r}")

                elif name == "press":
                    target = args.get("target")
                    key = args["key"]

                    if target and not self.browser.target_available(target):
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=call,
                            reason="Press target is not available.",
                            interrupted=True,
                        )

                    if target:
                        self.browser.resolve_target(target).press(key)
                    else:
                        self.browser.current_page().keyboard.press(key)

                elif name == "scroll":
                    direction = args.get("direction", "down")
                    amount = int(args.get("amount", 650))
                    delta = amount if direction == "down" else -amount
                    self.browser.current_page().mouse.wheel(0, delta)

                elif name == "wait":
                    seconds = min(max(float(args.get("seconds", 0.5)), 0), 5)
                    time.sleep(seconds)

                elif name == "navigate":
                    self.browser.goto(args["url"])

                else:
                    return ExecutionResult(
                        success=False,
                        completed_actions=completed,
                        failed_action=call,
                        reason=f"Unknown action: {name}",
                    )

                completed += 1

                # After every browser action, allow the page to settle.
                self.browser.wait_stable()

                # If a genuinely NEW dialog appeared and the next action is not
                # explicitly targeting that dialog, stop the batch and replan.
                new_dialog = self.browser.dialog_signature()
                if (
                    new_dialog
                    and new_dialog != baseline_dialog
                    and index + 1 < len(calls)
                    and calls[index + 1]["name"] in {
                        "click", "type", "press"
                    }
                ):
                    next_target = calls[index + 1].get("arguments", {}).get("target")
                    if next_target and not self._target_inside_dialog(next_target):
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=calls[index + 1],
                            reason="A new modal/dialog appeared, invalidating the remaining batch.",
                            interrupted=True,
                        )

            except Exception as exc:
                return ExecutionResult(
                    success=False,
                    completed_actions=completed,
                    failed_action=call,
                    reason=f"{type(exc).__name__}: {exc}",
                    interrupted=True,
                )

        return ExecutionResult(
            success=True,
            completed_actions=completed,
        )

    def _target_inside_dialog(self, target: Dict[str, Any]) -> bool:
        """
        Conservative demo heuristic.
        If the target is one of the currently visible dialog elements, the
        batch is allowed to continue. Otherwise we replan.
        """
        role = target.get("role")
        name = target.get("name")
        if not role or not name:
            return False

        page = self.browser.current_page()
        try:
            dialogs = page.locator('[role="dialog"], [role="alertdialog"]')
            for i in range(dialogs.count()):
                dialog = dialogs.nth(i)
                if not dialog.is_visible():
                    continue

                candidate = dialog.get_by_role(role, name=name, exact=True)
                if candidate.count() > 0 and candidate.first.is_visible():
                    return True
        except Exception:
            pass

        return False
