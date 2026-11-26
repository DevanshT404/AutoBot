import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

STOPPED = "Stopped by user."
TAB_ACTIONS = {"switch_tab", "open_tab", "close_tab"}
NON_BROWSER = {"checkpoint", "done", "ask_user", "request_human"}

# Reset the window and every scrolled container back to the top. Used when a
# page auto-scrolls to its bottom on load and hides the pickers above.
SCROLL_TOP_JS = """() => {
    window.scrollTo(0, 0);
    for (const n of document.querySelectorAll('*')) {
        if (n.scrollTop > 0) {
            const o = getComputedStyle(n).overflowY;
            if (/(auto|scroll|overlay)/.test(o)) n.scrollTop = 0;
        }
    }
}"""


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

    def __init__(self, browser, secret_store, should_stop: Optional[Callable[[], bool]] = None):
        self.browser = browser
        self.secret_store = secret_store
        self.should_stop = should_stop or (lambda: False)

    @staticmethod
    def _describe(target: Any) -> str:
        if not isinstance(target, dict):
            return repr(target)
        role = target.get("role")
        label = target.get("name") or target.get("text") or target.get("css")
        if role and label:
            return f"{role} {label!r}"
        return repr(label or target)

    @staticmethod
    def _more_actions(calls: List[Dict[str, Any]], index: int) -> bool:
        return any(c["name"] not in NON_BROWSER for c in calls[index + 1:])

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

            if self.should_stop():
                return ExecutionResult(
                    success=False,
                    completed_actions=completed,
                    failed_action=call,
                    reason=STOPPED,
                    interrupted=True,
                )

            if name == "checkpoint":
                # The agent handles checkpoint bookkeeping outside this executor.
                completed += 1
                continue

            if name in {"ask_user", "done", "request_human"}:
                return ExecutionResult(
                    success=True,
                    completed_actions=completed,
                )

            try:
                if name == "click":
                    target = args["target"]
                    status = self.browser.target_status(target)
                    if status:
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=call,
                            reason=f"Click target {self._describe(target)} is {status}.",
                            interrupted=True,
                        )
                    self.browser.click_target(target)

                elif name == "type":
                    target = args["target"]
                    status = self.browser.target_status(target)
                    if status:
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=call,
                            reason=f"Typing target {self._describe(target)} is {status}.",
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
                    # A target dict with only empty values means "no target":
                    # press the key on the page instead of failing the precheck.
                    has_locator = isinstance(target, dict) and any(v for v in target.values())

                    status = self.browser.target_status(target) if has_locator else None
                    if status:
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=call,
                            reason=f"Press target {self._describe(target)} is {status}.",
                            interrupted=True,
                        )

                    if has_locator:
                        self.browser.resolve_target(target).press(key)
                    else:
                        self.browser.current_page().keyboard.press(key)

                elif name == "scroll" and args.get("direction") == "top":
                    self.browser.current_page().evaluate(SCROLL_TOP_JS)

                elif name == "scroll":
                    direction = args.get("direction", "down")
                    amount = int(args.get("amount", 650) or 650)
                    dx, dy = {
                        "down": (0, amount),
                        "up": (0, -amount),
                        "right": (amount, 0),
                        "left": (-amount, 0),
                    }.get(direction, (0, amount))
                    raw = args.get("target")
                    if isinstance(raw, str):
                        target = {"css": raw.strip()} if raw.strip() else None
                    elif isinstance(raw, dict) and any(v for v in raw.values()):
                        target = raw
                    else:
                        target = None
                    page = self.browser.current_page()
                    locator = self.browser.resolve_target(target) if target else None
                    if locator is None:
                        page.mouse.wheel(dx, dy)
                    else:
                        # Scroll the nearest scrollable ancestor in the requested axis
                        # (carousels / date & time-slot strips have no page scrollbar).
                        moved = locator.evaluate(
                            """(el, [dx, dy]) => {
                                const horiz = dx !== 0; let n = el;
                                while (n && n !== document.documentElement) {
                                    const cs = getComputedStyle(n);
                                    const ov = horiz ? cs.overflowX : cs.overflowY;
                                    const can = horiz ? n.scrollWidth > n.clientWidth + 1
                                                      : n.scrollHeight > n.clientHeight + 1;
                                    if (can && /(auto|scroll|overlay)/.test(ov)) {
                                        const before = horiz ? n.scrollLeft : n.scrollTop;
                                        n.scrollBy({left: dx, top: dy, behavior: 'instant'});
                                        const after = horiz ? n.scrollLeft : n.scrollTop;
                                        if (after !== before) return true;
                                    }
                                    n = n.parentElement;
                                }
                                return false;
                            }""",
                            [dx, dy],
                        )
                        if not moved:
                            # Fallback: emulate trackpad / shift+wheel over the element.
                            try:
                                locator.hover(timeout=2000)
                            except Exception:
                                pass
                            page.mouse.wheel(dx, dy)
                            if dx:
                                page.keyboard.down("Shift")
                                try:
                                    page.mouse.wheel(0, dx)
                                finally:
                                    page.keyboard.up("Shift")

                elif name == "wait":
                    seconds = min(max(float(args.get("seconds", 0.5)), 0), 5)
                    time.sleep(seconds)

                elif name == "navigate":
                    self.browser.goto(args["url"])

                elif name == "switch_tab":
                    self.browser.switch_tab(int(args["tab_id"]))

                elif name == "open_tab":
                    self.browser.open_tab(args.get("url") or None)

                elif name == "close_tab":
                    self.browser.close_tab(int(args["tab_id"]))

                elif name == "click_seat":
                    self.browser.click_seat(str(args["seat"]))

                else:
                    return ExecutionResult(
                        success=False,
                        completed_actions=completed,
                        failed_action=call,
                        reason=f"Unknown action: {name}",
                    )

                completed += 1

                # An action may have opened a new tab (link target=_blank,
                # popup, window.open). Follow it, and replan: the rest of the
                # batch was written for the previous tab.
                new_tab_note = self.browser.adopt_new_tab(
                    0.5 if name in {"click", "press", "click_seat"} else 0.0
                )
                if new_tab_note and self._more_actions(calls, index):
                    self.browser.wait_stable()
                    return ExecutionResult(
                        success=False,
                        completed_actions=completed,
                        failed_action=calls[index + 1],
                        reason=new_tab_note,
                        interrupted=True,
                    )

                if name in TAB_ACTIONS and self._more_actions(calls, index):
                    return ExecutionResult(
                        success=False,
                        completed_actions=completed,
                        failed_action=calls[index + 1],
                        reason="The active tab changed; remaining actions were planned for a different tab.",
                        interrupted=True,
                    )

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
                        "click", "type", "press", "click_seat"
                    }
                ):
                    next_args = calls[index + 1].get("arguments", {})
                    next_target = next_args.get("target")
                    if calls[index + 1]["name"] == "click_seat":
                        # The seat map was planned before this popup opened
                        # (e.g. "how many seats?" sheet): replan.
                        return ExecutionResult(
                            success=False,
                            completed_actions=completed,
                            failed_action=calls[index + 1],
                            reason="A popup appeared before seat selection; replan using the popup.",
                            interrupted=True,
                        )
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
        page = self.browser.current_page()

        # Snapshot-tagged targets: ask the page whether the element now sits
        # inside one of the visible overlays (ARIA or fixed-position popups).
        css = target.get("css") or ""
        if "data-ab-id" in css or "data-ab-seat" in css:
            try:
                from browser import _PAGE_HELPERS_JS  # shared page helpers
                script = (
                    "(sel) => {" + _PAGE_HELPERS_JS
                    + "const el = document.querySelector(sel); if (!el) return false;"
                    + "return findOverlays().some(o => o.contains(el)); }"
                )
                return bool(page.evaluate(script, css))
            except Exception:
                return False

        role = target.get("role")
        name = target.get("name")
        if not role or not name:
            return False

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
