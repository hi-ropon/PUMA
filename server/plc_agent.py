# plc_agent.py
"""
三菱 PLC 解析ユーティリティ
------------------------------------------
・コメント／プログラム検索
・デバイス値読取
・AI-Diagnostics (Agents)
"""

from __future__ import annotations

import csv
import io
import os
import re
import asyncio
import typing as t

import httpx
import requests
import openai
from dotenv import load_dotenv
from eventlet import tpool
from agents import Agent, Runner, function_tool as tool
from agents.exceptions import MaxTurnsExceeded

import comments_search as hs
import device_reasoner as dr       # reasoning_device を提供

# ──────────────────── OpenAI 初期化 ────────────────────
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY", "")
client = openai.OpenAI(
    api_key=openai.api_key,
    timeout=httpx.Timeout(60.0, read=None),
)

# ──────────────────── グローバル ------------------------------------------------
PROGRAMS: dict[str, dict] = {}

# ──────────────────── ヘルパ: ファイル I/O ------------------------------------
def decode_bytes(data: bytes) -> io.StringIO:
    """複数エンコーディングを試してバイト列→テキスト化。"""
    for enc in ("utf-8-sig", "utf-16", "shift_jis", "cp932"):
        try:
            return io.StringIO(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return io.StringIO(data.decode("utf-8", errors="replace"))


def load_program(stream: io.TextIOBase) -> dict:
    """三菱 PLC CSV を辞書へロード。"""
    sample: str = stream.read(1024)
    stream.seek(0)

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab

    rows = list(csv.reader(stream, dialect))
    if not rows:
        return {}

    project = rows[0][0].strip().strip('"') if rows[0] else ""
    model   = rows[1][1].strip().strip('"') if len(rows) > 1 and len(rows[1]) > 1 else ""
    headers = rows[2] if len(rows) > 2 else []
    body    = rows[3:] if len(rows) > 3 else []

    return {
        "project": project,
        "model":   model,
        "headers": headers,
        "rows":    body,
    }

# ──────────────────── プログラム検索 ------------------------------------------
def search_program(device: str, addr: int, context: int = 0) -> list[str]:
    """指定デバイスを含む行（前後 context 行付き）を返す。"""
    target: str = f"{device}{addr}"
    results: list[str] = []

    for prog in PROGRAMS.values():
        headers = prog.get("headers", [])
        rows    = prog.get("rows",    [])

        if not rows or "I/O(デバイス)" not in headers:
            continue

        io_idx   = headers.index("I/O(デバイス)")
        step_idx = headers.index("ステップ番号") if "ステップ番号" in headers else None
        inst_idx = headers.index("命令")       if "命令"       in headers else None
        note_idx = headers.index("ノート")     if "ノート"     in headers else None

        for i, row in enumerate(rows):
            if len(row) <= io_idx or row[io_idx].strip().strip('"') != target:
                continue

            start = max(0, i - context)
            for j in range(start, i + 1):
                ctx = rows[j]
                if len(ctx) <= io_idx:
                    continue

                parts: list[str] = []
                if step_idx is not None and len(ctx) > step_idx and ctx[step_idx]:
                    parts.append(f"ステップ{ctx[step_idx]}")
                if inst_idx is not None and len(ctx) > inst_idx and ctx[inst_idx]:
                    parts.append(ctx[inst_idx])
                if len(ctx) > io_idx and ctx[io_idx]:
                    parts.append(ctx[io_idx])
                if note_idx is not None and len(ctx) > note_idx and ctx[note_idx]:
                    parts.append(f"({ctx[note_idx]})")

                if parts:
                    line = " ".join(parts)
                    if line not in results:
                        results.append(line)

    return results


def related_devices(device: str, addr: int, context: int = 10) -> list[str]:
    """ターゲット近傍で使われている他デバイス一覧。"""
    deps: set[str] = set()
    pattern = re.compile(r"[XYMDTS]\d+")

    for line in search_program(device, addr, context):
        for m in pattern.findall(line):
            if m != f"{device}{addr}":
                deps.add(m)

    return sorted(deps)

# ──────────────────── デバイス値読取 -----------------------------------------
def read_device_values(
    device: str,
    addr: int,
    length: int,
    *,
    base_url: str,
    ip: str,
    port: str,
) -> list[int]:
    """Gateway REST でデバイス値を取得。"""
    res = requests.get(f"{base_url}/{device}/{addr}/{length}",
                       params = { "ip": ip, "port": port })
    res.raise_for_status()
    return res.json()["values"]

# ──────────────────── AI Diagnostics -----------------------------------------
def _run_diagnostics(
    *,
    base_url: str,
    ip: str,
    port: str,
    question: str,
) -> str:
    """
    OpenAI Agents を用いて質問を自律解析。
    * reasoning_device でデバイス推定
    * 追加ツールで詳細調査
    最終出力は『ANSWER: ...』
    """

    # ------------- ツール定義 ---------------------------------------------
    @tool
    def read_values(dev: str, address: int, length: int) -> str:
        """PLC デバイス値を取得する。"""
        length = length or 1
        vals = read_device_values(
            dev, address, length,
            base_url = base_url,
            ip       = ip,
            port     = port,
        )
        return ",".join(str(v) for v in vals)

    @tool
    def program_lines(dev: str, address: int) -> str:
        """周辺プログラム行を返す。"""
        return "\n".join(search_program(dev, address, context = 2))

    @tool
    def related(dev: str, address: int) -> str:
        """関連デバイス一覧を返す。"""
        return ",".join(related_devices(dev, address))

    @tool
    def comment(dev: str, address: int) -> str:
        """コメントを返す。"""
        return hs.get_comment(f"{dev}{address}")

    tools = [
        dr.reasoning_device,   # ① 推定
        read_values,           # ② 読取
        program_lines,         # ③ コード抜粋
        related,               # ④ 関連デバイス
        comment,               # ⑤ コメント取得
    ]

    agent = Agent(
        name         = "PLC-Diagnostics",
        instructions = (
            "まず reasoning_device を呼び出して対象デバイスを JSON で取得し、\n"
            "続けて read_values / program_lines などを用いて不具合原因を推論し、\n"
            "最後に『ANSWER: ...』で日本語の結論だけを出力してください。\n"
            "推論のなかで追加で調査するデバイスはコメントを取得してから調査してください。"
        ),
        model       = "o4-mini",
        tools       = tools,
        output_type = str,
    )

    # eventlet との親和性を考慮し tpool で実行
    def _run(a: Agent, q: str, turns: int) -> t.Any:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return Runner.run_sync(a, input = q, max_turns = turns)
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
    エンドポイントから直接呼び出す関数。
    ベクトル検索は廃止。コメントだけロードしてそのまま _run_diagnostics。
    """
    # コメントは毎回ロード (軽量)
    comment_path = os.getenv("COMMENT_CSV")
    if comment_path and os.path.exists(comment_path):
        with open(comment_path, "rb") as f:
            hs.load_comments(decode_bytes(f.read()))

    if not hs.COMMENTS:
        return "コメントがロードされていません"

    return _run_diagnostics(
        base_url = base_url,
        ip       = ip,
        port     = port,
        question = question,
    )
