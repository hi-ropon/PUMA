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

def _clean_filename(utf16: str) -> str:
    # ---- 直接 Unicode 文字列を検索 ----
    m = re.findall(r'[\$A-Za-z0-9_\-]{1,64}\.[A-Za-z0-9]{1,4}', utf16)
    if m:
        return m[-1]                       # 拡張子あり

    m = re.findall(r'[\$A-Za-z0-9_\-]{1,64}', utf16)
    return m[-1] if m else utf16.strip('\x00')

def parse_iqr_fileinfo(raw: bytes) -> list[dict]:
    """
    MELSEC iQ-R 1810h/0040h Directory / File-Info 解析（確定版）
    戻り値例:
      [{'name':'SY_SYSTEM','ext':'PRM','size':1048612,'attribute':0x24}, …]
    """
    files: list[dict] = []
    idx = 4                                      # 先頭 4B = Last-File-No + Reserved

    while idx + 4 <= len(raw):
        file_no = int.from_bytes(raw[idx:idx+2], 'little')
        if file_no == 0xFFFF:                    # 終端
            break
        idx += 2

        var_len = int.from_bytes(raw[idx:idx+2], 'little')
        idx += 2

        # ───────── 固定 18 バイト ──────────
        attr  = int.from_bytes(raw[idx:idx+2], 'little'); idx += 2
        idx  += 2             # 予約
        idx  += 4 + 4         # 更新時刻 + 更新日付
        idx  += 2             # 予約
        size  = int.from_bytes(raw[idx:idx+4], 'little'); idx += 4

        # ───────── 可変長部（後ろから読む）───
        var_end   = idx + var_len
        # 1) FileNameLen
        fname_len = int.from_bytes(raw[var_end-2:var_end], 'big')
        # 2) FileName
        fname_beg = var_end - 2 - fname_len*2
        fname     = raw[fname_beg:fname_beg + fname_len*2].decode('utf-16le','ignore')
        # 3) LinkInfoLen（FileName の直前）
        link_len  = int.from_bytes(raw[fname_beg-2:fname_beg], 'big')
        # LinkInfo は使わないが，位置を合わせるため読み飛ばす
        idx = var_end - 2 - fname_len*2 - 2 - link_len

        # ───────── 整形 ──────────
        #   余計なパス／制御文字を除去して「最後の ASCII 名」を拾う
        clean = _clean_filename(fname)

        base, _, ext = clean.partition('.')
        files.append({
            "name": base or ".",                # "." / ".." もそのまま返す
            "ext":  ext,
            "size": size,
            "attribute": attr & 0xFF,           # 0x10 → ディレクトリ
        })

        # 次のエントリへ
        idx = var_end

    return files

@app.get("/api/fileinfo/{drive}")
def api_fileinfo_get(
        drive: int,
        start_no: int = 1,
        count: int = 36,
        path: str | None = "$MELPRJ$",
        ip: Optional[str] = None,
        port: Optional[int] = None):
    try:
        return _file_info(drive, start_no, count,
                        path=path,
                        ip=ip or PLC_IP, port=port or PLC_PORT)
    except MCProtocolError as ex:
        raise HTTPException(status_code=400, detail=str(ex))

def _file_info(drive: int, start_no: int, count: int, *,
               path: str = "$MELPRJ$", ip: str, port: int):
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
        print("1810h raw hex:\n%s", binascii.hexlify(raw).decode())
        
        infos = parse_iqr_fileinfo(infos[0]["raw"])
        return {"files": infos}
    finally:
        plc.close()
