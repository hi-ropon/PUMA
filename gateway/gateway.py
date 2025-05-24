# .\venv\Scripts\Activate.ps1
# uvicorn gateway:app --host 127.0.0.1 --port 8001
# gateway.py
# ------------------------------------------------------------
# FastAPI Gateway  ─ デバイス読取 & ファイル情報 (1810) API
# uvicorn gateway:app --host 127.0.0.1 --port 8001
# ------------------------------------------------------------

import binascii
import re
from typing import List, Dict, Optional
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymcprotocol import Type3E
from pymcprotocol.mcprotocolerror import MCProtocolError 

app = FastAPI(title="PLC Gateway")

PLC_IP   = os.getenv("PLC_IP",   "127.0.0.1")
PLC_PORT = int(os.getenv("PLC_PORT", "5511"))
TIMEOUT  = float(os.getenv("PLC_TIMEOUT_SEC", "3.0"))  # sec
PLC_DRIVE = int(os.getenv("PLC_DRIVE", "4"))
_HEX_BYTES = set(b"0123456789ABCDEF")
ASCII_NAME = re.compile(rb'[\$A-Za-z0-9_\-]{1,64}(?:\.[A-Za-z0-9]{1,4})?')

# ──────────────────── デバイス値読取 API (既存) ────────────────────
class ReadRequest(BaseModel):
    device: str = "D"
    addr: int
    length: int
    ip: Optional[str] = None
    port: Optional[int] = None


# ──────────────────── デバイス値読取 helper ────────────────────
def _read_plc(device: str, addr: int, length: int, *, ip: str, port: int) -> List[int]:
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT * 4)
    plc.connect(ip, port)
    try:
        dev = device.upper()
        if dev in ("D", "W", "R", "ZR"):
            return plc.batchread_wordunits(f"{dev}{addr}", length)
        if dev in ("X", "Y", "M"):
            return plc.batchread_bitunits (f"{dev}{addr}", length)
        raise ValueError(f"Unsupported device '{device}'")
    finally:
        plc.close()

@app.post("/api/read")
def api_read(req: ReadRequest):
    try:
        vals = _read_plc(req.device, req.addr, req.length,
                         ip=req.ip or PLC_IP, port=req.port or PLC_PORT)
        return {"values": vals}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")


@app.get("/api/read/{device}/{addr}/{length}")
def api_read_get(device: str, addr: int, length: int,
                 ip: Optional[str] = None, port: Optional[int] = None):
    try:
        vals = _read_plc(device, addr, length,
                         ip=ip or PLC_IP, port=port or PLC_PORT)
        return {"values": vals}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")


# ──────────────────── ファイル情報 API 1810 ────────────────────
class FileInfoRequest(BaseModel):
    drive: int = PLC_DRIVE
    start_no: int = 1
    count: int = 36
    ip: Optional[str] = None
    port: Optional[int] = None

def _clean_filename(s: str) -> str:
    s = s.replace('\x00', '').strip()            # NUL, 前後スペース除去
    m = re.findall(r'[\$A-Za-z0-9_\-]{1,64}\.[A-Za-z0-9]{1,4}', s)
    if m:  return m[-1]
    m = re.findall(r'[\$A-Za-z0-9_\-]{1,64}', s)
    return m[-1] if m else s

import re

ASCII_NAME = re.compile(r'[\$A-Za-z0-9_\-]{1,64}(?:\.[A-Za-z0-9]{1,4})?')

def parse_iqr_fileinfo(raw: bytes) -> list[dict]:
    files = []
    # ヘッダ (LastFileNo + Reserved) 4 byte をスキップ
    idx = 4

    while idx + 4 <= len(raw):
        # 1) ファイル番号（2 byte, リトル）
        file_no = int.from_bytes(raw[idx:idx+2], 'little')
        if file_no == 0xFFFF:
            break
        idx += 2

        # 2) 可変長部のバイト数（2 byte, リトル）
        var_len = int.from_bytes(raw[idx:idx+2], 'little')
        idx += 2

        # 3) 固定部 (属性＋予約＋更新時刻＋更新日付＋予約＋サイズ) の読み飛ばしと取得
        attr = int.from_bytes(raw[idx:idx+2], 'little')
        idx += 2
        idx += 2 + 4 + 4 + 2  # 予約 + 日時 + 予約
        size = int.from_bytes(raw[idx:idx+4], 'little')
        idx += 4

        # 4) 可変部本体をスライス
        var_start = idx
        var_end   = var_start + var_len
        var_bytes = raw[var_start:var_end]

        # 5) 末尾からファイル名長 (2 byte, ビッグ) を読み取って、UTF-16LE 名を逆引き
        name = ""
        if len(var_bytes) >= 2:
            name_len = int.from_bytes(var_bytes[-2:], 'big')
            start = len(var_bytes) - 2 - name_len*2
            if start >= 0:
                name = var_bytes[start:start + name_len*2].decode('utf-16le', 'ignore')

        # 6) 最後の ASCII 部分だけクリーンアップ
        m = ASCII_NAME.findall(name)
        clean = m[-1] if m else name.strip('\x00') or "."

        base, _, ext = clean.partition('.')
        files.append({
            "name":      base,
            "ext":       ext,
            "size":      size,
            "attribute": attr & 0xFF,  # 0x10 → ディレクトリ
        })

        # 7) 次レコードへ
        idx = var_end

    return files

@app.get("/api/fileinfo/{drive}")
def api_fileinfo_get(
    drive: int,
    start_no: int = 1,
    count: int    = 36,
    path: str | None = "$MELPRJ$",
    ip: Optional[str] = None,
    port: Optional[int] = None
):
    try:
        return _file_info(
            drive, start_no, count,
            path   = path,
            ip     = ip   or PLC_IP,
            port   = port or PLC_PORT
        )
    except MCProtocolError as ex:
        raise HTTPException(status_code=400, detail=str(ex))

def _file_info(
    drive: int, start_no: int, count: int,
    *, path: str, ip: str, port: int
):
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT * 4)
    plc.connect(ip, port)
    try:
        _, infos = plc.read_directory_fileinfo(
            drive_no      = drive,
            start_file_no = start_no,
            request_count = count,
            iqr           = True,
            path          = path
        )
        raw = infos[0]["raw"]
        print(f"info: {infos[0]}")
        print(f"info raw:{infos[0]['raw']}")
        # デバッグ用に生データを出力
        print(f"1810h raw hex:\n{binascii.hexlify(raw).decode()}")
        files = parse_iqr_fileinfo(raw)
        return {"files": files}
    finally:
        plc.close()