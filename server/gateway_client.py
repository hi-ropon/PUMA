"""
gateway_client.py
=================================
Gateway (REST) との通信ヘルパ
・read_device_values … デバイス値を取得
"""

from __future__ import annotations
from typing import List, Dict
import requests


def read_device_values(device: str, addr: int, length: int, *,
                       base_url: str, ip: str, port: str) -> List[int]:
    res = requests.get(f"{base_url}/{device}/{addr}/{max(1,length)}",
                       params={"ip": ip, "port": port},
                       timeout=30)
    res.raise_for_status()
    return res.json()["values"]


def read_file_info(drive: int = 0, start_no: int = 1, count: int = 36, *,
                   base_url: str, ip: str, port: str, **params) -> List[Dict]:
    res = requests.get(f"{base_url}/{drive}",
                       params={"start_no": start_no,
                               "count":    count,
                               "ip":       ip,
                               "port":     port,
                               **params},
                       timeout=30)
    if res.status_code != 200:
        # --- 詳細メッセージを拾う ---
        try:
            detail = res.json().get("detail", res.text)
        except ValueError:
            detail = res.text
        raise requests.HTTPError(f"{res.status_code} {detail}", response=res)
    return res.json()["files"]