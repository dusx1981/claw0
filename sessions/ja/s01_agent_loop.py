"""
Section 01: エージェントループ
「エージェントとは while True + finish_reason のことである」

    ユーザー入力 --> [messages[]] --> LLM API --> finish_reason?
                                                /        \
                                          "stop"  "tool_calls"
                                              |           |
                                           表示      (次のセクション)

使い方:
    cd claw0
    python ja/s01_agent_loop.py

.env に必要な設定:
    DASHSCOPE_API_KEY=sk-xxxxx
    MODEL_ID=qwen-plus
"""

# ---------------------------------------------------------------------------
# インポート
# ---------------------------------------------------------------------------
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env", override=True)

MODEL_ID = os.getenv("MODEL_ID", "qwen-plus")
client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url=os.getenv("DASHSCOPE_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
)

SYSTEM_PROMPT = "You are a helpful AI assistant. Answer questions directly."

# ---------------------------------------------------------------------------
# ANSI カラー
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
# コア: エージェントループ
# ---------------------------------------------------------------------------
#   1. ユーザー入力を受け取り、messages に追加
#   2. API を呼び出す
#   3. stop_reason を確認して次の動作を決定
#
#   ここでは stop_reason は常に "end_turn" (ツールなし)。
#   次のセクションで "tool_use" を追加 -- ループ構造はそのまま。
# ---------------------------------------------------------------------------


def agent_loop() -> None:
    """メインのエージェントループ -- 対話型 REPL。"""

    messages: list[dict] = []

    print_info("=" * 60)
    print_info("  claw0  |  Section 01: エージェントループ")
    print_info(f"  モデル: {MODEL_ID}")
    print_info("  'quit' または 'exit' で終了。Ctrl+C でも可。")
    print_info("=" * 60)
    print()

    while True:
        # --- ユーザー入力を取得 ---
        try:
            user_input = input(colored_prompt()).strip()
        except (KeyboardInterrupt, EOFError):
            print(f"\n{DIM}さようなら。{RESET}")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            print(f"{DIM}さようなら。{RESET}")
            break

        # --- 履歴に追加 ---
        messages.append({
            "role": "user",
            "content": user_input,
        })

        # --- LLM を呼び出す ---
        try:
            api_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
            response = client.chat.completions.create(
                model=MODEL_ID,
                max_tokens=8096,
                messages=api_messages,
            )
        except Exception as exc:
            print(f"\n{YELLOW}API エラー：{exc}{RESET}\n")
            messages.pop()
            continue

        # --- finish_reason を確認 ---
        if response.choices[0].finish_reason == "stop":
            assistant_text = response.choices[0].message.content or ""

            print_assistant(assistant_text)

            messages.append({
                "role": "assistant",
                "content": assistant_text,
            })

        elif response.choices[0].finish_reason == "tool_calls":
            print_info("[finish_reason=tool_calls] このセクションにはツールがありません。")
            print_info("ツール対応は s02_tool_use.py を参照してください。")
            assistant_text = response.choices[0].message.content or ""
            messages.append({
                "role": "assistant",
                "content": assistant_text,
            })

        else:
            print_info(f"[finish_reason={response.choices[0].finish_reason}]")
            assistant_text = response.choices[0].message.content or ""
            if assistant_text:
                print_assistant(assistant_text)
            messages.append({
                "role": "assistant",
                "content": assistant_text,
            })


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.getenv("DASHSCOPE_API_KEY"):
        print(f"{YELLOW}エラー：DASHSCOPE_API_KEY が設定されていません。{RESET}")
        print(f"{DIM}.env.example を .env にコピーして API キーを記入してください。{RESET}")
        sys.exit(1)

    agent_loop()


if __name__ == "__main__":
    main()
