"""联机对局时限：每手倒计时与 Fischer 时钟（客户端/服务端共用）。

两种模式共用同一个纯 Python 描述，避免两端对“总时长”“加秒”理解不一致：

* ``per_move``：每手固定 N 秒，落子后本手计时重置；
* ``fischer``：双方各有一份总时长（毫秒级余量），每落一手在当前余量上
  增加固定加秒（increment）；余量耗尽即超时判负。

服务端始终是计时的权威方。客户端只根据权威快照里的余量与服务器时间在
本地平滑显示，不自行判定谁超时。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from gomoku.shared.errors import ProtocolError

MODE_PER_MOVE = "per_move"
MODE_FISCHER = "fischer"

# 输入范围。客户端下拉框只提供预设，但协议层必须独立校验。
PER_MOVE_MIN_SECONDS = 5
PER_MOVE_MAX_SECONDS = 600
FISCHER_MIN_MAIN_SECONDS = 60
FISCHER_MAX_MAIN_SECONDS = 3600
FISCHER_MIN_INCREMENT_SECONDS = 0
FISCHER_MAX_INCREMENT_SECONDS = 60


def _require_int(
    value: Any, field: str, minimum: int, maximum: int
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProtocolError(f"{field} 必须在 {minimum}～{maximum} 之间")
    return value


@dataclass(frozen=True)
class TimeControl:
    """一局对局的时限设置。"""

    mode: str
    turn_seconds: int = 0
    main_seconds: int = 0
    increment_seconds: int = 0

    def __post_init__(self) -> None:
        if self.mode == MODE_PER_MOVE:
            _require_int(
                self.turn_seconds,
                "每手时间",
                PER_MOVE_MIN_SECONDS,
                PER_MOVE_MAX_SECONDS,
            )
        elif self.mode == MODE_FISCHER:
            _require_int(
                self.main_seconds,
                "总时长",
                FISCHER_MIN_MAIN_SECONDS,
                FISCHER_MAX_MAIN_SECONDS,
            )
            _require_int(
                self.increment_seconds,
                "加秒",
                FISCHER_MIN_INCREMENT_SECONDS,
                FISCHER_MAX_INCREMENT_SECONDS,
            )
        else:
            raise ProtocolError("时间模式必须是 per_move 或 fischer")

    @classmethod
    def per_move(cls, seconds: int) -> "TimeControl":
        return cls(mode=MODE_PER_MOVE, turn_seconds=int(seconds))

    @classmethod
    def fischer(
        cls, main_seconds: int, increment_seconds: int = 0
    ) -> "TimeControl":
        return cls(
            mode=MODE_FISCHER,
            main_seconds=int(main_seconds),
            increment_seconds=int(increment_seconds),
        )

    @property
    def is_fischer(self) -> bool:
        return self.mode == MODE_FISCHER

    def initial_remaining_ms(self) -> int:
        """每方开局时的初始余量。"""
        if self.is_fischer:
            return self.main_seconds * 1000
        return self.turn_seconds * 1000

    def key(self) -> tuple[int, int, int, int]:
        """匹配队列的时限键：只有完全相同的设置才会配对。"""
        return (
            1 if self.is_fischer else 0,
            self.turn_seconds,
            self.main_seconds,
            self.increment_seconds,
        )

    def label(self) -> str:
        if self.is_fischer:
            main_text = (
                f"{self.main_seconds // 60} 分"
                if self.main_seconds % 60 == 0
                else f"{self.main_seconds} 秒"
            )
            if self.increment_seconds:
                return (
                    f"Fischer {main_text} + "
                    f"{self.increment_seconds} 秒/手"
                )
            return f"Fischer {main_text}总时长"
        return f"每手 {self.turn_seconds} 秒"

    def to_payload(self) -> dict[str, Any]:
        if self.is_fischer:
            return {
                "mode": MODE_FISCHER,
                "main_seconds": self.main_seconds,
                "increment_seconds": self.increment_seconds,
            }
        return {
            "mode": MODE_PER_MOVE,
            "turn_seconds": self.turn_seconds,
        }

    @classmethod
    def from_payload(cls, value: Any) -> "TimeControl":
        """解析协议里的 time_control 对象；畸形输入抛 ``ProtocolError``。"""
        if not isinstance(value, Mapping):
            raise ProtocolError("time_control 必须是对象")
        mode = value.get("mode", MODE_PER_MOVE)
        if mode == MODE_PER_MOVE:
            return cls.per_move(
                _require_int(
                    value.get("turn_seconds"),
                    "每手时间",
                    PER_MOVE_MIN_SECONDS,
                    PER_MOVE_MAX_SECONDS,
                )
            )
        if mode == MODE_FISCHER:
            return cls.fischer(
                _require_int(
                    value.get("main_seconds"),
                    "总时长",
                    FISCHER_MIN_MAIN_SECONDS,
                    FISCHER_MAX_MAIN_SECONDS,
                ),
                _require_int(
                    value.get("increment_seconds", 0),
                    "加秒",
                    FISCHER_MIN_INCREMENT_SECONDS,
                    FISCHER_MAX_INCREMENT_SECONDS,
                ),
            )
        raise ProtocolError("时间模式必须是 per_move 或 fischer")


#: 界面提供的预设。排位匹配只与相同预设的玩家配对。
TIME_CONTROL_PRESETS: tuple[TimeControl, ...] = (
    TimeControl.per_move(15),
    TimeControl.per_move(30),
    TimeControl.per_move(60),
    TimeControl.per_move(90),
    TimeControl.fischer(180, 2),
    TimeControl.fischer(300, 3),
    TimeControl.fischer(600, 5),
)

DEFAULT_TIME_CONTROL: TimeControl = TimeControl.per_move(30)


def time_control_presets() -> Sequence[TimeControl]:
    return TIME_CONTROL_PRESETS


def time_control_from_snapshot(
    value: Any, fallback_turn_seconds: Optional[int]
) -> Optional[TimeControl]:
    """兼容旧服务端：没有 time_control 时用每手秒数推导。"""
    if value is None:
        if fallback_turn_seconds is None:
            return None
        try:
            return TimeControl.per_move(fallback_turn_seconds)
        except ProtocolError:
            return None
    return TimeControl.from_payload(value)


__all__ = [
    "DEFAULT_TIME_CONTROL",
    "FISCHER_MAX_INCREMENT_SECONDS",
    "FISCHER_MAX_MAIN_SECONDS",
    "FISCHER_MIN_INCREMENT_SECONDS",
    "FISCHER_MIN_MAIN_SECONDS",
    "MODE_FISCHER",
    "MODE_PER_MOVE",
    "PER_MOVE_MAX_SECONDS",
    "PER_MOVE_MIN_SECONDS",
    "TIME_CONTROL_PRESETS",
    "TimeControl",
    "time_control_from_snapshot",
    "time_control_presets",
]
