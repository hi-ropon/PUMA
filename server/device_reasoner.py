# device_reasoner.py
"""
三菱 PLC デバイス推定ツール
------------------------------------------
・コメント CSV と PLC プログラムの内容を丸ごと
  OpenAI へ渡して “最も関連するデバイス” を JSON 構造で出力する
    例) {"dev": "Y", "address": 1000}
・@tool デコレータ付き関数として定義し、
  plc_agent._run_diagnostics 内から呼び出せる
"""

from __future__ import annotations

import os
import json
import textwrap
import httpx
import openai
from dotenv import load_dotenv
from agents import function_tool as tool        # OpenAI Agents SDK
import comments_search as hs                      # コメントを保管している既存モジュール
import plc_agent                                # PLC プログラム辞書を持つ既存モジュール

# ──────────────────── OpenAI 初期化 ────────────────────
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY", "")
client = openai.OpenAI(
    api_key=openai.api_key,
    timeout=httpx.Timeout(60.0, read=None),
)

# ──────────────────── 共通ユーティリティ ────────────────────
def _build_context(max_tokens: int = 200000) -> str:
    """
    コメント & PLC プログラムを大きな一塊のテキストにして返す。

    * token 数オーバーを避けるため、長過ぎる場合は末尾を切り捨てる
    * コメントは「デバイス: コメント」の1行形式
    * プログラムは I/O(デバイス) 列だけを抽出
    """
    lines: list[str] = []

    # --- コメント -----------------------------------------------------------
    for dev, comment in hs.COMMENTS.items():
        lines.append(f"{dev}: {comment}")

    # --- PLC プログラム (I/O(デバイス) 列のみ) ------------------------------
    for prog in plc_agent.PROGRAMS.values():
        headers = prog.get("headers", [])
        rows    = prog.get("rows",    [])

        if "I/O(デバイス)" not in headers:
            continue

        io_idx = headers.index("I/O(デバイス)")
        for row in rows:
            if len(row) > io_idx and row[io_idx].strip():
                lines.append(row[io_idx].strip())

    # --- トークン長をざっくり制御 (≈4 文字 ≒ 1 token として見積り) ----------
    joined = "\n".join(lines)
    if len(joined) // 4 > max_tokens:
        joined = joined[: max_tokens * 4]

    return joined


# ──────────────────── OpenAI Tool 実装 ────────────────────
@tool
def reasoning_device(query: str) -> str:
    """
    ユーザー質問 (`query`) から対象デバイスを推定して返す。

    Returns
    -------
    str
        JSON 文字列 (例: {"dev": "Y", "address": 1000})
    """
    system_prompt = textwrap.dedent(
        """
        あなたは三菱 PLC のデバイス抽出アシスタントです。
        与えられたコメント一覧・プログラム内デバイス一覧・ユーザー質問を元に、
        一番関連が深い単一デバイスを決定してください。
        結果は **以下の JSON だけ** を厳格に出力してください。

        {"dev": "<デバイス英字>", "address": <数値>}
        """
    ).strip()

    messages = [
        { "role": "system", "content": system_prompt },
        { "role": "user",
          "content":
              f"▼ユーザー質問\n{query}\n\n"
              "▼コメント + プログラム抜粋\n"
              f"{_build_context()}"
        },
    ]

    resp = client.chat.completions.create(
        model       = "o4-mini",
        messages    = messages,
    )

    content: str = resp.choices[0].message.content.strip()

    # フォーマット保証のため簡易検証し、失敗時はエラー文字列を返す
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict) and "dev" in parsed and "address" in parsed:
            return content
    except json.JSONDecodeError:
        pass

    return '{"error": "unparsable"}'
