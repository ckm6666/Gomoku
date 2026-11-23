"""登录页“记住密码”的本地凭据保存。

密码与 refresh token 都不会以明文写入磁盘：Windows 上使用 DPAPI
（``CryptProtectData``）按当前 Windows 用户加密，密文复制到其他账户/
机器无法解密。非 Windows 平台不保存密码或 token，只记住用户名。
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
KEY_REFRESH_TOKEN = "online/refresh_token"

_PREFIX = "dpapi:"
_ENTROPY = b"gomoku-online-login-v1"
_TOKEN_ENTROPY = b"gomoku-online-session-v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    """DPAPI 的 DATA_BLOB。

    ``cbData`` 用定宽的 ``c_uint32`` 而不是 ``wintypes.DWORD``：DWORD 在
    Windows 上是 4 字节，但 ``wintypes.DWORD`` 实际是 ``c_ulong``，在
    LP64 平台（Linux/macOS）上会是 8 字节，结构体布局就错了。
    """

    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob(data: bytes) -> Tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def _dpapi(
    data: bytes, *, protect: bool, entropy: bytes = _ENTROPY
) -> bytes:
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
    entropy_blob, entropy_buffer = _blob(entropy)
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


def protect_refresh_token(token: str) -> Optional[str]:
    """把 refresh token 加密成可写入 QSettings 的字符串。"""
    if not token or sys.platform != "win32":
        return None
    try:
        encrypted = _dpapi(
            token.encode("utf-8"), protect=True, entropy=_TOKEN_ENTROPY
        )
    except OSError:
        logger.exception("DPAPI 加密 refresh token 失败")
        return None
    return _PREFIX + base64.b64encode(encrypted).decode("ascii")


def unprotect_refresh_token(value: str) -> Optional[str]:
    """解密保存的 refresh token；数据损坏或平台不支持时返回 None。"""
    if sys.platform != "win32" or not value.startswith(_PREFIX):
        return None
    try:
        encrypted = base64.b64decode(
            value[len(_PREFIX) :], validate=True
        )
        return _dpapi(
            encrypted, protect=False, entropy=_TOKEN_ENTROPY
        ).decode("utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        logger.warning("已保存的 refresh token 无法解密，已忽略。")
        return None


def save_refresh_token(settings: QSettings, token: str) -> bool:
    """保存 refresh token；返回是否已安全加密保存。"""
    protected = protect_refresh_token(token)
    if protected is None:
        settings.remove(KEY_REFRESH_TOKEN)
        settings.sync()
        return False
    settings.setValue(KEY_REFRESH_TOKEN, protected)
    settings.sync()
    return True


def load_refresh_token(settings: QSettings) -> str:
    """读取 refresh token；密文失效时删除并返回空串。"""
    protected = settings.value(KEY_REFRESH_TOKEN, "", type=str) or ""
    if not protected:
        return ""
    token = unprotect_refresh_token(protected)
    if token is None:
        settings.remove(KEY_REFRESH_TOKEN)
        settings.sync()
        return ""
    return token


def clear_refresh_token(settings: QSettings) -> None:
    """删除已保存的 refresh token。"""
    settings.remove(KEY_REFRESH_TOKEN)
    settings.sync()


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
    """读取登录信息，返回 ``(是否记住, 用户名, 密码)``。

    密文失效时（Windows 账户密钥轮换、密文被从别的机器拷过来等）
    按“没有保存密码”处理：删掉失效的密文并返回空密码，避免界面显示
    “已记住”却拿不出密码。用户名是明文，仍可继续显示。
    """
    remember = bool(settings.value(KEY_REMEMBER, False, type=bool))
    username = settings.value(KEY_USERNAME, "", type=str) or ""
    token = settings.value(KEY_PASSWORD, "", type=str) or ""
    if not remember:
        return False, "", ""
    password = unprotect_password(token) if token else ""
    if password is None:
        password = ""
    if token and not password:
        settings.remove(KEY_PASSWORD)
        settings.sync()
        remember = False
    return remember, username, password


def clear_login(settings: QSettings) -> None:
    """删除已保存的登录信息与 refresh token。"""
    for key in (
        KEY_REMEMBER,
        KEY_USERNAME,
        KEY_PASSWORD,
        KEY_REFRESH_TOKEN,
    ):
        settings.remove(key)
    settings.sync()


__all__ = [
    "KEY_PASSWORD",
    "KEY_REFRESH_TOKEN",
    "KEY_REMEMBER",
    "KEY_USERNAME",
    "clear_login",
    "clear_refresh_token",
    "load_login",
    "load_refresh_token",
    "protect_password",
    "protect_refresh_token",
    "save_login",
    "save_refresh_token",
    "unprotect_password",
    "unprotect_refresh_token",
]
