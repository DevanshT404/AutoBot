import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright


class BrowserController:
    """
    Owns Chromium and all browser-side observation.
    The LLM never sees raw HTML. It receives a compact semantic DOM snapshot.
    """

    COMMON_CHROMIUM_PATHS = [
        # Windows
        os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Chromium\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Chromium\Application\chrome.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Google\Chrome\Application\chrome.exe"),
        # Linux
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]

    def __init__(self, executable_path: Optional[str], profile_dir: str):
        self.executable_path = executable_path or self._find_executable()
        self.profile_dir = str(Path(profile_dir).resolve())

        self._playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    def _find_executable(self) -> str:
        for command in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]:
            found = shutil.which(command)
            if found:
                return found

        for path in self.COMMON_CHROMIUM_PATHS:
            if path and Path(path).exists():
                return path

        raise FileNotFoundError(
            "Could not find Chromium automatically. "
            "Run with --browser-path \"C:\\\\path\\\\to\\\\chrome.exe\" "
            "or set CHROMIUM_PATH."
        )

    def start(self):
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            executable_path=self.executable_path,
            headless=False,
            args=["--start-maximized"],
            viewport=None,
        )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

        print(f"[browser] Chromium: {self.executable_path}")
        print(f"[browser] Profile:  {self.profile_dir}")
        print(f"[browser] URL:      {self.page.url}")

    def stop(self):
        if self.context:
            self.context.close()
        if self._playwright:
            self._playwright.stop()

    def current_page(self) -> Page:
        if not self.page:
            raise RuntimeError("Browser is not started.")
        return self.page

    def wait_stable(self, timeout_ms: int = 1800):
        """
        Wait until the semantic page fingerprint stops changing briefly.
        This is deliberately lightweight: no LLM call and no network-idle dependency.
        """
        page = self.current_page()
        deadline = time.time() + timeout_ms / 1000
        previous = self.state_fingerprint()

        while time.time() < deadline:
            time.sleep(0.15)
            current = self.state_fingerprint()
            if current == previous:
                return
            previous = current

    def semantic_snapshot(self, max_elements: int = 120) -> str:
        page = self.current_page()

        script = """
        () => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 &&
                   s.visibility !== "hidden" &&
                   s.display !== "none" &&
                   s.opacity !== "0";
          };

          const clean = (s) => (s || "").replace(/\\s+/g, " ").trim().slice(0, 180);

          const candidates = Array.from(document.querySelectorAll(
            'button, input, textarea, select, a, [role], [contenteditable="true"]'
          )).filter(visible);

          const items = [];
          for (const el of candidates) {
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute("role") || (
              tag === "button" ? "button" :
              tag === "a" ? "link" :
              tag === "input" ? "textbox" :
              tag === "textarea" ? "textbox" :
              tag === "select" ? "combobox" : "generic"
            );

            let name = clean(el.getAttribute("aria-label"));
            if (!name) name = clean(el.innerText);
            if (!name && el.labels && el.labels.length) {
              name = clean(Array.from(el.labels).map(x => x.innerText).join(" "));
            }

            const placeholder = clean(el.getAttribute("placeholder"));
            const type = clean(el.getAttribute("type"));
            const disabled = el.disabled === true || el.getAttribute("aria-disabled") === "true";

            items.push({
              role,
              name: name || null,
              placeholder: placeholder || null,
              type: type || null,
              disabled
            });

            if (items.length >= %d) break;
          }

          const dialogs = Array.from(
            document.querySelectorAll('[role="dialog"], [role="alertdialog"]')
          ).filter(visible).map(d => clean(d.innerText)).filter(Boolean).slice(0, 8);

          return {
            url: location.href,
            title: document.title,
            elements: items,
            dialogs
          };
        }
        """ % max_elements

        data = page.evaluate(script)
        return json.dumps(data, ensure_ascii=False, indent=2)

    def state_fingerprint(self) -> str:
        snapshot = self.semantic_snapshot(max_elements=80)
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def dialog_signature(self) -> str:
        page = self.current_page()

        script = """
        () => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 &&
                   s.visibility !== "hidden" &&
                   s.display !== "none" &&
                   s.opacity !== "0";
          };
          const clean = (s) => (s || "").replace(/\\s+/g, " ").trim().slice(0, 500);

          return Array.from(
            document.querySelectorAll('[role="dialog"], [role="alertdialog"]')
          ).filter(visible).map(d => clean(d.innerText)).sort().join(" || ");
        }
        """
        return page.evaluate(script)

    def resolve_target(self, target: Dict[str, Any]):
        """
        Resolve a semantic target into a Playwright Locator.
        Preferred order:
          1) CSS selector
          2) role + accessible name
          3) placeholder
          4) visible text
        """
        page = self.current_page()

        css = target.get("css")
        if css:
            locator = page.locator(css)
            if locator.count() > 0:
                return locator.first

        role = target.get("role")
        name = target.get("name")

        if role:
            role_locator = page.get_by_role(
                role,
                name=name if name else None,
                exact=bool(target.get("exact", True)),
            )
            if role_locator.count() > 0:
                index = int(target.get("index", 0))
                return role_locator.nth(index)

        placeholder = target.get("placeholder")
        if placeholder:
            locator = page.get_by_placeholder(placeholder, exact=bool(target.get("exact", True)))
            if locator.count() > 0:
                return locator.first

        text = target.get("text")
        if text:
            locator = page.get_by_text(text, exact=bool(target.get("exact", True)))
            if locator.count() > 0:
                return locator.first

        return None

    def target_available(self, target: Dict[str, Any]) -> bool:
        locator = self.resolve_target(target)
        if locator is None:
            return False

        try:
            return locator.is_visible() and locator.is_enabled()
        except Exception:
            return False

    def visible_dialogs(self) -> List[str]:
        signature = self.dialog_signature()
        return [x for x in signature.split(" || ") if x.strip()]

    def goto(self, url: str):
        self.current_page().goto(url, wait_until="domcontentloaded")
        self.wait_stable()

    def screenshot(self, path: str):
        self.current_page().screenshot(path=path, full_page=False)
