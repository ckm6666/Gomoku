"""登录页“记住密码”的本地凭据保存。

密码不会以明文写入磁盘：Windows 上使用 DPAPI（``CryptProtectData``）
按当前 Windows 用户加密，密文复制到其他账户/机器无法解密。非 Windows
平台不保存密码，只记住用户名。
"""

from __future__ import annotations

import base64
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional, Tuple

from PySide6.QtCore import QSettings

logger = logging.getLogger(__name__)

#: QSettings 键名。放在 online/ 分组下，避免和其他偏好冲突。
KEY_REMEMBER = "online/remember_password"
KEY_USERNAME = "online/saved_username"
KEY_PASSWORD = "online/saved_password"

_PREFIX = "dpapi:"
_ENTROPY = b"gomoku-online-login-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob(data: bytes) -> Tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    """调用 Windows DPAPI 加密/解密；失败时抛 ``OSError``。"""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]

    in_blob, in_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    out_blob = _DataBlob()
    # 保留缓冲区引用，直到 DPAPI 调用返回。
    _ = (in_buffer, entropy_buffer)
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(
            ctypes.cast(out_blob.pbData, wintypes.HLOCAL)
        )


def protect_password(password: str) -> Optional[str]:
    """把密码加密成可写入 QSettings 的字符串；不支持时返回 None。"""
    if not password or sys.platform != "win32":
        return None
    try:
        encrypted = _dpapi(password.encode("utf-8"), protect=True)
    except OSError:
        logger.exception("DPAPI 加密登录密码失败")
        return None
    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_password(token: str) -> Optional[str]:
    """解密保存的密码；数据损坏或平台不支持时返回 None。"""
    if sys.platform != "win32" or not token.startswith(_PREFIX):
        return None
    try:
        encrypted = base64.b64decode(
            token[len(_PREFIX) :], validate=True
        )
        return _dpapi(encrypted, protect=False).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        logger.warning("已保存的登录密码无法解密，已忽略。")
        return None


def save_login(
    settings: QSettings, username: str, password: str
) -> bool:
    """保存登录信息；返回密码是否已安全加密保存。"""
    settings.setValue(KEY_REMEMBER, True)
    settings.setValue(KEY_USERNAME, username)
    token = protect_password(password)
    if token is None:
        settings.remove(KEY_PASSWORD)
        settings.sync()
        return False
    settings.setValue(KEY_PASSWORD, token)
    settings.sync()
    return True


def load_login(settings: QSettings) -> Tuple[bool, str, str]:
    """读取登录信息，返回 ``(是否记住, 用户名, 密码)``。"""
    remember = bool(settings.value(KEY_REMEMBER, False, type=bool))
    username = settings.value(KEY_USERNAME, "", type=str) or ""
    token = settings.value(KEY_PASSWORD, "", type=str) or ""
    password = unprotect_password(token) if remember and token else ""
    return remember, username, password


def clear_login(settings: QSettings) -> None:
    """删除已保存的登录信息。"""
    for key in (KEY_REMEMBER, KEY_USERNAME, KEY_PASSWORD):
        settings.remove(key)
    settings.sync()


__all__ = [
    "KEY_PASSWORD",
    "KEY_REMEMBER",
    "KEY_USERNAME",
    "clear_login",
    "load_login",
    "protect_password",
    "save_login",
    "unprotect_password",
]
