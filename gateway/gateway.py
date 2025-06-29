# ------------------------------------------------------------
# gateway.py
# FastAPI Gateway ─ 1810 / 1811 File-API & Device Read
# ------------------------------------------------------------
import os
import json
import base64
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pymcprotocol import Type3E
from pymcprotocol.mcprotocolerror import MCProtocolError

from plc_filecontrol import PlcFileControl 

# ──────────────────── 環境変数 ────────────────────
PLC_IP      = os.getenv("PLC_IP",         "127.0.0.1")
PLC_PORT    = int(os.getenv("PLC_PORT",   "5511"))
TIMEOUT_SEC = float(os.getenv("PLC_TIMEOUT_SEC", "3.0"))
PLC_DRIVE   = int(os.getenv("PLC_DRIVE",  "4"))

# ──────────────────── FastAPI ────────────────────
app = FastAPI(title="PLC Gateway")
file_ctl = PlcFileControl(PLC_IP, PLC_PORT, TIMEOUT_SEC)

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
    drive:    int,
    filename: str,
    path:     str | None = "",            # "",  PROG_FILES,  $TMP$ など
    ip:       str | None = None,
    port:     int | None = None,
    debug:    bool = False
):
    plc = Type3E(plctype="iQ-R")
    plc.setaccessopt(commtype="binary")
    plc.timer = int(TIMEOUT_SEC * 4)
    plc.connect(ip or PLC_IP, port or PLC_PORT)

    if debug:
        plc._set_debug(True)

    # ---------- 1811 用 / 1810 用パス生成 ----------
    if not path:                          # ルート
        dir1811 = ""                      # ← 空文字
        dir1810 = "$MELPRJ$"
    elif path.startswith("$"):
        core   = path.rstrip("\\")
        dir1811 = core + "\\"
        dir1810 = core
    else:
        core   = rf"$MELPRJ$\{path}".rstrip("\\")
        dir1811 = core + "\\"
        dir1810 = core

    try:
        # ---------- ① 1811h ----------
        rsp = plc.read_search_fileinfo(
            drive_no = drive,
            filename = filename,
            directory = dir1811
        )
        return { "files": _parse_iqr(rsp["raw"]) }

    except MCProtocolError as ex:
        # ----- エラーコードを安全に取り出す -----
        ec = getattr(ex, "errorcode", None)
        if ec is None:
            raw = ex.args[0]
            if isinstance(raw, int):
                ec = f"0x{raw:04X}"
            else:
                import re
                m = re.search(r"0x[0-9A-Fa-f]+", str(raw))
                ec = m.group(0) if m else str(raw)

        # ---------- ② 1810h フォールバック ----------
        if ec.upper() == "0XC061":        # 指定ファイル無し
            files = []
            start = 1
            while True:
                cnt, info = plc.read_directory_fileinfo(
                    drive_no       = drive,
                    start_file_no  = start,
                    request_count  = 256,
                    iqr            = True,
                    path           = dir1810
                )
                files += _parse_iqr(info[0]["raw"])
                if cnt < 256:
                    break
                start += cnt

            hits = [
                f for f in files
                if f"{f['name']}.{f['ext']}".upper() == filename.upper()
            ]
            if hits:
                return { "files": hits }

        # ---------- ③ どうしても無い / 別コード ----------
        raise HTTPException(
            status_code = 404 if ec.upper() == "0XC061" else 500,
            detail      = f"MC-Protocol error {ec}"
        )

    finally:
        plc.close()

# ------------------------------------------------------------
# 1827 / 1828 / 182A 追加 API
# ------------------------------------------------------------
class FileOpenReq(BaseModel):
    drive: int = PLC_DRIVE
    filename: str
    mode: str = "r"

class FileReadReq(BaseModel):
    fp: int
    offset: int = 0
    length: int = 1024            # 0-1920

class FileCloseReq(BaseModel):
    fp: int


@app.post("/api/file/open")
def api_file_open(req: FileOpenReq):
    try:
        fp = file_ctl.open_file(drive=req.drive,
                                filename=req.filename,
                                mode=req.mode)
        return {"fp": fp}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))


@app.post("/api/file/read")
def api_file_read(req: FileReadReq):
    try:
        data = file_ctl.read_file(fp_no=req.fp,
                                  offset=req.offset,
                                  length=req.length)
        return {"size": len(data),
                "data": base64.b64encode(data).decode()}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))


@app.post("/api/file/close")
def api_file_close(req: FileCloseReq):
    try:
        file_ctl.close_file(fp_no=req.fp)
        return {"result": "ok"}
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))
    
# ------------------------------------------------------------
# 1827 / 1828 / 182A 追加 API – 1 回 1920 B 制限付きで
#                   ファイルを最後まで読み、JSON 化して保存
# ------------------------------------------------------------
class FileReadAllReq(BaseModel):
    """
    MAIN.PRG のようなファイルを全バイト読出して
    <filename>.json へ保存するリクエスト
    """
    drive: int = PLC_DRIVE
    filename: str                       # 例: "MAIN.PRG" あるいは "$MELPRJ$\\MAIN.PRG"
    chunk: int = 1920                   # 1828h 制約: 0-1920 byte
    json_path: Optional[str] = None     # 未指定なら filename+".json" に保存


@app.post("/api/file/readall")
def api_file_readall(req: FileReadAllReq):
    """
    1. 1827h でファイルを開く
    2. 1828h を chunk サイズずつ繰返して全バイト取得
    3. 182Ah でクローズ
    4. Base64 エンコードして JSON ファイルへ書出し
    """
    if req.chunk <= 0 or req.chunk > 1920:
        raise HTTPException(status_code=400,
                            detail="chunk は 1-1920 byte で指定してください")

    # ---------- ① ファイルを開く ----------
    try:
        fp = file_ctl.open_file(drive=req.drive,
                                filename=req.filename,
                                mode="r")
    except Exception as ex:
        raise HTTPException(status_code=500, detail=str(ex))

    try:
        # ---------- ② 全バイト読出し ----------
        offset = 0
        content = b""

        while True:
            part = file_ctl.read_file(fp_no=fp,
                                      offset=offset,
                                      length=req.chunk)
            if not part:
                break                    # 読出し完了
            content += part
            if len(part) < req.chunk:
                break                    # 最終チャンク
            offset += req.chunk

        # ---------- ③ JSON へ保存 ----------
        json_file = req.json_path or f"{os.path.basename(req.filename)}.json"
        record = {
            "filename": req.filename,
            "size":     len(content),
            "data":     base64.b64encode(content).decode("ascii")
        }
        with open(json_file, "w", encoding="utf-8") as fp_json:
            json.dump(record, fp_json, ensure_ascii=False, indent=2)

        # ---------- ④ 結果 ----------
        return {
            "filename": req.filename,
            "bytes_read": len(content),
            "json_saved": json_file
        }

    finally:
        # ---------- ⑤ 必ずクローズ ----------
        try:
            file_ctl.close_file(fp_no=fp)
        except Exception:
            pass