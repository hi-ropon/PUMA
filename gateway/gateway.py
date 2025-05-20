# .\venv\Scripts\Activate.ps1
# uvicorn gateway:app --host 127.0.0.1 --port 8001
# gateway.py
# ------------------------------------------------------------
# FastAPI Gateway  ─ デバイス読取 & ファイル情報 (1810) API
# uvicorn gateway:app --host 127.0.0.1 --port 8001
# ------------------------------------------------------------

from typing import List, Optional
import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymcprotocol import Type3E
from pymcprotocol.mcprotocolerror import MCProtocolError 

app = FastAPI(title="PLC Gateway")

PLC_IP   = os.getenv("PLC_IP",   "127.0.0.1")
PLC_PORT = int(os.getenv("PLC_PORT", "5511"))
TIMEOUT  = float(os.getenv("PLC_TIMEOUT_SEC", "3.0"))  # sec


# ──────────────────── デバイス値読取 API (既存) ────────────────────
class ReadRequest(BaseModel):
    device: str = "D"
    addr: int
    length: int
    ip: Optional[str] = None
    port: Optional[int] = None


def _read_plc(device: str, addr: int, length: int, *, ip: str, port: int) -> List[int]:
    plc = Type3E(plctype="iQ-R")
    plc.timer = int(TIMEOUT * 4)
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
    drive: int = 0
    start_no: int = 1
    count: int = 36
    ip: Optional[str] = None
    port: Optional[int] = None

def parse_iqr_fileinfo(raw: bytes) -> list[dict]:
    """iQ-R 1810h/0040h の可変長ファイル情報を解析"""
    idx = 0
    entries = []
    while idx < len(raw):
        # ① レコード長（ byte ）を取得
        rec_len = int.from_bytes(raw[idx:idx+2], "little")  # 2B
        idx += 2
        rec = raw[idx: idx + rec_len]

        # ② 文字列長（UTF-16 文字数）を取得
        name_len = int.from_bytes(rec[0:2], "little")       # 2B
        p = 2                                              # オフセット

        # ③ ファイル名（可変, UTF-16LE, NUL 終端なし）
        name_bytes = rec[p : p + name_len*2]
        name = name_bytes.decode("utf-16le")
        p += name_len*2

        # ④ 拡張子長
        ext_len = int.from_bytes(rec[p: p+2], "little");  p += 2
        ext = rec[p : p + ext_len*2].decode("utf-16le");  p += ext_len*2

        # ⑤ 属性・サイズほか（固定4+4+1B）
        attr = rec[p];               p += 1
        size = int.from_bytes(rec[p:p+4], "little")

        entries.append({
            "name": name or ".",     # 空なら "." 表示
            "ext":  ext,
            "size": size,
            "attribute": attr,
        })
        idx += rec_len
    return entries

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

def _file_info(drive: int,
               start_no: int,
               count: int,
               *,
               path: str = "$MELPRJ$",
               ip: str,
               port: int):
    plc = Type3E(plctype="iQ-R")
    plc.timer = int(TIMEOUT * 4)
    plc.connect(ip, port)
    try:
        _, infos = plc.read_directory_fileinfo(
            drive_no        = drive,
            start_file_no   = start_no,
            request_count   = count,
            iqr             = True,
            path            = path
        )
        infos = parse_iqr_fileinfo(infos[0]["raw"])
        return {"files": infos}
    finally:
        plc.close()

