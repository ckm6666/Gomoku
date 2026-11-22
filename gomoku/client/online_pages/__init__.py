"""联机面板的页面构建器。"""

from gomoku.client.online_pages.auth_page import build_auth_page
from gomoku.client.online_pages.game_page import build_game_page
from gomoku.client.online_pages.lobby_page import build_lobby_page
from gomoku.client.online_pages.plaza_page import PlazaPage

__all__ = [
    "PlazaPage",
    "build_auth_page",
    "build_game_page",
    "build_lobby_page",
]
