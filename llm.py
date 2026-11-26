import json
import os
from typing import Any, Dict, List, Optional

from openai import OpenAI


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click a visible interactive element.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "object",
                        "properties": {
                            "role": {"type": ["string", "null"]},
                            "name": {"type": ["string", "null"]},
                            "placeholder": {"type": ["string", "null"]},
                            "text": {"type": ["string", "null"]},
                            "css": {"type": ["string", "null"]},
                            "index": {"type": "integer"},
                            "exact": {"type": "boolean"},
                        },
                        "required": [
                            "role", "name", "placeholder", "text",
                            "css", "index", "exact"
                        ],
                        "additionalProperties": False,
                    }
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": (
                "Fill a textbox. For sensitive/local values use @username, "
                "@password, or another @variable handle exactly as provided; "
                "NEVER invent or request the actual secret value in the tool call."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "object",
                        "properties": {
                            "role": {"type": ["string", "null"]},
                            "name": {"type": ["string", "null"]},
                            "placeholder": {"type": ["string", "null"]},
                            "text": {"type": ["string", "null"]},
                            "css": {"type": ["string", "null"]},
                            "index": {"type": "integer"},
                            "exact": {"type": "boolean"},
                        },
                        "required": [
                            "role", "name", "placeholder", "text",
                            "css", "index", "exact"
                        ],
                        "additionalProperties": False,
                    },
                    "text": {"type": "string"},
                },
                "required": ["target", "text"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press",
            "description": "Press a keyboard key, optionally in a target element.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "target": {
                        "type": ["object", "null"],
                        "properties": {
                            "role": {"type": ["string", "null"]},
                            "name": {"type": ["string", "null"]},
                            "placeholder": {"type": ["string", "null"]},
                            "text": {"type": ["string", "null"]},
                            "css": {"type": ["string", "null"]},
                            "index": {"type": "integer"},
                            "exact": {"type": "boolean"},
                        },
                        "required": [
                            "role", "name", "placeholder", "text",
                            "css", "index", "exact"
                        ],
                        "additionalProperties": False,
                    },
                },
                "required": ["key", "target"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": (
                "Scroll the page, or a specific scrollable element/carousel "
                "(e.g. horizontal date/time-slot strips) up/down/left/right, "
                "or direction='top' to jump back to the very top of the page "
                "(amount and target ignored). "
                "target='' scrolls the whole page; otherwise target = the css "
                "selector of an element inside the strip (from the snapshot)."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right", "top"]},
                    "amount": {"type": "integer"},
                    "target": {"type": "string"},
                },
                "required": ["direction", "amount", "target"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait briefly for a page transition or UI animation.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {"type": "number"},
                },
                "required": ["seconds"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Navigate directly to a URL when appropriate.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "checkpoint",
            "description": (
                "Mark that a logical subtask has been completed. "
                "Use a short stable id such as 'instagram_login_complete'."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["id", "description"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user when the next step genuinely requires a "
                "human choice or missing information."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_tab",
            "description": (
                "Make another open tab the active tab (it is also brought to the "
                "front). Use the tab ids listed under 'tabs' in the browser state."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"tab_id": {"type": "integer"}},
                "required": ["tab_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_tab",
            "description": (
                "Open a new tab (optionally at a URL) and make it active, "
                "leaving the current tab untouched."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": ["string", "null"]}},
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_tab",
            "description": "Close a tab by id. Only close tabs the user asked to close.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"tab_id": {"type": "integer"}},
                "required": ["tab_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": (
                "End the task. Call it ALONE (never in the same response as other "
                "browser actions), after inspecting the current state. "
                "success=true only if the goal is verifiably achieved OR you have reached "
                "the payment/checkout page for the requested booking/purchase (never enter "
                "payment details); "
                "success=false if it is impossible or blocked."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Short reply to the user. If they asked a question, the answer goes here.",
                    },
                    "success": {"type": "boolean"},
                    "evidence": {
                        "type": "string",
                        "description": "What in the current browser state proves the goal is achieved (or why it is blocked).",
                    },
                },
                "required": ["message", "success", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    {"type":"function","function":{"name":"click_seat","description":"Click a seat on a seat map by its label from seats.available in the browser state (e.g. 'H12').","strict":True,"parameters":{"type":"object","properties":{"seat":{"type":"string"}},"required":["seat"],"additionalProperties":False}}},
    {"type":"function","function":{"name":"request_human","description":"Bring the AutoBot window to the user's desktop and ask them to act in the browser (pick seats, solve captcha, make a decision). Call it alone.","strict":True,"parameters":{"type":"object","properties":{"reason":{"type":"string"},"kind":{"type":"string","enum":["seat_selection","captcha","decision","other"]}},"required":["reason","kind"],"additionalProperties":False}}},
]


SYSTEM_PROMPT = r"""
You are the planning brain of a browser automation agent.

Your job is NOT to control the browser directly. You output function calls that
the local Python executor will perform.

CRITICAL RULES:
1. First inspect the semantic DOM supplied by the program.
2. Batch multiple obvious actions into ONE response whenever they form a
   coherent sequence. Do not ask the model to re-decide after every click/type.
3. Actions are executed sequentially, but the executor may stop the batch if
   the page changes in a way that invalidates the remaining actions.
4. Use checkpoint(id, description) after a logical subtask, especially after
   meaningful milestones such as logging in, opening a section, or submitting
   a form.
5. If a new modal/popup appears, deal with it as the new page state. Do not
   blindly continue an old plan.
6. Ask the user with ask_user() when a genuine human decision is required.
7. FINISHING. Call done() ALONE, in its own response, only after you have
   inspected the CURRENT BROWSER STATE and can name evidence on the page that
   the goal is achieved (put it in `evidence`). Actions running without error
   is NOT proof of success. Never put done() in the same response as other
   browser actions: first let them run, look at the result, then finish.
   Set success=false when the goal is impossible or blocked (CAPTCHA, login
   challenge, item not found, site error) and explain. If the user asked a
   question, the answer must be in done.message. Once the goal is achieved stop
   acting: do not click further (reaching a payment page for a booking/purchase
   counts as achieved, see rule 22). A separate verifier double-checks
   done(success=true) and will send you back to work if the page does not prove it.
   Use RECENT HISTORY to see what you already did; never redo an action that
   already succeeded, and if the same actions repeat without the page changing,
   change approach or finish with success=false / ask_user.
8. Never request, repeat, reveal, or invent a secret value. Local values are
   represented by handles such as @username and @password. In a type() call,
   pass the handle itself, not its value.
9. Prefer role + accessible name. Use placeholder/text only when that is more
   reliable. Avoid brittle CSS when possible.
10. Do not use element coordinates.
11. Keep the plan suitable for a demonstration: reliable, straightforward,
    and conservative rather than over-engineered.
12. When the user asks to play/watch a title without specifying a service,
    search Google for where that exact content is available, inspect relevant
    results, then open the matching service website and locate the title there.
    Do not assume YouTube is the source. Prefer a service the user is already
    signed into when that is evident.
13. For login tasks, use the current sign-in flow and submit credentials only
    through the site's visible form. After submitting, inspect the new page
    state and follow any next verification step; do not navigate back to the
    sign-in URL or open another sign-in tab just because the page is still
    loading or the DOM looks similar. If already on the requested site's
    sign-in page, do not navigate to it again. If authentication is blocked,
    requires a challenge, or cannot be confirmed, stop and explain the state.
14. Before clicking any player, inspect the CURRENT BROWSER STATE media field.
    If a video is already playing (paused=false and ended=false), do not click
    the video or its play control again; confirm playback with a checkpoint
    and finish the task. If media is paused, click play only once, then inspect
    the next state before taking another playback action. Never use a generic
    CSS click on 'video' to test playback; it can pause an active video. If
    media state is unavailable, inspect visible player status and make at most
    one play action before checking again.
15. Do not navigate to the URL already shown in CURRENT BROWSER STATE. Reuse
    the current page and continue from its visible state.

16. TABS. CURRENT BROWSER STATE lists every open tab under "tabs" (id, title,
    url, active). The "elements", "dialogs" and "media" shown belong ONLY to the
    active tab. Pick the tab that matches the task: if the page the user means
    is already open in another tab, switch_tab to it instead of navigating or
    opening a duplicate. Use open_tab when you need a new page while keeping the
    current one. Do not open tabs needlessly and close tabs only when the user
    asks. After switch_tab/open_tab the batch ends and you will see that tab's
    state. If a click opens a new tab it becomes active automatically and you
    are told.
18. CAPTCHAS. You must never try to solve, bypass, or work around a CAPTCHA or
    "verify you are human" challenge (no clicking its checkbox or images, no
    alternate routes to dodge it). When the state shows
    captcha_challenge_detected (or you see such a challenge), call ask_user
    asking the user to solve it themselves in the browser window and reply
    "done" when finished. After they reply, inspect the new state and continue.
17. The user chats with you. PRIOR CONVERSATION (if any) holds earlier requests
    and results; use it to resolve follow-ups like "click the second one".
19. POPUPS: check `dialogs` and in_popup elements first. Handle the popup
    (seat-count sheet, terms, location, offers) before anything behind it.
    Prefer the css [data-ab-id="N"] target for popup elements.
20. SEATS: use click_seat with labels from seats.available. If the user named a
    category (e.g. RECLINER, PRIME, EXECUTIVE), pick labels only from
    seats.categories[<that category>]. NEVER pick seats listed in
    seats.accessible_do_not_pick; if a wheelchair/accessibility popup appears,
    click Cancel/Close/No and choose a regular seat instead. In the
    "How many seats?" popup click the number ONCE, then "Select Seats"; never
    reopen it. If the count is already set, go straight to click_seat. If the
    requested category has no available seats, call ask_user. If the seat map
    is a canvas or unclear, or the user gave no seat preference, call
    request_human with kind seat_selection.
21. LOOPS: never repeat an action that did not change the page; switch
    approach or call request_human.
22. PAYMENT: reaching a payment/checkout gateway (card/UPI/wallet/net-banking
    fields, "Pay now"/"Proceed to pay", razorpay/paytm/stripe/payu/juspay or a
    checkout URL) with the requested details selected means YOUR part of the
    booking/purchase is complete. NEVER enter payment details, never click pay,
    and never navigate back. Immediately call done(success=true) ALONE with a
    message like "I've reached the payment page for <details>. Please complete
    the payment yourself." and evidence naming the payment page and details.
23. CAROUSELS: if a needed date/time slot is not visible in a horizontal
    strip/carousel, call scroll with direction left/right and target = css
    selector of an element inside that strip (or click the strip's
    next/arrow button), then read the new snapshot. Never scroll the page
    vertically to look for items in a horizontal strip.
24. BEFORE PROCEEDING: before clicking Proceed/Continue/Book/Reserve, verify
    the snapshot shows the requested date, time slot and guest count
    selected. If those pickers are not visible (page auto-scrolled down),
    call scroll with direction "top" and target '' first, then select them.

The Python executor owns:
- Chromium
- secrets
- DOM querying
- action execution
- checkpoint persistence
- interruption/replanning
"""


VERIFIER_PROMPT = r"""
You are a strict completion checker for a browser automation agent.
Decide whether the USER TASK is fully achieved, judging ONLY from the evidence
you are given: the current browser state (active tab, tabs, elements, dialogs,
media) and the recent history. The agent's own claim is not evidence.

- Playing/watching media: achieved only if a media entry shows paused=false and
  ended=false on the page for the requested content.
- Navigating/opening something: the active tab's url/title/elements must match it.
- Logging in: achieved only if the page shows a signed-in state, not a
  login form, challenge, or error.
- Questions / information requests: achieved if the agent's done message
  contains an answer that the page or history supports.
- Forms/submissions: achieved only if a confirmation or the resulting page is visible.
- Booking/purchase tasks: achieved if the active tab is a payment/checkout page
  showing the requested booking/item details (the agent must not pay; the user
  completes payment).
- If the task asked for several things, all of them must be done.

Reply with JSON only: {"achieved": true|false, "reason": "<one short sentence>"}
""".strip()


class DeepSeekPlanner:
    def __init__(self, model: str = "deepseek-flash"):
        self.model = model
        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
        )

    def verify(
        self,
        task: str,
        dom_snapshot: str,
        history: List[str],
        message: str,
        evidence: str,
    ) -> Dict[str, Any]:
        """Independent check of a done(success=true) claim against the live page."""
        context = f"""
USER TASK:
{task}

AGENT'S CLAIM (done message): {message}
AGENT'S EVIDENCE: {evidence}

RECENT HISTORY:
{chr(10).join(history) or "(none)"}

CURRENT BROWSER STATE:
{dom_snapshot}
""".strip()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": VERIFIER_PROMPT},
                    {"role": "user", "content": context},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            data = json.loads(response.choices[0].message.content)
            return {
                "achieved": bool(data.get("achieved")),
                "reason": str(data.get("reason", "")),
            }
        except Exception as exc:  # verifier trouble must not block a finished task
            return {"achieved": True, "reason": f"verifier unavailable ({type(exc).__name__})"}

    def plan(
        self,
        task: str,
        dom_snapshot: str,
        completed_checkpoints: List[str],
        event: str = "",
        history: Optional[List[str]] = None,
        conversation: Optional[List[Dict[str, str]]] = None,
    ) -> List[Dict[str, Any]]:
        prior = "\n".join(
            f"- User: {turn['task']}\n  Result: {turn['result']}"
            for turn in (conversation or [])
        )
        context = f"""
PRIOR CONVERSATION:
{prior or "(none)"}

USER TASK:
{task}

COMPLETED CHECKPOINTS:
{json.dumps(completed_checkpoints, ensure_ascii=False)}

RECENT HISTORY (your earlier batches for this task, oldest first):
{chr(10).join(history or []) or "(none yet)"}

CURRENT BROWSER STATE (semantic DOM, not raw HTML):
{dom_snapshot}

EVENT:
{event or "Initial planning. No previous execution event."}

Produce the next batch of browser tool calls.
""".strip()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]
        message = None
        for attempt in range(2):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_choice="auto",
                temperature=0.1,
            )
            message = response.choices[0].message
            if message.tool_calls:
                break
            messages.append({"role": "assistant", "content": message.content or ""})
            messages.append({
                "role": "user",
                "content": "You must answer with tool calls only. Call done() if the task is finished.",
            })

        if not message.tool_calls:
            # Plain text instead of a tool call: treat it as a finish claim; the
            # verifier decides whether the page actually supports it.
            return [{
                "name": "done",
                "arguments": {
                    "message": (message.content or "No answer.").strip(),
                    "success": True,
                    "evidence": "",
                },
            }]

        calls = []
        for tool_call in message.tool_calls:
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid JSON arguments from tool {tool_call.function.name}: "
                    f"{tool_call.function.arguments}"
                ) from exc

            calls.append(
                {
                    "name": tool_call.function.name,
                    "arguments": arguments,
                }
            )

        return calls
