# Checkpointed Browser Agent Demo

A pragmatic browser-use agent for a choreographed college major-project demo.

## Five-module split

```text
main.py      -> CLI, local @variables, run commands
agent.py     -> planning loop, checkpoints, persistence, logs
llm.py       -> DeepSeek tool-call planner
browser.py   -> Chromium + semantic DOM observation
actions.py   -> local action execution + batch invalidation
```

## `.env`

Create a file named `.env` in the project folder:

```text
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_MODEL=deepseek-flash
```

The program loads this file automatically via `python-dotenv`.

You do not need to expose the API key in the terminal.

## Browser startup

When `python main.py` starts, it launches Chromium before waiting for your
first task. A persistent profile is used, so the browser window stays available
for the agent and cookies/session state can persist between runs.

If Chromium cannot be found automatically, supply its executable path:

```powershell
python main.py --browser-path "C:\path\to\chrome.exe"
```

## Install

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
playwright install
```

The agent tries to find an installed Chromium/Chrome executable. If needed:

```powershell
python main.py --browser-path "C:\path\to\chrome.exe"
```

Set your DeepSeek key:

```powershell
$env:DEEPSEEK_API_KEY="YOUR_KEY"
python main.py
```

The default model is `deepseek-flash`.

## Local secrets

You can type:

```text
@username = "my_username" @password = "my_password" Log into Instagram
```

DeepSeek receives the task with the handles only:

```text
Log into Instagram
@username
@password
```

The actual values are held in a temporary local file and substituted only by
the local action executor. They are not written into normal agent logs.

The DOM extractor does not read or export textbox values.

## Batching

A single DeepSeek response can produce:

```text
click username
type @username
click password
type @password
click @password
click Log in
checkpoint instagram_login_complete
```

Those browser actions are executed sequentially without an LLM call between
each one.

The next DeepSeek call happens only after:
- the batch completes,
- a checkpoint/state boundary is reached,
- a user clarification is needed, or
- the executor detects that the current batch is no longer valid.

## Checkpoints

Checkpoints represent meaningful completed subtasks rather than individual
browser actions.

Example:

```text
instagram_login_complete
profile_open
settings_open
post_submitted
```

When the executor reaches one of those calls, it writes a checkpoint file.

## Run logs

Every task gets its own folder:

```text
logs/
└── 20261005_231900_log_into_instagram/
    ├── task.txt
    ├── state.json
    ├── events.jsonl
    ├── batches/
    │   ├── 001_plan.json
    │   └── 002_plan.json
    ├── dom/
    │   ├── 001_before_plan.json
    │   ├── 001_after_batch.json
    │   └── 002_before_plan.json
    ├── checkpoints/
    │   └── instagram_login_complete.json
    └── screenshots/
        └── 002_interrupted.png
```

This gives you an actual forensic trail if the demo gets stuck.

### `events.jsonl`

Contains events such as:

```text
task_started
dom_captured
batch_planned
batch_completed
checkpoint_reached
batch_interrupted
screenshot_captured
clarification_requested
task_completed
```

### `state.json`

Contains the current logical state:

```json
{
  "task": "Log into Instagram",
  "status": "running",
  "completed_checkpoints": [
    "instagram_login_complete"
  ],
  "last_event": "Batch completed successfully.",
  "iterations": 3
}
```

### DOM snapshots

The files in `dom/` contain the semantic DOM seen by the LLM before and
after batches. They are useful for debugging why the agent made a decision.

### Screenshots

When an execution is interrupted by a missing target, exception, or newly
appearing modal, the current browser page is captured automatically.

## Why the Instagram popup won't silently derail the batch

Suppose a plan is:

```text
click login
click profile
```

After clicking login, Instagram shows a new modal.

The executor detects that a new dialog appeared and checks whether the next
action is actually inside that dialog.

If not, it stops the batch and records:

```text
batch_interrupted
reason = "A new modal/dialog appeared, invalidating the remaining batch."
```

Then the agent captures a fresh DOM and makes another DeepSeek decision.

If the model had planned:

```text
click login
click Not now
checkpoint instagram_login_complete
```

the executor can continue because `Not now` belongs to the newly visible dialog.

This is the central safety rule:

> A batch continues only while the assumptions behind its remaining actions
> are still true.

## Demo-first approach

This is intentionally not a state-of-the-art general-purpose browser agent.
It is designed to be easy for a two-person team to understand, demonstrate,
debug, and extend.

For a college demo, reliability and observability matter more than building
every browser-agent feature.
