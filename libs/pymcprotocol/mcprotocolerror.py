"""This file is collection of mcprotocol error.

"""

class MCProtocolError(Exception):
    """devicecode error. Device is not exsist.

    Attributes:
        plctype(str):       PLC type. "Q", "L" or "iQ"
        devicename(str):    devicename. (ex: "Q", "P", both of them does not support mcprotocol.)

    """
    def __init__(self, code: int):
        self.status     = code
        self.code = f"0x{code:04X}"
        super().__init__(f"mc protocol error: error code {self.code}")

    def __str__(self) -> str:
        return f"MC-Protocol error (end-code {self.code})"

class UnsupportedComandError(MCProtocolError):
    """This command is not supported by the module you connected.  

    """
    def __init__(self):
        super().__init__(0xC059)

    def __str__(self):
        return ("The connected module does not support this command. "
                "If you are using a CPU unit, please insert an E71 Ethernet module.")
    
# ──────────── 1810 系ファイル制御エラー ────────────
class FileControlError(MCProtocolError): ...

class FileNotFoundError(FileControlError):
    def __init__(self): super().__init__(0xC051)
    def __str__(self): return "File or directory not found on PLC."

class DriveNotFoundError(FileControlError):
    def __init__(self): super().__init__(0xC052)
    def __str__(self): return "Specified drive number does not exist."

class AccessDeniedError(FileControlError):
    def __init__(self): super().__init__(0xC053)
    def __str__(self): return "Access to the file or drive is denied."

# end-code → 例外マップ
_error_map = {
    0x0000: None,
    0xC051: FileNotFoundError,
    0xC052: DriveNotFoundError,
    0xC053: AccessDeniedError,
    0xC059: UnsupportedComandError,
}
    
def check_mcprotocol_error(status: int):
    """非 0 の end-code を受け取ったら適切な例外を送出"""
    exc = _error_map.get(status)
    if status == 0:
        return
    if exc is None:
        raise MCProtocolError(status)
    raise exc()

        