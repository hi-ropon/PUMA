# device_reasoner.py
"""
三菱 PLC デバイス推定ツール
------------------------------------------
・コメント CSV と PLC プログラムの内容を丸ごと
  OpenAI へ渡して “最も関連するデバイス” を JSON 構造で出力
    例) {"dev": "Y", "address": 1000}
・@tool デコレータ付き関数として定義し、
  plc_agent._run_diagnostics 内から呼び出せる
"""

from __future__ import annotations

import os
import json
import re
import textwrap
import httpx
import openai
from dotenv import load_dotenv
from agents import function_tool as tool
import comments_search as hs
import plc_agent

# ──────────────────── OpenAI 初期化 ────────────────────
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY", "")
client = openai.OpenAI(
    api_key=openai.api_key,
    timeout=httpx.Timeout(120.0, read=None),
)

# ──────────────────── 定数 ────────────────────
ALLOWED_DEVS: set[str] = {"X", "Y", "D", "M"}
_DEV_SPLIT_PATTERN = re.compile(r"([XYDM])(\d+)", re.IGNORECASE)

# ──────────────────── 共通ユーティリティ ────────────────────
def _build_context(max_tokens: int = 200_000) -> str:
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
        rows = prog.get("rows", [])

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


def _sanitize_device(parsed: dict[str, t.Any]) -> dict[str, t.Any]:
    """
    dev と address を妥当値に補正する。

    Returns
    -------
    dict
        正常なら修正済み dict、不正なら {"error": "..."} を返す
    """
    dev_raw = str(parsed.get("dev", "")).upper()

    # そのまま許可リストにあれば OK
    if dev_raw in ALLOWED_DEVS:
        parsed["dev"] = dev_raw
        return parsed

    # 英字 + 数値 連結パターンを分離
    match = _DEV_SPLIT_PATTERN.fullmatch(dev_raw)
    if match:
        parsed["dev"] = match.group(1).upper()
        parsed["address"] = int(match.group(2))
        return parsed

    return {"error": "invalid dev"}


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
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"▼ユーザー質問\n{query}\n\n"
                       "▼コメント + プログラム抜粋\n"
                       f"{_build_context()}",
        },
    ]

    resp = client.chat.completions.create(
        model="o4-mini",
        messages=messages,
    )

    content: str = resp.choices[0].message.content.strip()

    # フォーマット保証のため簡易検証し、失敗時はエラー文字列を返す
    try:
        parsed: dict[str, t.Any] = json.loads(content)
        if isinstance(parsed, dict) and "dev" in parsed and "address" in parsed:
            parsed = _sanitize_device(parsed)
            return json.dumps(parsed, ensure_ascii=False)
    except json.JSONDecodeError:
        pass

    return '{"error": "unparsable"}'
