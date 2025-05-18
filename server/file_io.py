"""
file_io.py
=================================
ファイル I/O ユーティリティ
・decode_bytes … CSV バイト列 → TextIO
・load_program … 三菱 PLC CSV → dict 構造
"""

from __future__ import annotations
import csv
import io
from typing import Dict, List, TextIO

__all__ = ["decode_bytes", "load_program"]


def decode_bytes(data: bytes) -> io.StringIO:
    """
    受け取ったバイト列をマルチエンコーディングでデコードして TextIO にする
    優先順: UTF-8-SIG → UTF-16 → Shift-JIS → CP932 → UTF-8(replace)
    """
    for enc in ("utf-8-sig", "utf-16", "shift_jis", "cp932"):
        try:
            return io.StringIO(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return io.StringIO(data.decode("utf-8", errors="replace"))


def load_program(stream: TextIO) -> Dict:
    """
    三菱 PLC CSV を読み込み、基本メタ情報と本体行を dict で返す
    { project, model, headers, rows }
    """
    sample: str = stream.read(1024)
    stream.seek(0)

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab

    rows: List[List[str]] = list(csv.reader(stream, dialect))
    if not rows:
        return {}

    project = rows[0][0].strip().strip('"') if rows[0] else ""
    model = rows[1][1].strip().strip('"') if len(rows) > 1 and len(rows[1]) > 1 else ""
    headers = rows[2] if len(rows) > 2 else []
    body = rows[3:] if len(rows) > 3 else []

    return {
        "project": project,
        "model": model,
        "headers": headers,
        "rows": body,
    }
