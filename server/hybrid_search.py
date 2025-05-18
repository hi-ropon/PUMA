"""hybrid_search.py
=================================
三菱 PLC コメント検索をハイブリッド (BM25 + Embedding) で行うユーティリティ。

使い方:
    from hybrid_search import (
        load_comments,
        get_comment,
        find_best_device,
    )

    # CSV を読み込む
    load_comments(open("comments.csv", "rb"))

    # 質問から最適なデバイスを推定
    dev, score = find_best_device("Y1000 のシリンダが前進しない原因を調べて")

    # コメント取得
    text = get_comment(dev)

コーディング規約:
    - if 文やループの { } は改行して配置。
    - PEP8 + 日本語コメント。
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import typing as t

import httpx
import openai
from dotenv import load_dotenv

# ランタイムでインストールされていない場合、ImportError になるので try～except で回避
try:
    from rank_bm25 import BM25Okapi
except ImportError:  # pragma: no cover
    BM25Okapi = None  # type: ignore

# ──────────────────── OpenAI 初期化 ────────────────────
load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY", "")
client = openai.OpenAI(
    api_key=openai.api_key,
    timeout=httpx.Timeout(60.0, read=None),
)

# ──────────────────── グローバル状態 ────────────────────
COMMENTS: dict[str, str] = {}
COMMENT_EMBEDS: dict[str, list[float]] = {}
_BM25: BM25Okapi | None = None

# ──────────────────── 内部ユーティリティ ────────────────────

def _decode_bytes(data: bytes) -> io.StringIO:
    """受け取ったバイト列を複数エンコーディングでデコード。"""
    for enc in ("utf-8-sig", "utf-16", "shift_jis", "cp932"):
        try:
            return io.StringIO(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return io.StringIO(data.decode("utf-8", errors="replace"))


def _embed_text(text: str) -> list[float]:
    """OpenAI Embedding API でベクトル化。"""
    resp = client.embeddings.create(input=[text], model="text-embedding-3-small")
    return resp.data[0].embedding  # type: ignore


def _cosine(v1: list[float], v2: list[float]) -> float:
    """コサイン類似度。0–1 を想定 (同方向で 1)。"""
    if not v1 or not v2:
        return -1.0

    dot: float = sum(a * b for a, b in zip(v1, v2))
    norm1: float = math.sqrt(sum(a * a for a in v1))
    norm2: float = math.sqrt(sum(b * b for b in v2))

    if norm1 == 0.0 or norm2 == 0.0:
        return -1.0

    return dot / (norm1 * norm2)


# ──────────────────── 公開 API ────────────────────

def load_comments(stream_or_bytes: io.TextIOBase | bytes) -> None:
    """コメント CSV を読み込み、BM25・Embeddings を構築。"""
    global _BM25

    # ストリームを確保
    if isinstance(stream_or_bytes, bytes):
        stream = _decode_bytes(stream_or_bytes)
    else:
        stream = stream_or_bytes

    COMMENTS.clear()
    COMMENT_EMBEDS.clear()

    # CSV パース
    sample: str = stream.read(1024)
    stream.seek(0)

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
    except csv.Error:
        dialect = csv.excel_tab

    reader = csv.reader(stream, dialect)

    for row in reader:
        if len(row) < 2:
            continue

        key: str = row[0].strip().strip('"')
        val: str = row[1].strip().strip('"')

        if not key or key.lower() in ("test", "デバイス名", "\ufefftest"):
            continue

        COMMENTS[key] = val

    # BM25 セットアップ
    if BM25Okapi is None:
        _BM25 = None
    else:
        # トークン化を簡易にスペース + 日本語形態素も考慮して正規表現分割
        tokenize = lambda s: re.findall(r"[A-Za-z0-9]+|[一-龥ぁ-んァ-ヶ]+", s)
        corpus_tokens: list[list[str]] = [tokenize(text) for text in COMMENTS.values()]
        _BM25 = BM25Okapi(corpus_tokens)

    # Embedding 作成
    for k, v in COMMENTS.items():
        try:
            COMMENT_EMBEDS[k] = _embed_text(v)
        except Exception:
            COMMENT_EMBEDS[k] = []


def get_comment(device: str) -> str:
    """デバイスに対応するコメントを返す。"""
    return COMMENTS.get(device, "")


def _device_regex() -> re.Pattern[str]:
    """デバイスコード抽出用正規表現。"""
    return re.compile(r"\b([A-Z]{1,3}[0-9]{1,5})\b", flags=re.IGNORECASE)


def _bm25_candidates(query: str, top_k: int = 30) -> list[str]:
    """BM25 で上位候補デバイスキーを返す。"""
    if _BM25 is None:
        return []

    tokenize = lambda s: re.findall(r"[A-Za-z0-9]+|[一-龥ぁ-んァ-ヶ]+", s)
    scores = _BM25.get_scores(tokenize(query))
    ranked_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]
    keys = list(COMMENTS.keys())
    return [keys[i] for i in ranked_idx]


def find_best_device(question: str, *, alpha: float = 0.6) -> tuple[str | None, float]:
    """質問から最適なデバイス (キー) とスコアを返す。

    alpha: 0–1 (embedding の寄与率)
    """
    # --- 1) 正規表現で直接ヒット --------------------
    m = _device_regex().search(question)
    if m:
        direct: str = m.group(1).upper()
        if direct in COMMENTS:
            return direct, 1.0

    # --- 2) ベクトル化 --------------------------------
    try:
        q_vec = _embed_text(question)
    except Exception:
        q_vec = []

    # --- 3) BM25 --------------------------------------
    bm25_keys = _bm25_candidates(question, top_k=30)
    if not bm25_keys:
        bm25_keys = list(COMMENTS.keys())  # フォールバック

    best_key: str | None = None
    best_score: float = -1.0

    for key in bm25_keys:
        cos = _cosine(q_vec, COMMENT_EMBEDS.get(key, []))
        bm25_rank = (bm25_keys.index(key) + 1) if key in bm25_keys else len(COMMENTS)
        bm25_score = 1.0 / bm25_rank  # Reciprocal Rank

        score = alpha * cos + (1.0 - alpha) * bm25_score

        if score > best_score:
            best_score = score
            best_key = key

    return best_key, best_score


# ──────────────────── __all__ ────────────────────
__all__ = [
    "load_comments",
    "get_comment",
    "find_best_device",
]
