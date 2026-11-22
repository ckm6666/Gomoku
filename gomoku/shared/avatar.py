"""头像定义，客户端与服务端共用。

每个账号始终有一个**预置印章头像**（古典纹样字符 + 配色），可在
12 枚印章中自由选择；选择操作不需要文件存储，用户名哈希还能为每个
账号生成稳定的默认头像。

在此之上，登录账号可以上传一张本地图片作为自定义头像：客户端把图片
裁成方形并编码为 JPEG data URL，服务端只校验格式/体积/尺寸后原样保存，
再通过 ``GET /api/v1/users/{id}/avatar`` 提供给所有客户端。上传图片
仅作为预置印章的覆盖层；移除自定义图片后自动回落到预置印章。

本模块不依赖 Qt。
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

#: 上传头像的边长（逻辑像素）。客户端会先裁切/缩放到这个尺寸。
AVATAR_IMAGE_PIXELS = 192
#: 服务端接受的最大图片字节数（解码后）。
AVATAR_IMAGE_MAX_BYTES = 256 * 1024
#: 服务端接受的最大边长，防止绕过客户端上传超大图片。
AVATAR_IMAGE_MAX_DIMENSION = 1024
#: 客户端上传时使用的 MIME 类型；服务端按文件头再次校验。
AVATAR_IMAGE_MEDIA_TYPE = "image/jpeg"


@dataclass(frozen=True)
class AvatarPreset:
    """一个预置头像的展示定义。"""

    key: str
    label: str
    glyph: str
    background: str
    foreground: str = "#F7F0E3"


#: 12 个古典主题头像；顺序稳定，默认头像按用户名哈希从中选择。
AVATARS: tuple[AvatarPreset, ...] = (
    AvatarPreset("mei", "梅", "梅", "#A63D40"),
    AvatarPreset("lan", "兰", "兰", "#3E6B5A"),
    AvatarPreset("zhu", "竹", "竹", "#4F7A4A"),
    AvatarPreset("ju", "菊", "菊", "#B98A2E", "#2B2118"),
    AvatarPreset("song", "松", "松", "#2F5D50"),
    AvatarPreset("he", "鹤", "鹤", "#6B7C93"),
    AvatarPreset("yun", "云", "云", "#8A6D9B"),
    AvatarPreset("shan", "山", "山", "#6E5B4A"),
    AvatarPreset("shui", "水", "水", "#3C6E8F"),
    AvatarPreset("yue", "月", "月", "#8C7A5B", "#FBF6EA"),
    AvatarPreset("xing", "星", "星", "#4B5573"),
    AvatarPreset("feng", "风", "风", "#7A5A4A"),
)

AVATAR_BY_KEY: dict[str, AvatarPreset] = {item.key: item for item in AVATARS}
AVATAR_KEYS = frozenset(AVATAR_BY_KEY)
DEFAULT_AVATAR = "yun"


def is_valid_avatar(value: object) -> bool:
    """判断值是否为已知头像 key。"""
    return isinstance(value, str) and value in AVATAR_KEYS


def default_avatar_for(user_id: str) -> str:
    """按用户 id 稳定地选择一个默认头像。"""
    if not user_id:
        return DEFAULT_AVATAR
    index = zlib.crc32(user_id.encode("utf-8")) % len(AVATARS)
    return AVATARS[index].key


def normalize_avatar(value: object, user_id: str = "") -> str:
    """返回合法头像 key；缺失/非法时回落到用户默认头像。"""
    if is_valid_avatar(value):
        return value  # type: ignore[return-value]
    return default_avatar_for(user_id)


def avatar_preset(value: object, user_id: str = "") -> AvatarPreset:
    """返回头像展示定义。"""
    return AVATAR_BY_KEY[normalize_avatar(value, user_id)]


__all__ = [
    "AvatarPreset",
    "AVATARS",
    "AVATAR_BY_KEY",
    "AVATAR_KEYS",
    "DEFAULT_AVATAR",
    "AVATAR_IMAGE_PIXELS",
    "AVATAR_IMAGE_MAX_BYTES",
    "AVATAR_IMAGE_MAX_DIMENSION",
    "AVATAR_IMAGE_MEDIA_TYPE",
    "is_valid_avatar",
    "default_avatar_for",
    "normalize_avatar",
    "avatar_preset",
]
