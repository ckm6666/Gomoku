# Gomoku Online Server（自建房 + 排位匹配版）

FastAPI + WebSocket + SQLAlchemy 的联机服务端，提供：

- 用户名 + 密码账号注册、登录、refresh token 轮换、登出；
- 公开排位快速匹配队列（按积分接近配对、随机黑白）；
- 预置头像、公开资料与用户搜索；
- 好友申请/同意/删除、好友在线状态推送与好友私聊（消息持久化、离线补收）；
- 自建房邀请：房主创建房间后直接邀请在线好友，对方接受即开局，无需房间码；
- 服务器权威落子、五连判定、每手倒计时；
- 断线保留房间与重连恢复（每手 30 秒默认）；
- 认输、自建房双方确认再战、离开房间；
- Elo 积分、段位、排行榜、个人资料与最近排位战绩；
- 最近对局列表与单局完整棋谱（仅对局参与者可读）；
- SQLite 开发默认 / PostgreSQL 生产 URL 兼容。

当前版本对应客户端 **1.8.0**。已提供参与者棋谱读取；尚未实现赛季、Glicko-2、
邮箱验证、第三方登录、举报/处罚或 Redis 多实例路由。

## 目录

```text
server/
├── app/
│   ├── main.py             # FastAPI app factory、REST/WS 路由、生命周期
│   ├── config.py           # 环境变量配置
│   ├── db.py               # 异步引擎、建表、加列迁移、启动作废未完成对局
│   ├── models.py           # users/friendships/direct_messages/games/moves/rating_events
│   ├── ratings.py          # Elo 与段位纯函数
│   ├── security.py         # Argon2id、JWT、refresh token
│   ├── schemas.py          # 认证/资料/排行榜/好友/私聊 DTO
│   ├── auth_service.py     # 令牌生成辅助
│   ├── api/auth.py         # 注册/登录/刷新/登出/me
│   ├── api/ranked.py       # /profile/me 与 /leaderboard
│   ├── api/users.py        # /users/{id}、/users/search、头像更新
│   ├── api/friends.py      # 好友申请/列表/同意/拒绝/删除
│   ├── api/messages.py     # 好友私聊历史与发送
│   ├── api/games.py        # /games/recent 与 /games/{id}
│   └── ws/
│       ├── endpoint.py     # WebSocket 首帧认证与命令分发
│       └── rooms.py        # 自建房邀请、匹配队列、排位结算、时钟、重连、再战
├── tests/                  # REST + 双 WebSocket + Elo 测试
├── requirements.txt
├── requirements-dev.txt
├── requirements-postgres.txt
└── manage.py               # 管理员重置密码
```

## 快速启动（Windows，本地测试）

从项目根目录执行：

```powershell
cd C:\Users\a1325\Desktop\Gomoku

# 项目已包含 server\.venv；若从零安装：
py -3 -m venv server\.venv
server\.venv\Scripts\python.exe -m pip install -r server\requirements-dev.txt

# 固定一个足够长的开发 secret（生产必须换成真正随机秘密）
$env:GOMOKU_SERVER_ENV = "development"
$env:GOMOKU_JWT_SECRET = "replace-with-a-long-random-development-secret-32chars+"

# 单进程启动；房间/匹配队列在内存中，第一版不要开多个 worker
server\.venv\Scripts\python.exe -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/v1/health
```

默认数据库：

```text
server/data/gomoku.db
```

客户端在开发环境默认连接：

- REST：`http://127.0.0.1:8000`
- WebSocket：`ws://127.0.0.1:8000/api/v1/ws`

所以本机测试无需额外配置即可打开客户端“联机对战”。

## 账号与注册

- 客户端登录页同时支持“登录”和“注册”。
- 注册只需填写用户名和密码，不需要任何额外凭据；用户名唯一，密码使用 Argon2id 哈希存储。
- 关闭/重启服务后，如果使用随机开发 secret，旧 access/refresh token 会失效；固定 `GOMOKU_JWT_SECRET` 可避免。
- 目前没有邮箱/短信自助找回；管理员可重置密码并撤销 refresh 会话：

```powershell
server\.venv\Scripts\python.exe -m server.manage reset-password --username 用户名
# 不传 --new-password 时会生成随机密码并打印
```

本地联机测试时，直接在客户端注册两个账号（用户名 + 密码）即可用于排位对局；
好友功能可在好友窗口按用户名搜索并互相添加。

## 排位积分与匹配

- 客户端大厅发送 `quick_match` 加入内存队列；队列中已有对手时，服务器选择与入队者
  积分最接近的玩家，随机分配黑白并立即开局。
- 排位房在数据库中标记 `games.rated=true`，只能通过匹配创建，不能通过好友邀请加入，
  避免定向刷分；排位终局不支持 `rematch`，客户端需重新匹配。
- 每手默认 30 秒；认输/超时/五连/平局都由服务器裁决。
- 终局由服务器执行一次 Elo 结算：
  - 起始 1200 分，最低 100；
  - 前 10 局 K=40，此后 <1600 为 32、1600～1999 为 24、≥2000 为 16；
  - 平局双方各得 0.5 分；
  - 更新 `users.rating/peak_rating/wins/losses/draws/rated_games`，并为双方写入
    `rating_events`（含对手、赛前/赛后积分与增减）。
- 段位（由积分计算，服务端唯一定义）：

| 段位 | 积分 |
| --- | --- |
| 青铜 | < 1100 |
| 白银 | 1100～1199 |
| 黄金 | 1200～1349（新账号） |
| 铂金 | 1350～1499 |
| 钻石 | 1500～1649 |
| 大师 | 1650～1799 |
| 王者 | ≥ 1800 |

- 自建房 `rated=false`，无论胜负都不改积分。
- 服务启动时会把上次残留的 waiting/playing 对局标记为 `abandoned`，不结算积分。

### 排位相关接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /api/v1/leaderboard?limit=50&offset=0` | 登录后读取排行榜；只包含完成过排位赛的账号，积分降序、场次次序、用户名兜底 |
| `GET /api/v1/profile/me` | 当前账号积分资料 + 最近 10 场排位记录 |
| `GET /api/v1/games/recent?limit=20&offset=0` | 当前账号参与过的已结束对局（排位 + 自建房），按结束时间倒序 |
| `GET /api/v1/games/{game_id}` | 单局完整棋谱与落子序列；仅对局参与者可读，非参与者返回 404 |
| `GET /api/v1/auth/me` | 当前账号资料（含 avatar/rating/peak/rank_name/win_rate 等计算字段） |
| `GET /api/v1/users/{user_id}` | 公开资料、在线状态与好友关系状态 |
| `GET /api/v1/users/search?q=` | 按用户名搜索活跃账号（最多 20 条） |
| `PATCH /api/v1/users/me/avatar` | 更新预置头像 |
| `GET /api/v1/friends` | 好友列表、收到的申请、发出的申请与未读数 |
| `POST /api/v1/friends/requests` | 发送好友申请（`{"user_id": ...}`） |
| `POST /api/v1/friends/requests/{id}/accept` | 同意好友申请 |
| `POST /api/v1/friends/requests/{id}/decline` | 拒绝好友申请 |
| `DELETE /api/v1/friends/{user_id}` | 删除好友 |
| `GET /api/v1/messages/{user_id}?limit=50` | 与好友的最近私聊；只把本次返回的消息标记已读 |
| `POST /api/v1/messages/{user_id}` | 发送私聊（`{"content": ...}`），在线时实时推送 |
| `WS quick_match` | 加入排位匹配队列 |
| `WS cancel_match` | 离开排位队列 |
| `WS queue_status` | 服务端返回 queued/idle、队列人数、当前积分/段位 |
| `WS match_found` | 匹配成功，返回 game_id、执子颜色、对手昵称和双方赛前积分 |
| `WS game_state` | 权威快照新增 `rated`、`your_rating_before/after`、`opponent_rating_before/after`、`rating_delta`、双方 user_id 与头像 |
| `WS invite_to_room` | 房主邀请在线好友加入自建房 |
| `WS respond_room_invite` | 被邀请者接受/拒绝邀请 |
| `WS room_invitation` | 服务端推送给被邀请者的对局邀请 |
| `WS room_invite_result` | 邀请发送/接受/拒绝/取消结果 |
| `WS friend_event` | 好友申请、同意、删除与在线状态推送 |
| `WS direct_message` | 收到好友私聊时实时推送 |

## 联机规则与协议

- 创建自建房者执黑先手，被邀请并接受的玩家执白。
- 每手默认 30 秒，服务器倒计时；超时由服务器判负。
- 客户端只发送落子意图；服务器校验座位/回合/坐标/时钟后，先把落子和局面写入数据库，再广播权威快照。
- 断线不立即结束服务端房间：服务器保留房间与时钟，客户端可在回合时间内重连；重连后服务器下发完整快照。
- 进行中的对局不支持单方面悔棋；支持认输。
- 自建房双方都申请再战后，服务器创建新对局（不计分）并交换黑白；排位房拒绝再战。
- 服务重启时未终局对局统一标记为 `abandoned`，客户端回到大厅；不伪装成崩溃恢复。
- 自建房使用过期时间 60 秒的定向邀请；邀请要求双方为好友且被邀请者在线。内部仍保留随机房间码用于数据库记录，但客户端不再展示或要求输入。
- 匹配队列在断线时移除该玩家；重连并重新入队即可。

## 数据库兼容与迁移

- 开发默认 SQLite（`server/data/gomoku.db`），生产可切换 PostgreSQL。
- 启动时 `create_all` 创建缺失表；`db._apply_additive_schema` 会为 Stage 1 旧数据库
  自动补加 `users.rating` 等列、`games.rating_applied` 和 `ix_users_rating` 索引。
- 新表 `rating_events` 有 `(game_id, user_id)` 唯一约束，防止同一局重复入账。
- 正式公开化前仍建议引入 Alembic 版本化迁移、备份恢复演练和数据库连接池监控。

### PostgreSQL

本机没有安装 PostgreSQL，因此开发默认 SQLite。生产建议安装 PostgreSQL 并执行：

```powershell
server\.venv\Scripts\python.exe -m pip install -r server\requirements-postgres.txt
$env:GOMOKU_DATABASE_URL = "postgresql+asyncpg://user:password@host:5432/gomoku"
```

## 客户端远程连接

如果服务端不在本机，启动客户端前设置环境变量：

```powershell
$env:GOMOKU_API_BASE_URL = "https://your-domain.example"
$env:GOMOKU_WS_BASE_URL  = "wss://your-domain.example"
Start-Process C:\Users\a1325\Desktop\Gomoku\dist\Gomoku.exe
```

生产必须使用 HTTPS/WSS。WebSocket 地址可写服务根地址（会自动追加
`/api/v1/ws`），也可以直接写完整 `/api/v1/ws`。

## 服务端配置

所有配置通过环境变量提供：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GOMOKU_SERVER_ENV` | `development` | `production` 时强制要求 secret |
| `GOMOKU_DATABASE_URL` | SQLite `server/data/gomoku.db` | SQLAlchemy async URL |
| `GOMOKU_JWT_SECRET` | 开发环境随机 | 至少 32 字符；生产必须固定 |
| `GOMOKU_ACCESS_TOKEN_MINUTES` | `15` | access JWT 有效期 |
| `GOMOKU_REFRESH_TOKEN_DAYS` | `30` | refresh token 有效期 |
| `GOMOKU_TURN_SECONDS` | `30` | 每手倒计时，5～600 |
| `GOMOKU_WS_AUTH_TIMEOUT` | `10` | WebSocket 首帧认证超时 |
| `GOMOKU_WS_IDLE_TIMEOUT` | `75` | 客户端心跳间隔需短于此 |

起始积分、K 值、段位阈值目前定义在 `server/app/ratings.py`，修改后需同步更新文档；
不通过环境变量开放，避免部署时不同实例积分规则漂移。

## 生产部署要点

1. 只运行 **一个 Uvicorn worker**；当前房间、匹配队列和身份互斥状态在进程内存中。
   多实例需要 Redis/房间与队列路由后再开启。
2. 使用 Caddy/Nginx 终止 TLS，对外只开放 HTTPS/WSS 443；不要把开发 HTTP/WS
   服务直接暴露到公网。
3. 固定随机 `GOMOKU_JWT_SECRET`，限制配置文件权限，不把 secret 写入客户端或仓库。
4. 配置每日 PostgreSQL 备份、健康检查和日志收集；设置防火墙，仅开放必要端口。
5. 客户端 token 目前只存内存，应用重启需要重新登录；后续可用 Windows DPAPI 安全保存 refresh token。
6. 排位已通过“仅匹配计分、自建房不计分、排位不可直接再战”降低刷分面，但仍无法阻止
   排队串通、故意送分和外部 AI 辅助；积分阶段需另行设计反作弊、举报与回放审计。
7. 若未来在中国大陆公开运营，需单独评估 ICP/公安备案、实名/未成年人保护、隐私与游戏出版等要求；
   当前定位仅为内测。

## 测试

```powershell
cd C:\Users\a1325\Desktop\Gomoku
server\.venv\Scripts\python.exe -m unittest discover -s server/tests -v
```

当前 **47 项**测试覆盖：

- 用户名 + 密码注册、登录、refresh 轮换/重放、登出、认证响应积分字段；
- 双 WebSocket 建房/邀请/加入/落子/拒绝/认输/再战、服务器五连判定、断线重连；
- 快速匹配、取消匹配、排位房、随机黑白、终局 Elo 结算、自建房不计分；
- 排行榜、个人资料与最近战绩；
- 最近对局/单局棋谱的参与者权限、分页、认输终局；
- 头像更新与用户搜索、好友申请/同意/删除、在线状态推送、私聊历史与实时推送；
- 自建房邀请接受/拒绝、非好友/离线好友拒绝，以及快照中的双方 user_id/头像；
- Elo 期望分/K 值/段位边界/最低分保护。

另做过本机真实 Uvicorn + 两个 WebSocket 客户端完整排位对局验证：两名 1200 分玩家
对局后变为 1220/1180，排行榜与最近战绩同步更新。
