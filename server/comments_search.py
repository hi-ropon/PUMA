# hybrid_search.py
"""
コメント CSV を読み込んで `COMMENTS` 辞書を提供するだけの
シンプルなユーティリティ。

・公開 API は
      load_comments(stream_or_bytes)
      get_comment(device)
  の 2 関数のみ
"""

from __future__ import annotations

import csv
import io
import typing as t

# ──────────────────── グローバル ────────────────────
COMMENTS: dict[str, str] = {}

# ──────────────────── 内部ユーティリティ ────────────────────
def _decode_bytes(data: bytes) -> io.StringIO:
    """
    受け取ったバイト列をマルチエンコーディングでデコード。

    優先順: UTF-8-SIG → UTF-16 → Shift-JIS → CP932 → UTF-8 (replace)
    """
    for enc in ("utf-8-sig", "utf-16", "shift_jis", "cp932"):
        try:
            return io.StringIO(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return io.StringIO(data.decode("utf-8", errors="replace"))

# ──────────────────── 公開 API ────────────────────
def load_comments(stream_or_bytes: io.TextIOBase | bytes) -> None:
    """
    コメント CSV を読み込んで `COMMENTS` 辞書を構築する。

    Parameters
    ----------
    stream_or_bytes : IO または bytes
        CSV ファイルストリーム、あるいはファイルのバイト列。
    """
    # ストリーム化 ----------------------------------------------------------
    if isinstance(stream_or_bytes, bytes):
        stream = _decode_bytes(stream_or_bytes)
    else:
        stream = stream_or_bytes

    COMMENTS.clear()

    # CSV Dialect 判定 ------------------------------------------------------
    sample = stream.read(1024)
    stream.seek(0)

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab

    reader = csv.reader(stream, dialect)

    # CSV パース ------------------------------------------------------------
    for row in reader:
        if len(row) < 2:
            continue

        key = row[0].strip().strip('"')
        val = row[1].strip().strip('"')

        if not key:
            continue
        if key.lower() in ("test", "デバイス名", "\ufefftest"):
            continue

        COMMENTS[key] = val

def get_comment(device: str) -> str:
    """
    デバイス名に対応するコメント文字列を返す。
    該当しない場合は空文字列 ("")。
    """
    return COMMENTS.get(device, "")

# ──────────────────── __all__ ────────────────────
__all__ = [
    "load_comments",
    "get_comment",
    "COMMENTS",
]
