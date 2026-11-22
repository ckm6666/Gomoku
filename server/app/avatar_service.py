"""自定义头像图片的校验与解码。

客户端把本地图片裁成 192×192 的 JPEG data URL 后上传；服务端不引入
图像处理依赖，只做三件事：

1. 严格解析 ``data:image/{jpeg,png};base64,...``；
2. 解码后检查体积与文件头（JPEG/PNG）；
3. 读取像素尺寸，拒绝异常大的图片。

这样即使绕过客户端直接调用接口，也不会把任意二进制或超大图片写进
数据库，更不会在服务端解码不可信图片。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from gomoku.shared.avatar import (
    AVATAR_IMAGE_MAX_BYTES,
    AVATAR_IMAGE_MAX_DIMENSION,
)

_DATA_URL_RE = re.compile(
    r"^data:(image/(?:jpeg|png));base64,([A-Za-z0-9+/=\s]+)$"
)
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class AvatarImageError(ValueError):
    """头像图片不合法；消息可以直接展示给用户。"""


@dataclass(frozen=True)
class DecodedAvatarImage:
    data: bytes
    media_type: str


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24:
        raise AvatarImageError("PNG 图片数据不完整")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _jpeg_size(data: bytes) -> tuple[int, int]:
    length = len(data)
    offset = 2  # 跳过 SOI (FF D8)
    while offset + 4 <= length:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        # 填充字节与无长度信息的标记。
        if marker == 0xFF:
            offset += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        segment_length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if segment_length < 2 or offset + 2 + segment_length > length:
            break
        if marker in _JPEG_SOF_MARKERS:
            if offset + 9 > length:
                break
            height = int.from_bytes(data[offset + 5 : offset + 7], "big")
            width = int.from_bytes(data[offset + 7 : offset + 9], "big")
            return width, height
        offset += 2 + segment_length
    raise AvatarImageError("JPEG 图片缺少有效的尺寸信息")


def _image_size(data: bytes, media_type: str) -> tuple[int, int]:
    if media_type == "image/png":
        if not data.startswith(_PNG_MAGIC):
            raise AvatarImageError("文件内容不是有效的 PNG 图片")
        return _png_size(data)
    if not data.startswith(b"\xff\xd8\xff"):
        raise AvatarImageError("文件内容不是有效的 JPEG 图片")
    return _jpeg_size(data)


def validate_avatar_image(value: object) -> str:
    """校验并返回规范化的 data URL；失败抛出 :class:`AvatarImageError`。"""
    if not isinstance(value, str) or not value:
        raise AvatarImageError("请先选择一张图片")
    match = _DATA_URL_RE.match(value.strip())
    if match is None:
        raise AvatarImageError("只支持 JPEG/PNG 图片")
    media_type, payload = match.group(1), match.group(2)
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AvatarImageError("图片数据损坏，请重新选择") from exc
    if not data:
        raise AvatarImageError("请先选择一张图片")
    if len(data) > AVATAR_IMAGE_MAX_BYTES:
        raise AvatarImageError(
            f"图片不能超过 {AVATAR_IMAGE_MAX_BYTES // 1024} KB"
        )
    width, height = _image_size(data, media_type)
    if width <= 0 or height <= 0:
        raise AvatarImageError("图片尺寸无效")
    if max(width, height) > AVATAR_IMAGE_MAX_DIMENSION:
        raise AvatarImageError(
            f"图片边长不能超过 {AVATAR_IMAGE_MAX_DIMENSION} 像素"
        )
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def decode_avatar_image(value: object) -> DecodedAvatarImage:
    """把数据库中保存的 data URL 解码成可直接返回的字节。"""
    match = _DATA_URL_RE.match(value.strip()) if isinstance(value, str) else None
    if match is None:
        raise AvatarImageError("头像数据无效")
    media_type, payload = match.group(1), match.group(2)
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AvatarImageError("头像数据无效") from exc
    return DecodedAvatarImage(data=data, media_type=media_type)


__all__ = [
    "AvatarImageError",
    "DecodedAvatarImage",
    "validate_avatar_image",
    "decode_avatar_image",
]
