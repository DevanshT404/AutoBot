import json
import os
from typing import Any, Dict, List

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
            "description": "Scroll the page.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "amount": {"type": "integer"},
                },
                "required": ["direction", "amount"],
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
            "name": "done",
            "description": "End the task when the user's goal has been achieved.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
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
7. End with done() only when the user's goal is actually complete.
8. Never request, repeat, reveal, or invent a secret value. Local values are
   represented by handles such as @username and @password. In a type() call,
   pass the handle itself, not its value.
9. Prefer role + accessible name. Use placeholder/text only when that is more
   reliable. Avoid brittle CSS when possible.
10. Do not use element coordinates.
11. Keep the plan suitable for a demonstration: reliable, straightforward,
    and conservative rather than over-engineered.

The Python executor owns:
- Chromium
- secrets
- DOM querying
- action execution
- checkpoint persistence
- interruption/replanning
"""


class DeepSeekPlanner:
    def __init__(self, model: str = "deepseek-flash"):
        self.model = model
        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
        )

    def plan(
        self,
        task: str,
        dom_snapshot: str,
        completed_checkpoints: List[str],
        event: str = "",
    ) -> List[Dict[str, Any]]:
        context = f"""
USER TASK:
{task}

COMPLETED CHECKPOINTS:
{json.dumps(completed_checkpoints, ensure_ascii=False)}

CURRENT BROWSER STATE (semantic DOM, not raw HTML):
{dom_snapshot}

EVENT:
{event or "Initial planning. No previous execution event."}

Produce the next batch of browser tool calls.
""".strip()

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            tools=TOOL_DEFINITIONS,
            tool_choice="auto",
            temperature=0.1,
        )

        message = response.choices[0].message

        if not message.tool_calls:
            raise RuntimeError(
                "DeepSeek returned no tool calls. "
                f"Model message: {message.content!r}"
            )

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
