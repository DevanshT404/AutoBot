import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright


# Shared in-page helpers. Concatenated in front of each page script below.
_PAGE_HELPERS_JS = r"""
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
  };
  const clean = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n || 180);
  const inView = (el) => {
    const r = el.getBoundingClientRect();
    return r.bottom > 0 && r.top < innerHeight && r.right > 0 && r.left < innerWidth;
  };
  const isRoot = (n) => !n || n === document.body || n === document.documentElement;
  const isFixed = (el) => {
    for (let n = el; !isRoot(n); n = n.parentElement) {
      if (getComputedStyle(n).position === "fixed") return true;
    }
    return false;
  };

  // Popups / modals / bottom sheets. Many sites (BookMyShow included) build
  // these from plain fixed-position divs without role="dialog", so ARIA alone
  // misses them. Three sources: ARIA, popup-ish class names, and whatever
  // fixed layer is painted on top of the middle of the viewport.
  const findOverlays = () => {
    const found = new Set();
    document.querySelectorAll(
      '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]'
    ).forEach(el => found.add(el));

    const rx = /modal|popup|pop-up|overlay|bottomsheet|bottom-sheet|drawer|popover/i;
    let checked = 0;
    for (const el of document.querySelectorAll("[class], [id]")) {
      const cls = typeof el.className === "string" ? el.className : (el.getAttribute("class") || "");
      if (!rx.test(cls + " " + el.id)) continue;
      if (++checked > 400) break;
      if (isFixed(el)) found.add(el);
    }

    const pts = [[.5, .5], [.5, .3], [.5, .7], [.5, .88], [.3, .5], [.7, .5]];
    for (const [fx, fy] of pts) {
      const hit = document.elementFromPoint(innerWidth * fx, innerHeight * fy);
      let top = null;
      for (let n = hit; !isRoot(n); n = n.parentElement) {
        const s = getComputedStyle(n);
        if (s.position === "fixed" && (parseInt(s.zIndex, 10) || 0) >= 1) top = n;
      }
      if (top) found.add(top);
    }

    const list = Array.from(found).filter(el => {
      if (isRoot(el) || !visible(el) || !inView(el)) return false;
      const t = (el.innerText || "").trim();
      return t.length > 0 && t.length < 4000;
    });
    return list.filter(el => !list.some(o => o !== el && o.contains(el)));
  };

  // Seat maps (cinemas, flights, events). Returns null when there is none.
  const findSeats = (tag) => {
    if (tag) document.querySelectorAll("[data-ab-seat]").forEach(e => {
      e.removeAttribute("data-ab-seat");
      e.removeAttribute("data-ab-seat-status");
      e.removeAttribute("data-ab-seat-cat");
    });
    const sized = (el) => {
      const r = el.getBoundingClientRect();
      return r.width >= 6 && r.height >= 6 && r.width <= 70 && r.height <= 70;
    };
    let cands = Array.from(document.querySelectorAll(
      '[class*="seat" i], [id*="seat" i], [aria-label*="seat" i], [data-seat], [data-seat-id], [data-seatid]'
    )).filter(el => sized(el) && visible(el));
    if (cands.length < 4) {
      const rxL = /^[A-Z]{1,2}[-\s]?\d{1,3}$/;
      cands = Array.from(document.querySelectorAll("[aria-label], [title]")).filter(el =>
        rxL.test((el.getAttribute("aria-label") || el.getAttribute("title") || "").trim()) &&
        sized(el) && visible(el));
    }
    cands = cands.filter(el => !cands.some(o => o !== el && el.contains(o)));

    if (cands.length < 4) {
      const canvas = Array.from(document.querySelectorAll("canvas")).find(c => {
        const r = c.getBoundingClientRect();
        return r.width > 300 && r.height > 200 && visible(c);
      });
      if (canvas && document.body && /seat/i.test(document.body.innerText.slice(0, 20000))) {
        return { elements: new Set(), summary: { canvas_seat_map: true } };
      }
      return null;
    }

    let rowLabels = null;
    const rowOf = (el) => {
      if (rowLabels === null) {
        rowLabels = Array.from(document.querySelectorAll("div, span, td, li, p, b, strong"))
          .filter(e => e.childElementCount === 0 && /^[A-Z]{1,2}$/.test((e.textContent || "").trim()) && visible(e))
          .map(e => ({ t: e.textContent.trim(), r: e.getBoundingClientRect() }));
      }
      const r = el.getBoundingClientRect();
      const cy = r.top + r.height / 2;
      let best = null, bestX = -Infinity;
      for (const L of rowLabels) {
        const ly = L.r.top + L.r.height / 2;
        if (L.r.right <= r.left + 1 && Math.abs(ly - cy) < Math.max(6, r.height / 2) && L.r.right > bestX) {
          best = L.t; bestX = L.r.right;
        }
      }
      return best;
    };

    const labelOf = (el) => {
      const sources = [el, el.firstElementChild].filter(Boolean);
      for (const s of sources) {
        for (const a of ["aria-label", "title", "data-seat", "data-seat-id", "data-seatid", "id"]) {
          const v = (s.getAttribute(a) || "").trim();
          if (!v) continue;
          let m = v.match(/row\s*([A-Z]{1,2})\D{0,12}?(\d{1,3})/i);
          if (m) return m[1] + m[2];
          m = v.match(/\b([A-Z]{1,2})[-_ ]?(\d{1,3})\b/);
          if (m) return m[1] + m[2];
        }
      }
      const num = (el.innerText || "").trim();
      if (/^\d{1,3}$/.test(num)) {
        const row = rowOf(el);
        if (row) return row + num;
      }
      return null;
    };

    const statusOf = (el) => {
      const bits = [el, ...Array.from(el.querySelectorAll("*")).slice(0, 5)]
        .map(n => (n.getAttribute("class") || "") + " " + (n.getAttribute("aria-label") || "") + " " +
                  (n.getAttribute("title") || "") + " " + (n.getAttribute("alt") || "") + " " +
                  (n.getAttribute("src") || "").slice(-60))
        .join(" ");
      // Wheelchair / companion seats: never pick these unless asked (they open
      // an accessibility confirmation popup on BookMyShow and similar sites).
      const accessible = /wheel\s*chair|wheelchair|accessib|handicap|companion|disabled[-_ ]?(person|people|access|friendly|seat)|\bwc\b/i.test(bits);
      const soldBits = bits.replace(/disabled[-_ ]?(person|people|access|friendly|seat)/gi, "");
      if (el.disabled === true || el.getAttribute("aria-disabled") === "true" ||
          /sold|booked|unavailable|blocked|disabled|reserved|occupied/i.test(soldBits)) return "sold";
      if (accessible) return "accessible";
      if (el.getAttribute("aria-pressed") === "true" || el.getAttribute("aria-checked") === "true" ||
          /selected|active|chosen/i.test(bits)) return "selected";
      return "available";
    };

    const counts = {};
    const raw = cands.map(el => {
      let label = labelOf(el);
      label = label ? label.toUpperCase().replace(/[\s\-_]/g, "") : null;
      if (label && !/\d/.test(label)) label = null;
      if (label) counts[label] = (counts[label] || 0) + 1;
      return { el, label, status: statusOf(el) };
    });
    const seats = raw.filter(s => s.label && counts[s.label] === 1);
    if (seats.length < 4) return null;

    // Seat categories (RECLINER, PRIME, CLASSIC, ...): headers above blocks of rows.
    const catRx = /recliner|prime|classic|executive|premium|royal|gold|silver|platinum|club|sofa|lounge|balcony|insignia|luxe|₹\s*\d|rs\.?\s*\d/i;
    const headers = Array.from(document.querySelectorAll("div, span, p, h2, h3, h4, h5, td, li, b, strong"))
      .filter(e => e.childElementCount === 0 && visible(e) && !elements_has_seat(e))
      .map(e => ({ t: (e.textContent || "").replace(/\s+/g, " ").trim(), r: e.getBoundingClientRect() }))
      .filter(h => h.t && h.t.length <= 60 && catRx.test(h.t));
    function elements_has_seat(e) { return cands.some(c => c === e || c.contains(e)); }
    const catOf = (el) => {
      const top = el.getBoundingClientRect().top;
      let best = null, bestBottom = -Infinity;
      for (const h of headers) {
        if (h.r.bottom <= top + 2 && h.r.bottom > bestBottom) { best = h.t; bestBottom = h.r.bottom; }
      }
      return best;
    };

    const elements = new Set();
    const categories = {};
    for (const s of seats) {
      elements.add(s.el);
      s.cat = headers.length ? catOf(s.el) : null;
      if (tag) {
        s.el.setAttribute("data-ab-seat", s.label);
        s.el.setAttribute("data-ab-seat-status", s.status);
        if (s.cat) s.el.setAttribute("data-ab-seat-cat", s.cat);
      }
      if (s.cat) {
        const c = categories[s.cat] || (categories[s.cat] = {
          price: ((s.cat.match(/(?:₹|rs\.?)\s*([\d,]+)/i) || [])[1] || null),
          available_count: 0, avail: []
        });
        if (s.status === "available") { c.available_count++; c.avail.push(s.label); }
      }
    }
    for (const k of Object.keys(categories)) {
      categories[k].available = categories[k].avail.slice(0, 60).join(" ");
      delete categories[k].avail;
    }
    const avail = seats.filter(s => s.status === "available").map(s => s.label);
    return {
      elements,
      summary: {
        available_count: avail.length,
        sold_count: seats.filter(s => s.status === "sold").length,
        selected: seats.filter(s => s.status === "selected").map(s => s.label),
        available: avail.slice(0, 120).join(" "),
        categories: Object.keys(categories).length ? categories : null,
        accessible_do_not_pick: seats.filter(s => s.status === "accessible").map(s => s.label).slice(0, 30)
      }
    };
  };
"""

_SNAPSHOT_BODY_JS = r"""
  const MAX = args.max, TAG = args.tag;
  if (TAG) document.querySelectorAll("[data-ab-id]").forEach(e => e.removeAttribute("data-ab-id"));

  const overlays = findOverlays();
  const inOverlay = (el) => overlays.some(o => o.contains(el));
  const seatInfo = findSeats(TAG);
  const seatEls = seatInfo ? seatInfo.elements : new Set();
  const inSeat = (el) => {
    if (!seatEls.size) return false;
    for (let n = el; n; n = n.parentElement) if (seatEls.has(n)) return true;
    return false;
  };

  const BASE = 'button, input, textarea, select, a, [role], [contenteditable="true"], [onclick], [tabindex]:not([tabindex="-1"])';
  const set = new Set(document.querySelectorAll(BASE));
  const pointer = new Set();

  // Plain divs/spans with cursor:pointer are how many sites build buttons
  // (popup "Accept", "Select seats", quantity pickers...). Keep the outermost
  // pointer element that isn't already inside a real control.
  const addPointer = (root, limit, onlyInView) => {
    let n = 0;
    for (const el of root.querySelectorAll("div, span, li, p, img, svg, label, h1, h2, h3, h4, h5, h6")) {
      if (++n > limit) break;
      if (set.has(el)) continue;
      if (onlyInView && !inView(el)) continue;
      if (getComputedStyle(el).cursor !== "pointer") continue;
      const p = el.parentElement;
      if (p && getComputedStyle(p).cursor === "pointer" && (p.innerText || "").length <= 200) continue;
      if (p && p.closest(BASE)) continue;
      const tag = el.tagName.toLowerCase();
      if (tag !== "img" && tag !== "svg" && (el.innerText || "").length > 200) continue;
      set.add(el);
      pointer.add(el);
    }
  };
  overlays.forEach(o => addPointer(o, 1500, false));
  if (TAG && document.body) addPointer(document.body, 2500, true);

  const SKIP_ROLES = new Set(["dialog", "alertdialog", "presentation", "none", "main", "navigation",
                              "region", "banner", "contentinfo", "document", "application"]);
  const rank = (el) => inOverlay(el) ? 0 : (inView(el) ? 1 : 2);
  const candidates = Array.from(set).filter(el => visible(el) && !inSeat(el))
    .map(el => ({ el, k: rank(el) }))
    .sort((a, b) => (a.k - b.k) ||
      (a.el.compareDocumentPosition(b.el) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1));

  const inputRole = (el) => {
    const t = (el.getAttribute("type") || "text").toLowerCase();
    if (t === "checkbox" || t === "radio") return t;
    if (["button", "submit", "reset", "image"].includes(t)) return "button";
    if (t === "search") return "searchbox";
    if (t === "range") return "slider";
    return "textbox";
  };

  const items = [];
  let nextId = 1;
  for (const { el, k } of candidates) {
    const tag = el.tagName.toLowerCase();
    let role = el.getAttribute("role");
    if (role && SKIP_ROLES.has(role)) continue;
    if (!role) {
      role = tag === "button" ? "button" :
             tag === "a" ? "link" :
             tag === "input" ? inputRole(el) :
             tag === "textarea" ? "textbox" :
             tag === "select" ? "combobox" :
             (tag === "img" || tag === "svg") ? "img" :
             pointer.has(el) || el.hasAttribute("onclick") ? "clickable" : "generic";
    }

    let name = clean(el.getAttribute("aria-label"));
    if (!name && tag === "input" && inputRole(el) === "button") name = clean(el.value);
    if (!name) name = clean(el.innerText);
    if (!name && el.labels && el.labels.length) {
      name = clean(Array.from(el.labels).map(x => x.innerText).join(" "));
    }
    if (!name) name = clean(el.getAttribute("alt") || el.getAttribute("title"));
    if (!name && role === "generic") continue;

    const placeholder = clean(el.getAttribute("placeholder"));
    const type = clean(el.getAttribute("type"));
    const disabled = el.disabled === true || el.getAttribute("aria-disabled") === "true";

    const item = {
      role,
      name: name || null,
      placeholder: placeholder || null,
      type: type || null,
      disabled,
      in_popup: k === 0
    };
    if (TAG) {
      el.setAttribute("data-ab-id", String(nextId));
      item.id = nextId;
    }
    nextId++;
    items.push(item);
    if (items.length >= MAX) break;
  }

  const dialogs = overlays.map(o => clean(o.innerText, 400)).filter(Boolean).slice(0, 6);

  // Native media state is often absent from the accessibility tree,
  // especially on streaming sites with custom player controls.
  const media = Array.from(document.querySelectorAll("video, audio"))
    .filter(visible).slice(0, 4).map(el => ({
      kind: el.tagName.toLowerCase(),
      paused: el.paused,
      ended: el.ended,
      currentTime: Math.floor(el.currentTime || 0),
      duration: Number.isFinite(el.duration) ? Math.floor(el.duration) : null,
      readyState: el.readyState
    }));

  // CAPTCHA / human-verification challenge present? (handed to the user)
  const captcha = Array.from(document.querySelectorAll("iframe")).some(f =>
      /recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha/i.test(f.src || "")
      && visible(f)) ||
    /verify (that )?you are (a )?human|are you a robot|i'm not a robot|complete the security check/i
      .test(document.body ? document.body.innerText.slice(0, 5000) : "");

  return {
    url: location.href,
    title: document.title,
    elements: items,
    dialogs,
    media,
    captcha,
    seats: seatInfo ? seatInfo.summary : null
  };
"""

# Real ARIA roles that page.get_by_role() understands. The snapshot also emits
# pseudo-roles such as "clickable" (cursor:pointer / onclick divs); those never
# match get_by_role and must be resolved by their text instead.
_ARIA_ROLES = {
    "button", "link", "checkbox", "radio", "textbox", "combobox", "option",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch",
    "searchbox", "listbox", "gridcell", "cell", "row", "heading", "img",
    "slider", "spinbutton", "treeitem", "dialog",
}


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
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe"),
        # macOS
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
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
        # `page` is the ACTIVE tab: the only tab the agent observes/acts on.
        # It changes only when the agent switches/opens/closes a tab, when an
        # agent action opens a new tab, or when a task starts (we adopt the tab
        # the user is looking at). A tab the user opens meanwhile never hijacks it.
        self.page: Optional[Page] = None
        self._tab_ids: Dict[Page, int] = {}
        self._next_tab_id = 1
        self._new_pages: List[Page] = []
        self.notices: List[str] = []
        self._recoveries: Dict[str, Tuple[int, float]] = {}  # video id -> (tries, last time)

    def _find_executable(self) -> str:
        for command in ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"]:
            found = shutil.which(command)
            if found:
                return found

        for path in self.COMMON_CHROMIUM_PATHS:
            if path and Path(path).exists():
                return path

        env_path = os.environ.get("CHROMIUM_PATH")
        if env_path and Path(env_path).exists():
            return env_path

        # Fall back to Playwright's bundled Chromium (from `playwright install chromium`).
        return ""

    def start(self):
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)

        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=self.profile_dir,
            executable_path=self.executable_path or None,
            headless=False,
            # Don't announce "controlled by automation" (navigator.webdriver):
            # YouTube degrades/blocks playback for sessions that look scripted.
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
            viewport=None,
        )

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        for p in self.context.pages:
            self._register(p)
        self.context.on("page", self._on_new_page)

        print(f"[browser] Chromium: {self.executable_path}")
        print(f"[browser] Profile:  {self.profile_dir}")
        print(f"[browser] URL:      {self.page.url}")

    def stop(self):
        if self.context:
            self.context.close()
        if self._playwright:
            self._playwright.stop()

    # ------------------------------------------------------------------ tabs

    def _register(self, page: Page) -> int:
        if page not in self._tab_ids:
            self._tab_ids[page] = self._next_tab_id
            self._next_tab_id += 1
        return self._tab_ids[page]

    def _on_new_page(self, page: Page):
        self._register(page)
        self._new_pages.append(page)

    def live_pages(self) -> List[Page]:
        pages = [p for p in self.context.pages if not p.is_closed()]
        for p in pages:
            self._register(p)
        return pages

    def current_page(self) -> Page:
        if not self.context:
            raise RuntimeError("Browser is not started.")
        pages = self.live_pages()
        if not pages:
            self.page = self.context.new_page()
            self._register(self.page)
            self.notices.append("All tabs were closed; opened a fresh blank tab.")
        elif self.page is None or self.page not in pages:
            self.page = pages[-1]
            self.notices.append(
                f"The active tab was closed. Now working on tab {self._tab_ids[self.page]}."
            )
        return self.page

    def tab_id(self, page: Optional[Page] = None) -> int:
        return self._register(page or self.current_page())

    def tabs_info(self) -> List[Dict[str, Any]]:
        active = self.current_page()
        tabs = []
        for p in self.live_pages():
            try:
                title = p.title()
            except Exception:
                title = ""
            tabs.append({
                "id": self._tab_ids[p],
                "title": title[:80],
                "url": p.url[:200],
                "active": p is active,
            })
        return tabs

    def _find_tab(self, tab_id: int) -> Page:
        for p in self.live_pages():
            if self._tab_ids.get(p) == tab_id:
                return p
        known = [self._tab_ids[p] for p in self.live_pages()]
        raise ValueError(f"No open tab with id {tab_id}. Open tab ids: {known}")

    def switch_tab(self, tab_id: int):
        page = self._find_tab(tab_id)
        self.page = page
        page.bring_to_front()
        self.wait_stable()

    def open_tab(self, url: Optional[str] = None):
        page = self.context.new_page()
        self._register(page)
        self._new_pages = [p for p in self._new_pages if p is not page]
        self.page = page
        if url:
            page.goto(url, wait_until="domcontentloaded")
        page.bring_to_front()
        self.wait_stable()

    def close_tab(self, tab_id: int):
        page = self._find_tab(tab_id)
        was_active = page is self.page
        pages = self.live_pages()
        page.close()
        remaining = [p for p in pages if p is not page]
        if was_active and remaining:
            # Fall back to the tab that sat just before the closed one.
            idx = max(pages.index(page) - 1, 0)
            self.page = remaining[min(idx, len(remaining) - 1)]
            self.page.bring_to_front()
        self.current_page()

    def adopt_new_tab(self, wait_s: float = 0.0) -> Optional[str]:
        """
        If an agent action just opened a new tab (popup, target=_blank,
        window.open), make it the active tab and describe the change.
        """
        deadline = time.time() + wait_s
        while True:
            fresh = [p for p in self._new_pages if not p.is_closed()]
            if fresh or time.time() >= deadline:
                break
            self.current_page().wait_for_timeout(100)  # pumps Playwright events
        self._new_pages = []
        if not fresh:
            return None
        page = fresh[-1]
        self.page = page
        try:
            page.bring_to_front()
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        note = (
            f"A new tab opened and is now the active tab "
            f"(tab {self._tab_ids[page]}, {page.url[:120]})."
        )
        self.notices.append(note)
        return note

    def sync_to_foreground_tab(self):
        """At task start, work on the tab the user is actually looking at."""
        visible = []
        for p in self.live_pages():
            try:
                if p.evaluate("document.visibilityState") == "visible":
                    visible.append(p)
            except Exception:
                pass
        if visible and self.page not in visible:
            self.page = visible[0]

    def recover_playback_error(self) -> Optional[str]:
        """
        YouTube's player sometimes dies mid-video with "Something went wrong.
        Refresh or try again later." Reload the page at the same timestamp so
        playback resumes. Gives up after 3 consecutive attempts per video.
        """
        for page in self.live_pages():
            try:
                if "youtube.com/watch" not in page.url:
                    continue
                info = page.evaluate("""() => {
                    const player = document.querySelector('#movie_player') || document;
                    const e = Array.from(player.querySelectorAll('.ytp-error, [role="alert"]')).find(
                        el => el.offsetParent &&
                              /went wrong|error occurred|try again later/i.test(el.innerText || '')
                    );
                    if (!e) return null;
                    const t = document.querySelector('.ytp-time-current');
                    return {time: t ? t.innerText : ''};
                }""")
            except Exception:
                continue
            if not info:
                continue

            parsed = urlparse(page.url)
            query = parse_qs(parsed.query)
            video = (query.get("v") or [""])[0]
            tries, last = self._recoveries.get(video, (0, 0.0))
            now = time.time()
            if now - last > 120:
                tries = 0                      # it played fine for a while; fresh budget
            if tries >= 3 or now - last < 8:
                continue

            seconds = 0
            for part in (info.get("time") or "").strip().split(":"):
                seconds = seconds * 60 + (int(part) if part.isdigit() else 0)
            query["t"] = [f"{seconds}s"]
            url = urlunparse(parsed._replace(query=urlencode(query, doseq=True)))

            self._recoveries[video] = (tries + 1, now)
            try:
                page.goto(url, wait_until="domcontentloaded")
                # `t=` is ignored for playlist/mix URLs and autoplay is blocked
                # after a reload, so seek and press play ourselves.
                page.wait_for_function(
                    "() => { const v = document.querySelector('video'); return !!v && v.readyState >= 2; }",
                    timeout=10000,
                )
                page.wait_for_timeout(800)
                page.evaluate(
                    "s => { const v = document.querySelector('video'); if (v) v.currentTime = s; }",
                    seconds,
                )
                if page.evaluate("() => { const v = document.querySelector('video'); return !!v && v.paused; }"):
                    page.click(".ytp-play-button", timeout=3000)
            except Exception:
                pass
            return (
                f"YouTube hit a playback error; reloaded the video and resumed at "
                f"{info.get('time') or '0:00'} (attempt {tries + 1}/3)."
            )
        return None

    def drain_notices(self) -> List[str]:
        notes, self.notices = self.notices, []
        return notes

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

    def semantic_snapshot(self, max_elements: int = 120, include_tabs: bool = True, tag: bool = True) -> str:
        """
        Compact JSON view of the page for the planner.

        Elements inside popups / modals / bottom sheets (ARIA dialogs AND the
        unlabelled fixed-position overlays sites like BookMyShow use) come
        first and carry in_popup=true. With tag=True every listed element gets
        a stable data-ab-id so the planner can target it as
        css '[data-ab-id="N"]'. Seat maps are summarised under "seats".
        """
        page = self.current_page()
        script = "(args) => {" + _PAGE_HELPERS_JS + _SNAPSHOT_BODY_JS + "}"
        data = page.evaluate(script, {"max": max_elements, "tag": bool(tag)})

        # Compact for the LLM: drop null/false fields and empty sections.
        elements = [
            {k: v for k, v in el.items() if v not in (None, False)}
            for el in data["elements"]
        ]
        out: Dict[str, Any] = {"active_tab": self.tab_id(page), "url": data["url"], "title": data["title"]}
        if include_tabs:
            out["tabs"] = self.tabs_info()
        if data.get("dialogs"):
            out["dialogs"] = data["dialogs"]
        if data.get("seats"):
            out["seats"] = data["seats"]
        out["elements"] = elements
        if data.get("media"):
            out["media"] = data["media"]
        if data.get("captcha"):
            out["captcha_challenge_detected"] = True
        return json.dumps(out, ensure_ascii=False, separators=(",", ":"))

    def state_fingerprint(self) -> str:
        # tag=False: don't touch the DOM (and ids) just to fingerprint it.
        snapshot = self.semantic_snapshot(max_elements=80, include_tabs=False, tag=False)
        return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()

    def dialog_signature(self) -> str:
        page = self.current_page()
        script = (
            "() => {" + _PAGE_HELPERS_JS
            + "return findOverlays().map(o => clean(o.innerText, 500)).filter(Boolean).sort().join(' || '); }"
        )
        return page.evaluate(script)

    def resolve_target(self, target: Dict[str, Any]):
        """
        Resolve a semantic target into a Playwright Locator.
        Preferred order:
          1) CSS selector
          2) ARIA role + accessible name
          3) label / aria-label
          4) placeholder
          5) visible text
          6) name used as visible text (custom "clickable" divs, wrong roles)
        """
        page = self.current_page()

        # The model often sends null for index/exact; treat that as "default".
        raw_index = target.get("index")
        index = 0 if raw_index is None else int(raw_index)
        raw_exact = target.get("exact")
        exact_modes = [True, False] if raw_exact in (None, True) else [False]

        def attempt(make_locator):
            try:
                return self._visible_nth(make_locator(), index)
            except Exception:
                return None

        css = target.get("css")
        if css:
            hit = attempt(lambda: page.locator(css))
            if hit is not None:
                return hit

        role = target.get("role")
        name = target.get("name")
        if role and str(role).lower() in _ARIA_ROLES:
            # Exact name first, then substring: the snapshot truncates long
            # names and its names don't always equal the accessible name.
            for exact in exact_modes:
                hit = attempt(lambda: page.get_by_role(role, name=name or None, exact=exact))
                if hit is not None:
                    return hit

        if name:
            # Role guesses are often off (e.g. Gmail's "To" field is a combobox).
            # Fall back to the label/aria-label regardless of role.
            for exact in exact_modes:
                hit = attempt(lambda: page.get_by_label(name, exact=exact))
                if hit is not None:
                    return hit
            safe = name.replace("\\", "\\\\").replace('"', '\\"')
            hit = attempt(lambda: page.locator(f'[aria-label="{safe}"]'))
            if hit is not None:
                return hit

        placeholder = target.get("placeholder")
        if placeholder:
            for exact in exact_modes:
                hit = attempt(lambda: page.get_by_placeholder(placeholder, exact=exact))
                if hit is not None:
                    return hit

        text = target.get("text")
        if text:
            for exact in exact_modes:
                hit = attempt(lambda: page.get_by_text(text, exact=exact))
                if hit is not None:
                    return hit

        if name:
            # Custom-designed buttons (div/span with cursor:pointer) have no ARIA
            # role, so the snapshot name is just their visible text.
            for exact in exact_modes:
                hit = attempt(lambda: page.get_by_text(name, exact=exact))
                if hit is not None:
                    return hit
            # Composite names are joined from child nodes and may differ in
            # whitespace ("Thu 26 Nov 40% off" vs "Thu26 Nov40% off").
            words = name.split()
            if words:
                pattern = re.compile(r"\s*".join(map(re.escape, words)), re.I)
                hit = attempt(lambda: page.get_by_text(pattern))
                if hit is not None:
                    return hit

        return None

    @staticmethod
    def _visible_nth(locator, index: int = 0):
        """
        The index-th VISIBLE match. Sites like Gmail keep hidden duplicates of
        their fields earlier in the DOM, so a plain .first/.nth(0) can point at
        an invisible copy. Returns None when nothing visible matches.
        """
        total = min(locator.count(), 40)
        visible = []
        for i in range(total):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    visible.append(candidate)
            except Exception:
                continue
            if len(visible) > index:
                break
        if not visible:
            return None
        return visible[index] if index < len(visible) else visible[-1]

    def target_status(self, target: Dict[str, Any]) -> Optional[str]:
        """None when the target can be acted on, otherwise why it can't."""
        locator = self.resolve_target(target)
        if locator is None:
            return "not found on the page"

        try:
            locator.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        try:
            if not locator.is_visible():
                return "hidden"
            if not locator.is_enabled():
                return "disabled"
        except Exception as exc:
            return f"could not be inspected ({exc.__class__.__name__})"
        return None

    def target_available(self, target: Dict[str, Any]) -> bool:
        return self.target_status(target) is None

    def visible_dialogs(self) -> List[str]:
        signature = self.dialog_signature()
        return [x for x in signature.split(" || ") if x.strip()]

    def goto(self, url: str):
        self.current_page().goto(url, wait_until="domcontentloaded")
        self.wait_stable()

    def click_target(self, target: Dict[str, Any]):
        """Click once. New tabs are adopted afterwards by the executor via adopt_new_tab()."""
        self.current_page()
        self._new_pages = []
        locator = self.resolve_target(target)
        if locator is None:
            raise RuntimeError(f"Click target not found: {target}")
        locator.click()

    def seat_candidates(self):
        page = self.current_page()
        return page.evaluate("() => {" + _PAGE_HELPERS_JS + "const s = findSeats(true); return s ? s.summary : null; }")

    def click_seat(self, seat: str):
        """Click a seat by its row+number label (e.g. "H12"), using the tags findSeats() put on the seat map."""
        page = self.current_page()
        label = re.sub(r"[\s\-_]", "", str(seat)).upper()
        summary = self.seat_candidates() or {}
        loc = self._visible_nth(page.locator(f'[data-ab-seat="{label}"]'))
        if loc is not None and (loc.get_attribute("data-ab-seat-status") or "") == "sold":
            raise RuntimeError(f"Seat {seat} is sold/unavailable")
        if loc is not None and (loc.get_attribute("data-ab-seat-status") or "") == "accessible":
            raise RuntimeError(f"Seat {seat} is a wheelchair/accessible seat; pick a regular available seat from seats.available or the requested category")
        if loc is None:
            for cand in (
                page.get_by_role("button", name=seat, exact=True),
                page.get_by_label(seat, exact=True),
                page.locator(f'[aria-label*="{seat}" i]'),
                page.get_by_title(seat, exact=True),
            ):
                loc = self._visible_nth(cand)
                if loc is not None:
                    break
        if loc is None:
            raise RuntimeError(f"Seat {seat} not found. Available: {' '.join(str(summary.get('available') or '').split()[:40])}")
        self._new_pages = []
        loc.click()

    def screenshot(self, path: str):
        self.current_page().screenshot(path=path, full_page=False)
