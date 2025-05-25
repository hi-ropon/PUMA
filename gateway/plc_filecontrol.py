"""
plc_filecontrol.py
------------------------------------
MCプロトコル 1827 / 1828 / 182A をラップする小さなユーティリティ
• open_file()  – ファイルをロックして開き File-Pointer を取得
• read_file()  – 任意オフセットをバイト単位で読出し (～1920 B/回)
• close_file() – ロック解除
"""
from __future__ import annotations

import base64
from typing import Tuple, List

from pymcprotocol import Type3E
from pymcprotocol.mcprotocolerror import MCProtocolError


class PlcFileControl:
    """ワンショット接続で 1827/1828/182A を実行する薄いラッパ"""

    def __init__(self, ip: str, port: int, timeout_sec: float = 3.0) -> None:
        self._ip = ip
        self._port = port
        self._timeout = timeout_sec

    # ───────────────────────── 内部ヘルパ ──────────────────────────
    def _connect(self) -> Type3E:
        plc = Type3E(plctype="iQ-R")                 # ★ iQ-R 固定
        plc.setaccessopt(commtype="binary")
        plc.timer = int(self._timeout * 4)
        plc.connect(self._ip, self._port)
        return plc

    @staticmethod
    def _send(plc: Type3E, cmd: int, sub: int, param: bytes) -> bytes:
        req = plc._make_commanddata(cmd, sub) + param
        plc._send(plc._make_senddata(req))
        rsp = plc._recv()
        plc._check_cmdanswer(rsp)
        return rsp

    # ───────────────────────── 公開 API ────────────────────────────
    def open_file(self, *, drive: int, filename: str,
                mode: str = "r", password: str = "") -> int:
        """
        1827h – Open file (iQ-R, sub 0x0040)
        """
        import ntpath, binascii
        plc = self._connect()
        try:
            sub   = 0x0040
            param = b""

            # ① パスワード
            pwd = password.encode("ascii")
            param += plc._encode_value(len(pwd), "short") + pwd      # = 0000

            # ② オープンモード  (先!)
            open_mode = 0x0100 if mode.lower() == "w" else 0x0000
            param += plc._encode_value(open_mode, "short")           # = 0000

            # ③ ドライブ No.
            param += plc._encode_value(drive, "short")               # = 0004

            # ④ ファイル名 (“MAIN.PRG” だけ / 先頭 \ 不要)
            # ④ ファイル名（$MELPRJ$\MAIN.PRG などをまとめて渡す）
            _, base = ntpath.split(filename)
            if not base:
                base = filename                    # 引数がフルパスの場合も対応

            if not base.upper().startswith("$MELPRJ$"):
                fullpath = f"$MELPRJ$\\{base}".upper()
            else:
                fullpath = base.upper()

            raw = fullpath.encode("utf-16le")
            param += plc._encode_value(len(fullpath), "short") + raw

            # 送信ダンプ (確認用)
            print("SEND:", binascii.hexlify(param))
            # 期待: 0000 0000 0400 0800 4d0041...

            rsp = self._send(plc, 0x1827, sub, param)
            idx = plc._get_answerdata_index()
            fp  = plc._decode_value(rsp[idx:idx + 2], "short")
            print("FP =", fp)
            return fp
        finally:
            plc.close()

    def read_file(self, *, fp_no: int, offset: int,
                  length: int = 1024) -> bytes:
        """
        ファイルから length B 読み出す。戻り値は生バイト列
        """
        if length > 1920:
            raise ValueError("length は 0-1920 byte で指定してください")

        plc = self._connect()
        try:
            sub = 0x0040
            param  = plc._encode_value(fp_no, "short")
            param += plc._encode_value(offset, "long")
            param += plc._encode_value(length, "short")

            rsp = self._send(plc, 0x1828, sub, param)
            idx = plc._get_answerdata_index()
            read_len = plc._decode_value(rsp[idx:idx + 2], "short")
            return rsp[idx + 2: idx + 2 + read_len]
        finally:
            plc.close()

    def close_file(self, *, fp_no: int, close_type: int = 0) -> None:
        """
        ファイルをクローズ（182Ah）— close_type=0 で単一 FP を解放
        """
        plc = self._connect()
        try:
            sub = 0x0040
            param  = plc._encode_value(fp_no, "short")
            param += plc._encode_value(close_type, "short")
            self._send(plc, 0x182A, sub, param)    # 応答データなし
        finally:
            plc.close()
