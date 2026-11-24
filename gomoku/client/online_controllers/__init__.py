"""联机面板的领域控制器包。

``OnlinePanel`` 保留页面搭建、控件状态与信号接线，登录、连接、资料、
社交、匹配和对局页行为分别由本包中的控制器承担。控制器只依赖面板
实例，不自行创建 HTTP 客户端。
"""

from gomoku.client.online_controllers.auth_flow import AuthFlowController
from gomoku.client.online_controllers.connection import ConnectionController
from gomoku.client.online_controllers.game import GameController
from gomoku.client.online_controllers.match import MatchController
from gomoku.client.online_controllers.profile import ProfileController
from gomoku.client.online_controllers.social import SocialController

__all__ = [
    "AuthFlowController",
    "ConnectionController",
    "GameController",
    "MatchController",
    "ProfileController",
    "SocialController",
]
