"""自定义头像图片的本地缓存与上传前处理。

自定义头像不随每个接口响应下发（对局快照每手都要广播，内嵌图片会
把流量放大几十倍），而是由客户端按 ``(user_id, avatar_version)`` 主动
拉取并缓存。版本号变化即视为新图片；版本 0 表示没有自定义图片。

所有绘制/编码都使用 Qt，不引入 Pillow 等额外依赖。网络配置由联机面板
在登录页初始化时调用 :func:`configure_avatar_images` 注入；没有配置
服务器时（离线模式/UI 测试）不会发起任何请求。
"""

from __future__ import annotations

import base64
import logging
from typing import Optional
from urllib.parse import quote

from PySide6.QtCore import (
    QBuffer,
    QIODevice,
    QObject,
    QRect,
    QSize,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from gomoku.client.theme_palette import PAPER_LIGHT
from gomoku.shared.avatar import (
    AVATAR_IMAGE_MAX_BYTES,
    AVATAR_IMAGE_MEDIA_TYPE,
    AVATAR_IMAGE_PIXELS,
)

logger = logging.getLogger(__name__)

#: 缓存上限；头像尺寸很小，几百张也只是一两 MB。
_MAX_CACHED_IMAGES = 256
#: 请求超时，避免离线时一直挂着。
_REQUEST_TIMEOUT_MS = 8000


class AvatarImageError(ValueError):
    """本地图片无法读取或编码；消息可直接展示给用户。"""


def prepare_avatar_image(path: str) -> str:
    """读取本地图片，裁成方形并编码为 JPEG data URL。

    超过 2048 像素的大图先让 :class:`QImageReader` 缩小解码，避免手机
    相册原图占用过多内存。
    """
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > 2048:
        scaled = size.scaled(
            QSize(2048, 2048),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        raise AvatarImageError("无法读取该图片，请换一张 JPEG/PNG 图片")

    width = max(1, image.width())
    height = max(1, image.height())
    side = min(width, height)
    left = (width - side) // 2
    top = (height - side) // 2
    square = image.copy(QRect(left, top, side, side))
    square = square.scaled(
        QSize(AVATAR_IMAGE_PIXELS, AVATAR_IMAGE_PIXELS),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )

    # 铺一层纸色底，透明 PNG 保存成 JPEG 时不会变成黑底。
    canvas = QImage(
        AVATAR_IMAGE_PIXELS,
        AVATAR_IMAGE_PIXELS,
        QImage.Format.Format_RGB32,
    )
    canvas.fill(QColor(PAPER_LIGHT))
    painter = QPainter(canvas)
    painter.drawImage(0, 0, square)
    painter.end()

    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise AvatarImageError("图片编码失败，请重试")
    if not canvas.save(buffer, "JPEG", 88):
        raise AvatarImageError("图片编码失败，请换一张图片")
    data = bytes(buffer.data())
    buffer.close()
    if not data:
        raise AvatarImageError("图片编码失败，请换一张图片")
    if len(data) > AVATAR_IMAGE_MAX_BYTES:
        raise AvatarImageError("图片过于复杂，请换一张更简单的图片")
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{AVATAR_IMAGE_MEDIA_TYPE};base64,{encoded}"


class AvatarImageCache(QObject):
    """按（用户 id，版本）缓存自定义头像，并在加载完成后发出通知。"""

    #: (user_id, avatar_version)
    image_loaded = Signal(str, int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._base_url = ""
        self._manager: Optional[QNetworkAccessManager] = None
        self._images: dict[tuple[str, int], QImage] = {}
        self._order: list[tuple[str, int]] = []
        self._inflight: set[tuple[str, int]] = set()
        self._failed: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------
    # 配置/查询
    # ------------------------------------------------------------------
    def configure(self, api_base_url: str) -> None:
        """设置服务器地址；切换服务器时清空旧服务器缓存。"""
        base = (api_base_url or "").rstrip("/")
        if base == self._base_url:
            return
        self._base_url = base
        self._images.clear()
        self._order.clear()
        self._inflight.clear()
        self._failed.clear()

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def image(self, user_id: str, version: int) -> Optional[QImage]:
        """读取已缓存图片；未命中时排队下载并返回 None。"""
        key = (user_id, int(version))
        if not user_id or key[1] <= 0:
            return None
        cached = self._images.get(key)
        if cached is not None:
            self._touch(key)
            return cached
        self._request(key)
        return None

    def invalidate(self, user_id: str) -> None:
        """丢弃某个用户的全部缓存（更换头像后调用）。"""
        for key in [key for key in self._images if key[0] == user_id]:
            self._images.pop(key, None)
        self._order = [key for key in self._order if key[0] != user_id]
        self._failed = {key for key in self._failed if key[0] != user_id}

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def _touch(self, key: tuple[str, int]) -> None:
        try:
            self._order.remove(key)
        except ValueError:
            pass
        self._order.append(key)

    def _request(self, key: tuple[str, int]) -> None:
        if not self._base_url or key in self._inflight or key in self._failed:
            return
        if self._manager is None:
            self._manager = QNetworkAccessManager(self)
        user_id, version = key
        url = QUrl(
            f"{self._base_url}/api/v1/users/"
            f"{quote(user_id, safe='')}/avatar?v={version}"
        )
        if url.scheme() not in ("http", "https") or not url.host():
            return
        request = QNetworkRequest(url)
        request.setTransferTimeout(_REQUEST_TIMEOUT_MS)
        reply = self._manager.get(request)
        self._inflight.add(key)
        reply.finished.connect(
            lambda reply=reply, key=key: self._on_finished(reply, key)
        )

    def _on_finished(
        self, reply: QNetworkReply, key: tuple[str, int]
    ) -> None:
        self._inflight.discard(key)
        try:
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            raw = bytes(reply.readAll())
            ok = (
                reply.error() == QNetworkReply.NetworkError.NoError
                or (isinstance(status, int) and 200 <= status < 300)
            )
            image = QImage()
            if ok and raw:
                image.loadFromData(raw)
            if image.isNull():
                self._failed.add(key)
                logger.warning(
                    "头像图片加载失败 user=%s version=%s status=%s",
                    key[0],
                    key[1],
                    status,
                )
                return
            self._images[key] = image
            self._touch(key)
            while len(self._order) > _MAX_CACHED_IMAGES:
                oldest = self._order.pop(0)
                self._images.pop(oldest, None)
            self.image_loaded.emit(key[0], key[1])
        finally:
            reply.deleteLater()


_cache: Optional[AvatarImageCache] = None


def avatar_image_cache() -> AvatarImageCache:
    """返回进程级头像缓存单例。"""
    global _cache
    if _cache is None:
        _cache = AvatarImageCache()
    return _cache


def configure_avatar_images(api_base_url: str) -> None:
    """配置头像图片服务器；传空串会关闭下载。"""
    avatar_image_cache().configure(api_base_url)


def reset_for_tests() -> None:
    """丢弃进程级头像缓存单例；仅供测试隔离使用。"""
    global _cache
    _cache = None


__all__ = [
    "AvatarImageError",
    "AvatarImageCache",
    "avatar_image_cache",
    "configure_avatar_images",
    "prepare_avatar_image",
    "reset_for_tests",
]
