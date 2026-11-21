"""五子棋包命名空间。

``gomoku.shared`` 只包含无 Qt 的棋盘规则/协议，可被客户端与服务端复用；
``gomoku.client`` 包含桌面 UI、本地/AI 会话与可注入的传输抽象。
"""

from __future__ import annotations

__version__ = "1.3.0"
