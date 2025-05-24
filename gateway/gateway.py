# ------------------------------------------------------------
# FastAPI Gateway ─ 1810 / 1811 File-API & Device Read
# ------------------------------------------------------------
import os
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymcprotocol import Type3E
from pymcprotocol.mcprotocolerror import MCProtocolError

app = FastAPI(title="PLC Gateway")

PLC_IP      = os.getenv("PLC_IP",         "127.0.0.1")
PLC_PORT    = int(os.getenv("PLC_PORT",   "5511"))
TIMEOUT_SEC = float(os.getenv("PLC_TIMEOUT_SEC", "3.0"))
PLC_DRIVE   = int(os.getenv("PLC_DRIVE",  "4"))

# ─────────────────── Device Read (既存) ───────────────────
class ReadRequest(BaseModel):
    device: str = "D"
    addr:   int
    length: int
    ip:     Optional[str] = None
    port:   Optional[int] = None


def _read_plc(device: str, addr: int, length: int, *, ip: str, port: int) -> List[int]:
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT_SEC * 4)
    plc.connect(ip, port)
    try:
        upper = device.upper()
        if upper in ("D", "W", "R", "ZR"):
            return plc.batchread_wordunits(f"{upper}{addr}", length)
        if upper in ("X", "Y", "M"):
            return plc.batchread_bitunits(f"{upper}{addr}", length)
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
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))


@app.get("/api/read/{device}/{addr}/{length}")
def api_read_get(device: str, addr: int, length: int,
                 ip: Optional[str] = None, port: Optional[int] = None):
    return api_read(ReadRequest(device=device, addr=addr, length=length,
                                ip=ip, port=port))

# ─────────────────── 共通パーサ（iQ-R UTF-16） ───────────────────
def _parse_iqr(raw: bytes) -> list[dict]:
    files, idx = [], 4
    while idx + 2 <= len(raw):
        chars = int.from_bytes(raw[idx:idx + 2], "little")
        if chars in (0, 0xFFFF):
            break
        idx += 2
        name = raw[idx:idx + chars * 2].decode("utf-16le", "ignore").rstrip("\x00")
        idx += chars * 2
        attr = raw[idx]
        idx += 1 + 9 + 3 + 4          # 予約領域＋日付時刻＋サイズ
        size = int.from_bytes(raw[idx:idx + 4], "little")
        idx += 4
        base, _, ext = name.partition(".")
        files.append({"name": base, "ext": ext, "size": size, "attribute": attr})
    return files

# ─────────────────── 1810 ディレクトリ一覧 (変更なし) ───────────────────
@app.get("/api/fileinfo/{drive}")
def api_fileinfo(
    drive: int,
    start_no: int = 1,
    count: int = 36,
    path: str | None = "$MELPRJ$",
    ip: Optional[str] = None,
    port: Optional[int] = None
):
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT_SEC * 4)
    plc.connect(ip or PLC_IP, port or PLC_PORT)
    try:
        _, info = plc.read_directory_fileinfo(
            drive_no=drive,
            start_file_no=start_no,
            request_count=count,
            iqr=True,
            path=path or ""
        )
        files = _parse_iqr(info[0]["raw"])
        return {"files": files}
    finally:
        plc.close()

# ─────────────────── 1811 ファイル検索 ───────────────────
@app.get("/api/filesearch/{drive}")
def api_filesearch(
    drive: int,
    filename: str,
    path: str | None = "$MELPRJ$",
    ip: str | None = None,
    port: int | None = None,
):
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT_SEC * 4)
    plc.connect(ip or PLC_IP, port or PLC_PORT)

    try:
        rsp = plc.read_search_fileinfo(
            drive_no=drive,
            filename=filename,
            directory=path            # ★ 復活させる（空文字可）
        )
        files = _parse_iqr(rsp["raw"])
        return {"files": files}
    
    except MCProtocolError as ex:
        if ex.status == 0xC061:       # 指定ファイル無し
            raise HTTPException(status_code=404, detail="file not found")
        else:
            raise HTTPException(status_code=500, detail=str(ex))

    finally:
        plc.close()
        