import argparse
import os
import re
import sys
from getpass import getpass

from dotenv import load_dotenv
from pathlib import Path

from agent import BrowserAgent, list_runs
from browser import BrowserController
from llm import DeepSeekPlanner


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


def show_runs(logs_dir: str):
    runs = list_runs(logs_dir)
    if not runs:
        print("No runs yet.")
        return

    print("\nSaved runs:")
    for run in runs:
        print(f"  {run}")


def cli():
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Checkpointed Chromium browser agent"
    )
    parser.add_argument(
        "--browser-path",
        default=os.getenv("CHROMIUM_PATH"),
        help="Path to Chromium/Chrome executable.",
    )
    parser.add_argument(
        "--profile-dir",
        default=os.getenv("BROWSER_PROFILE_DIR", ".chromium_profile"),
        help="Persistent browser profile directory.",
    )
    parser.add_argument(
        "--logs-dir",
        default=os.getenv("AGENT_LOGS_DIR", "logs"),
        help="Directory containing one subfolder per run.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
        help="DeepSeek model name.",
    )
    args = parser.parse_args()

    if not os.getenv("DEEPSEEK_API_KEY"):
        print("ERROR: Set DEEPSEEK_API_KEY before running.")
        sys.exit(1)

    print("Browser Agent")
    print('Example: @username = "my_user" @password = "secret" Log into Instagram')
    print("Commands: 'runs' to list saved runs, 'exit' to quit.\n")

    browser = BrowserController(
        executable_path=args.browser_path,
        profile_dir=args.profile_dir,
    )
    planner = DeepSeekPlanner(model=args.model)

    browser.start()

    try:
        while True:
            raw = input("\nTask> ").strip()
            if not raw:
                continue

            if raw.lower() in {"exit", "quit"}:
                break

            if raw.lower() == "runs":
                show_runs(args.logs_dir)
                continue

            task, secrets = parse_user_input(raw)

            if not task:
                print("Please provide a task after the @variable assignments.")
                continue

            # Convenience: allow a user to omit a sensitive value and enter it
            # privately in the terminal. Still never send it to the LLM.
            handles = re.findall(r"@([A-Za-z_][A-Za-z0-9_]*)", task)
            for handle in handles:
                if handle.lower() in {"password", "pass", "secret"} and handle not in secrets:
                    secrets[handle] = getpass(f"{handle}: ")

            agent = BrowserAgent(
                browser=browser,
                planner=planner,
                logs_dir=args.logs_dir,
            )

            result = agent.run(task, secrets)
            print(f"\nAgent: {result}")
    finally:
        browser.stop()


if __name__ == "__main__":
    cli()
