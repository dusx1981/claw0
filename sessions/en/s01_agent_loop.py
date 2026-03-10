"""
Section 01: The Agent Loop
"An agent is just while True + stop_reason"

    User Input --> [messages[]] --> LLM API --> finish_reason?
                                                /        \
                                           "stop"  "tool_calls"
                                              |           |
                                           Print      (next section)

Usage:
    cd claw0
    python en/s01_agent_loop.py

Required .env config:
    DASHSCOPE_API_KEY=sk-xxxxx
    MODEL_ID=qwen-plus
"""

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "qwen-plus")
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
)

SYSTEM_PROMPT = "You are a helpful AI assistant. Answer questions directly."

# ---------------------------------------------------------------------------
# ANSI colors
# ---------------------------------------------------------------------------
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"
BOLD = "\033[1m"


def colored_prompt() -> str:
    return f"{CYAN}{BOLD}You > {RESET}"


def print_assistant(text: str) -> None:
    print(f"\n{GREEN}{BOLD}Assistant:{RESET} {text}\n")


def print_info(text: str) -> None:
    print(f"{DIM}{text}{RESET}")


# ---------------------------------------------------------------------------
# Core: The Agent Loop
# ---------------------------------------------------------------------------
# 1. Collect user input, append to messages
# 2. Call the API
# 3. Check stop_reason -- "end_turn" means print, "tool_use" means dispatch
#
# Here stop_reason is always "end_turn" (no tools yet).
# Next section adds tools; the loop structure stays the same.
# ---------------------------------------------------------------------------


def agent_loop() -> None:
    """Main agent loop -- conversational REPL."""

    messages: list[dict] = []

    print_info("=" * 60)
    print_info("  claw0  |  Section 01: The Agent Loop")
    print_info(f"  Model: {MODEL_ID}")
    print_info("  Type 'quit' or 'exit' to leave. Ctrl+C also works.")
    print_info("=" * 60)
    print()

    while True:
        try:
            user_input = input(colored_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}Goodbye.{RESET}")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print(f"{DIM}Goodbye.{RESET}")
            break

        messages.append({
            "role": "user",
            "content": user_input,
        })

        try:
            api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            response = client.chat.completions.create(
                model=MODEL_ID,
                max_tokens=8096,
                messages=api_messages,
            )
        except Exception as exc:
            print(f"\n{YELLOW}API Error: {exc}{RESET}\n")
            messages.pop()
            continue

        # Check finish_reason to decide what happens next
        if response.choices[0].finish_reason == "stop":
            assistant_text = response.choices[0].message.content or ""

            print_assistant(assistant_text)

            messages.append({
                "role": "assistant",
                "content": response.choices[0].message.content,
            })

        elif response.choices[0].finish_reason == "tool_calls":
            print_info("[finish_reason=tool_calls] No tools in this section.")
            print_info("See s02_tool_use.py for tool support.")
            messages.append({
                "role": "assistant",
                "content": response.choices[0].message.content,
            })

        else:
            print_info(f"[finish_reason={response.choices[0].finish_reason}]")
            assistant_text = response.choices[0].message.content or ""
            if assistant_text:
                print_assistant(assistant_text)
            messages.append({
                "role": "assistant",
                "content": response.choices[0].message.content,
            })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("DASHSCOPE_API_KEY"):
        print(f"{YELLOW}Error: DASHSCOPE_API_KEY not set.{RESET}")
        print(f"{DIM}Copy .env.example to .env and fill in your key.{RESET}")
        sys.exit(1)

    agent_loop()


if __name__ == "__main__":
    main()
