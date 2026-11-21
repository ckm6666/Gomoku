"""本局对战会话层：本地双人、人机 AI 与未来联机会话的共同边界。"""

from gomoku.client.sessions.base import GameSession
from gomoku.client.sessions.local import LocalGameSession
from gomoku.client.sessions.ai import AIGameSession
from gomoku.client.sessions.online import OnlineGameSession
from gomoku.client.sessions.puzzle import PuzzleSession

__all__ = [
    "GameSession",
    "LocalGameSession",
    "AIGameSession",
    "OnlineGameSession",
    "PuzzleSession",
]
