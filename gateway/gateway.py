# .\venv\Scripts\Activate.ps1
# uvicorn gateway:app --host 127.0.0.1 --port 8001

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import os
from pymcprotocol import Type3E

app = FastAPI(title="PLC Gateway")

PLC_IP   = os.getenv("PLC_IP",   "127.0.0.1")
PLC_PORT = int(os.getenv("PLC_PORT", "5511"))
TIMEOUT  = 3.0  # 秒

class ReadRequest(BaseModel):
    addr: int   # D レジスタ開始アドレス
    length: int # 読み取り語数 (1語=16bit)

def read_plc(start: int, length: int) -> list[int]:
    plc = Type3E(plctype="iQ-R")
    plc.timer = int(TIMEOUT * 4)  # 例: 3秒 → timer=12
    plc.connect(PLC_IP, PLC_PORT)
    try:
        data = plc.batchread_wordunits(f"D{start}", length)
        return data
    finally:
        plc.close()

@app.post("/api/read")
def api_read(req: ReadRequest):
    try:
        values = read_plc(req.addr, req.length)
        return {"values": values}
    except Exception as ex:
        import traceback, sys
        traceback.print_exc(file=sys.stdout)
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")


@app.get("/api/read/{addr}/{length}")
def api_read_get(addr: int, length: int):
    try:
        values = read_plc(addr, length)
        return {"values": values}
    except Exception as ex:
        import traceback, sys
        traceback.print_exc(file=sys.stdout)
        raise HTTPException(status_code=500, detail=f"PLC read error: {ex}")
