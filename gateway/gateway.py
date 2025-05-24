# ------------------------------------------------------------
# FastAPI Gateway  ─ デバイス読取 & ファイル情報 (1810) API
# uvicorn gateway:app --host 127.0.0.1 --port 8001
# ------------------------------------------------------------
import os
import re
import binascii
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymcprotocol import Type3E
from pymcprotocol.mcprotocolerror import MCProtocolError   # pymcprotocol-v1.1.1 以上推奨

app = FastAPI(title="PLC Gateway")

# ─────────────────────────  環 境 変 数  ─────────────────────────
PLC_IP      = os.getenv("PLC_IP",         "127.0.0.1")
PLC_PORT    = int(os.getenv("PLC_PORT",   "5511"))
TIMEOUT_SEC = float(os.getenv("PLC_TIMEOUT_SEC", "3.0"))
PLC_DRIVE   = int(os.getenv("PLC_DRIVE",  "4"))

# ──────────────────── デバイス値読取 API (既存) ────────────────────
class ReadRequest(BaseModel):
    device: str = "D"
    addr:   int
    length: int
    ip:     Optional[str] = None
    port:   Optional[int] = None


def _read_plc(device: str, addr: int, length: int, *, ip: str, port: int) -> List[int]:
    """
    汎用ワード／ビット読取 helper
    """
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT_SEC * 4)
    plc.connect(ip, port)

    try:
        dev = device.upper()
        if dev in ("D", "W", "R", "ZR"):
            return plc.batchread_wordunits(f"{dev}{addr}", length)
        if dev in ("X", "Y", "M"):
            return plc.batchread_bitunits(f"{dev}{addr}", length)
        raise ValueError(f"Unsupported device '{device}'")
    finally:
        plc.close()


@app.post("/api/read")
def api_read(req: ReadRequest):
    try:
        vals = _read_plc(req.device, req.addr, req.length,
                         ip=req.ip or PLC_IP,
                         port=req.port or PLC_PORT)
        return {"values": vals}
    except Exception as ex:      # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")


@app.get("/api/read/{device}/{addr}/{length}")
def api_read_get(device: str, addr: int, length: int,
                 ip: Optional[str] = None, port: Optional[int] = None):
    try:
        vals = _read_plc(device, addr, length,
                         ip=ip   or PLC_IP,
                         port=port or PLC_PORT)
        return {"values": vals}
    except Exception as ex:      # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")

# ──────────────────── ファイル情報 API (1810h) ────────────────────
class FileInfoRequest(BaseModel):
    drive:    int = PLC_DRIVE
    start_no: int = 1
    count:    int = 36
    ip:       Optional[str] = None
    port:     Optional[int] = None


# MELSEC iQ-R (サブコマンド 0040) ファイル情報 1 レコード当たりの固定長部
_FIXED_TAIL = 1 + 9 + 3 + 4 + 4          # 属性1 + 予備9 + Time3 + Date4 + Size4  = 21byte

_ASCII_NAME = re.compile(r'[\$A-Za-z0-9_\-]{1,64}(?:\.[A-Za-z0-9]{1,4})?')


def parse_iqr_fileinfo(raw: bytes) -> list[dict]:
    """
    三菱 iQ-R シリーズ 0040 応答 (Binary) 専用パーサ
      - 先頭 4byte は Last-FileNo / Reserved
      - 各レコードは以下で並ぶ
          0-1 : 文字数 (UTF-16 文字数, little-endian)
          2-…: UTF-16 ファイル名 (文字数×2 byte)
          ..  : 1   byte  属性
                 9   byte  予備 (未使用)
                 3   byte  最終編集時刻
                 4   byte  最終編集日付
                 4   byte  ファイルサイズ (little-endian, byte 単位)
    """
    files: list[dict] = []
    idx: int = 4                       # 先頭 4byte を飛ばす

    while idx + 2 <= len(raw):
        char_cnt: int = int.from_bytes(raw[idx:idx + 2], "little")
        if char_cnt in (0, 0xFFFF):    # 0=余白, 0xFFFF=終端
            break

        idx += 2
        if idx + char_cnt * 2 > len(raw):
            break                      # 壊れたフレーム

        # ファイル名 (UTF-16LE)
        name_utf16 = raw[idx:idx + char_cnt * 2]
        idx += char_cnt * 2
        name = name_utf16.decode("utf-16le", errors="ignore").rstrip("\x00").strip()

        # 属性
        attribute: int = raw[idx]
        idx += 1

        # 予備 9byte + 時刻 3byte + 日付 4byte
        idx += 9 + 3 + 4

        # サイズ (4byte, little)
        size: int = int.from_bytes(raw[idx:idx + 4], "little")
        idx += 4

        # レコード完了 ─ 次へ
        base, _, ext = name.partition(".")
        files.append({
            "name":      base,
            "ext":       ext,
            "size":      size,
            "attribute": attribute         # 0x10: ディレクトリ
        })

    return files


@app.get("/api/fileinfo/{drive}")
def api_fileinfo_get(
    drive:     int,
    start_no:  int = 1,
    count:     int = 36,
    path:      str | None = "$MELPRJ$",
    ip:        Optional[str] = None,
    port:      Optional[int] = None
):
    """
    1810h  ディレクトリ / ファイル情報の読出 (iQ-R 用, サブコマンド 0040)
    """
    try:
        return _file_info(
            drive, start_no, count,
            path   = path   or "",
            ip     = ip     or PLC_IP,
            port   = port   or PLC_PORT
        )
    except MCProtocolError as ex:
        raise HTTPException(status_code=400, detail=str(ex))


def _file_info(
    drive: int, start_no: int, count: int,
    *, path: str, ip: str, port: int
):
    """
    実際の PLC へ 1810h を投げ、raw をパースして JSON へ整形
    """
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT_SEC * 4)
    plc.connect(ip, port)

    try:
        _, infos = plc.read_directory_fileinfo(
            drive_no      = drive,
            start_file_no = start_no,
            request_count = count,
            iqr           = True,          # iQ-R 用サブコマンド 0040 固定
            path          = path
        )

        raw = infos[0]["raw"]
        # デバッグ用 (必要ならコメントアウト)
        # print("1810h raw:", binascii.hexlify(raw).decode())

        files = parse_iqr_fileinfo(raw)
        return {"files": files}

    finally:
        plc.close()
