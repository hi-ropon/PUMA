"""
program_search.py
=================================
PLC プログラム検索ユーティリティ
・search_program … 対象デバイス出現ブロック抽出
・related_devices … 近傍で使用される他デバイス一覧
"""

from __future__ import annotations
import re
from typing import Dict, List


def search_program(
    programs: Dict[str, dict],
    device: str,
    addr: int,
    context: int = 0,
) -> List[List[str]]:
    """
    与えられた programs 内から指定デバイスが出現するブロックを抽出

    Returns
    -------
    List[List[str]]
        [[行1, 行2, ...], ...] 形式
    """
    target: str = f"{device}{addr}"
    blocks: List[List[str]] = []

    for prog in programs.values():
        headers = prog.get("headers", [])
        rows = prog.get("rows", [])

        if not rows or "I/O(デバイス)" not in headers:
            continue

        io_idx = headers.index("I/O(デバイス)")
        step_idx = headers.index("ステップ番号") if "ステップ番号" in headers else None
        inst_idx = headers.index("命令") if "命令" in headers else None
        note_idx = headers.index("ノート") if "ノート" in headers else None

        for i, row in enumerate(rows):
            if len(row) <= io_idx or row[io_idx].strip().strip('"') != target:
                continue

            start = max(0, i - context)
            end = min(len(rows), i + context + 1)

            block: List[str] = []
            for j in range(start, end):
                ctx = rows[j]
                if len(ctx) <= io_idx:
                    continue

                parts: List[str] = []

                if step_idx is not None and len(ctx) > step_idx and ctx[step_idx]:
                    parts.append(f"ステップ{ctx[step_idx]}")
                if inst_idx is not None and len(ctx) > inst_idx and ctx[inst_idx]:
                    parts.append(ctx[inst_idx])
                if len(ctx) > io_idx and ctx[io_idx]:
                    parts.append(ctx[io_idx])
                if note_idx is not None and len(ctx) > note_idx and ctx[note_idx]:
                    parts.append(f"({ctx[note_idx]})")

                if parts:
                    block.append(" ".join(parts))

            if block:
                blocks.append(block)

    return blocks


def related_devices(
    programs: Dict[str, dict],
    device: str,
    addr: int,
    context: int = 30,
) -> List[str]:
    """
    target ブロック近傍で使われている他デバイスを抽出してソート
    """
    deps: set[str] = set()
    pattern = re.compile(r"[XYMDTS]\d+")

    for block in search_program(programs, device, addr, context):
        for line in block:
            for m in pattern.findall(line):
                if m != f"{device}{addr}":
                    deps.add(m)

    return sorted(deps)
