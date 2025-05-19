# .\venv\Scripts\Activate.ps1
# uvicorn gateway:app --host 127.0.0.1 --port 8001
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent / "libs"))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
from typing import Optional
from pymcprotocol import Type3E

app = FastAPI(title="PLC Gateway")

PLC_IP   = os.getenv("PLC_IP",   "127.0.0.1")
PLC_PORT = int(os.getenv("PLC_PORT", "5511"))
TIMEOUT  = 3.0  # 秒

class ReadRequest(BaseModel):
    device: str = "D"  # デバイス種別 (例: D, X, Y, M)
    addr: int   # アドレス
    length: int # 読み取り点数
    ip: Optional[str] = None
    port: Optional[int] = None

def read_plc(device: str, start: int, length: int, ip: str = PLC_IP, port: int = PLC_PORT) -> list[int]:
    plc = Type3E(plctype="iQ-R")
    plc.timer = int(TIMEOUT * 4)  # 例: 3秒 → timer=12
    plc.connect(ip, port)
    try:
        dev = device.upper()
        if dev in ("D", "W", "R", "ZR"):
            data = plc.batchread_wordunits(f"{dev}{start}", length)
        elif dev in ("X", "Y", "M"):
            data = plc.batchread_bitunits(f"{dev}{start}", length)
        else:
            raise ValueError(f"Unsupported device '{device}'")
        return data
    finally:
        plc.close()

@app.post("/api/read")
def api_read(req: ReadRequest):
    try:
        ip = req.ip or PLC_IP
        port = req.port or PLC_PORT
        values = read_plc(req.device, req.addr, req.length, ip=ip, port=port)
        return {"values": values}
    except Exception as ex:
        import traceback, sys
        traceback.print_exc(file=sys.stdout)
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")


@app.get("/api/read/{device}/{addr}/{length}")
def api_read_get(device: str, addr: int, length: int, ip: Optional[str] = None, port: Optional[int] = None):
    try:
        ip = ip or PLC_IP
        port = port or PLC_PORT
        values = read_plc(device, addr, length, ip=ip, port=port)
        return {"values": values}
    except Exception as ex:
        import traceback, sys
        traceback.print_exc(file=sys.stdout)
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")
