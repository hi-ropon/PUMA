"""
gateway_client.py
=================================
Gateway (REST) との通信ヘルパ
・read_device_values … デバイス値を取得
"""

from __future__ import annotations
from typing import List
import requests


def read_device_values(
    device: str,
    addr: int,
    length: int,
    *,
    base_url: str,
    ip: str,
    port: str,
) -> List[int]:
    """
    FastAPI Gateway からデバイス値を取得して list[int] で返す
    """
    length = max(length, 1)
    res = requests.get(
        f"{base_url}/{device}/{addr}/{length}",
        params={"ip": ip, "port": port},
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["values"]
