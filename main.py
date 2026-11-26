import argparse
import os
import sys

from dotenv import load_dotenv

from agent import list_runs
from browser import BrowserController
from llm import DeepSeekPlanner
from browser_window import BrowserWindow
from service import AgentService, parse_user_input  # noqa: F401  (parse_user_input re-exported)


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

    print("Starting AutoBot Browser. The browser profile is persistent between launches.\n")

    planner = DeepSeekPlanner(model=args.model)

    # Chromium and the agent live on a worker thread (Playwright's sync API is
    # thread-affine); the Tk chat window stays on the main thread.
    service = AgentService(
        make_browser=lambda: BrowserController(
            executable_path=args.browser_path,
            profile_dir=args.profile_dir,
        ),
        planner=planner,
        logs_dir=args.logs_dir,
    )
    service.start()

    BrowserWindow(service).run()
    service.shutdown()
    service.join(timeout=10)


if __name__ == "__main__":
    cli()
