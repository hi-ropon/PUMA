"""
plc_agent.py
=================================
三菱 PLC 解析ユーティリティ (コア)
責務:
    * OpenAI Agents による Diagnostics
    * PROGRAMS 共有リポジトリ保持
"""

from __future__ import annotations
import asyncio
import os
from typing import Any, Dict

import httpx
import openai
from dotenv import load_dotenv
from eventlet import tpool
from agents import Agent, Runner, function_tool as tool
from agents.exceptions import MaxTurnsExceeded

# 分離したヘルパ
from file_io import decode_bytes, load_program
from program_search import search_program, related_devices
from gateway_client import read_device_values, read_file_info
import comments_search as hs
import device_reasoner as dr  # reasoning_device を提供

# ──────────────────── OpenAI 初期化 ────────────────────
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY", "")
client = openai.OpenAI(
    api_key=openai.api_key,
    timeout=httpx.Timeout(120.0, read=None),
)

# ──────────────────── グローバル ------------------------------------------------
PROGRAMS: Dict[str, dict] = {}

# ──────────────────── AI Diagnostics -----------------------------------------
def _run_diagnostics(
    *,
    base_url: str,
    ip: str,
    port: str,
    question: str,
) -> str:
    """
    OpenAI Agents で自律的に調査し『ANSWER: ...』を返す
    """

    # ---------- tool 群 -----------------------------------------------------
    @tool
    def read_values(dev: str, address: int, length: int) -> str:
        """PLC デバイス値を取得する"""
        vals = read_device_values(
            dev,
            address,
            length or 1,
            base_url=base_url,
            ip=ip,
            port=port,
        )
        return ",".join(str(v) for v in vals)

    @tool
    def program_lines(dev: str, address: int) -> list[str]:
        """
        周辺プログラム行を返す
        """
        blocks = search_program(PROGRAMS, dev, address, context=30)
        return ["\n".join(b) for b in blocks]

    @tool
    def related(dev: str, address: int) -> str:
        """関連デバイス一覧"""
        return ",".join(related_devices(PROGRAMS, dev, address))

    @tool
    def comment(dev: str, address: int) -> str:
        """コメント取得"""
        return hs.get_comment(f"{dev}{address}")

    tools = [
        dr.reasoning_device,  # ① デバイス推定
        read_values,          # ② 読取
        program_lines,        # ③ コード抜粋
        related,              # ④ 関連デバイス
        comment,              # ⑤ コメント
    ]

    agent = Agent(
        name="PLC-Diagnostics",
        instructions=(
            "まず reasoning_device を呼び出して対象デバイスを JSON で取得し、\n"
            "続けて read_values / program_lines などを用いて推論し、\n"
            "最後に『ANSWER: ...』で日本語の結論だけを出力してください。\n"
            "推論のなかで追加で調査するデバイスはコメントを取得してから調査してください。\n"
            "不具合調査の場合は、原因は1つとは限らないので、\n"
            "複数の可能性を挙げて調査してください。\n"
        ),
        model="gpt-4.1-mini",
        tools=tools,
        output_type=str,
    )

    # eventlet 親和性のためスレッドプール実行
    def _run(a: Agent, q: str, turns: int) -> Any:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return Runner.run_sync(a, input=q, max_turns=turns)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    try:
        result = tpool.execute(_run, agent, question, 30)
        return result.final_output
    except MaxTurnsExceeded:
        result = tpool.execute(_run, agent, question, 50)
        return result.final_output
    except Exception as ex:
        return f"AI 呼び出しでエラーが発生しました: {ex}"

# ──────────────────── 公開 API -------------------------------------------------
def run_analysis(
    question: str,
    *,
    base_url: str,
    ip: str,
    port: str,
) -> str:
    """
    Flask から直接呼び出すエントリポイント
    """
    # コメントは毎回ロード
    comment_path = os.getenv("COMMENT_CSV")
    if comment_path and os.path.exists(comment_path):
        with open(comment_path, "rb") as f:
            hs.load_comments(decode_bytes(f.read()))

    if not hs.COMMENTS:
        return "コメントがロードされていません"

    return _run_diagnostics(
        base_url=base_url,
        ip=ip,
        port=port,
        question=question,
    )

# ──────────────────── ファイル情報ラッパ ────────────────────
def read_directory_fileinfo(
    *,
    drive_no: int = 0,
    start_file_no: int = 1,
    request_count: int = 36,
    base_url: str = "http://127.0.0.1:8001/api/fileinfo",
    ip: str = "127.0.0.1",
    port: str = "5511",
):
    """
    Gateway 経由で 1810 (ディレクトリ/ファイル情報読出し) を呼び出す
    """
    return read_file_info(
        drive=drive_no,
        start_no=start_file_no,
        count=request_count,
        base_url=base_url,
        ip=ip,
        port=port,
    )