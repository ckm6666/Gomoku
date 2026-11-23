# Gomoku Online Server（自建房 + 排位匹配版）

FastAPI + WebSocket + SQLAlchemy 的联机服务端，提供：

- 用户名 + 密码账号注册、登录、refresh token 轮换、登出；
- 公开排位快速匹配队列（按积分接近配对、随机黑白，排位固定每手 30 秒）；
- 自建房每手倒计时与 **Fischer 总时长/加秒**，服务器权威扣除与加秒；
- **Glicko-2** 积分（积分/RD/波动率）与段位、排行榜、个人资料与最近排位战绩；
- **排位赛季**：当前赛季信息、到期归档 `season_ratings`、积分软重置、历史赛季排行榜；
- 预置头像与自定义图片头像（JPEG/PNG 校验、体积/尺寸限制、按版本长缓存）、公开资料与用户搜索；
- 排位成就：胜场/连胜/段位成就由账号统计推导，排位结算时更新连胜并在终局快照下发新解锁勋章；
- 好友申请/同意/删除、好友在线状态推送与好友私聊（消息持久化、离线补收）；
- 自建房邀请：房主创建房间后直接邀请在线好友，对方接受即开局，无需房间码；
- **举报与回放审计**：参与者举报对手，管理员白名单复核；审计检查落子序列/座位/终局一致性与时间异常；
- 断线保留房间与重连恢复；
- 认输、自建房双方确认再战、离开房间；
- 最近对局列表与单局完整棋谱（仅对局参与者可读）以及参与者审计接口；
- SQLite 开发默认 / PostgreSQL 生产 URL 兼容。

当前版本对应客户端 **1.11.0**（服务端 API 0.5.0）。尚未实现邮箱验证、第三方登录、
账号自动处罚/申诉或 Redis 多实例路由。

## 目录

```text
server/
├── app/
│   ├── main.py             # FastAPI app factory、REST/WS 路由、生命周期
│   ├── config.py           # 环境变量配置
│   ├── db.py               # 异步引擎、旧库兼容建表、启动作废未完成对局
│   ├── migrations.py       # 启动时执行 Alembic upgrade / 旧库登记基线
│   ├── metrics.py          # Prometheus 指标与房间状态 collector
│   ├── observability.py    # 结构化日志、请求 ID、用户上下文
│   ├── models.py           # users/games/moves/rating_events/seasons/game_reports/社交
│   ├── glicko2.py          # Glicko-2 积分、RD、波动率纯函数
│   ├── ratings.py          # 积分结算封装、段位与赛季软重置
│   ├── seasons.py          # 当前赛季推进、归档与软重置
│   ├── audit.py            # 回放审计（序列/座位/结果/时间异常）
│   ├── achievement_service.py  # 由账号统计推导成就
│   ├── avatar_service.py   # 自定义头像 data URL 校验与解码
│   ├── security.py         # Argon2id、JWT、refresh token
│   ├── schemas.py          # 认证/资料/排行榜/好友/私聊 DTO
│   ├── auth_service.py     # 令牌生成辅助
│   ├── api/auth.py         # 注册/登录/刷新/登出/me
│   ├── api/ranked.py       # /profile/me（含赛季排名）与 /leaderboard
│   ├── api/seasons.py      # /seasons/current 与历史赛季归档排行榜
│   ├── api/reports.py      # /reports 与 /admin/reports 复核
│   ├── api/users.py        # /users/{id}、/users/search、头像更新/上传/读取
│   ├── api/friends.py      # 好友申请/列表/同意/拒绝/删除
│   ├── api/messages.py     # 好友私聊历史与发送
│   ├── api/games.py        # /games/recent、/games/live、/games/{id} 与 /audit
│   ├── ws/
│   │   ├── endpoint.py     # WebSocket 首帧认证、能力协商与命令分发
│   │   ├── entities.py     # 连接/房间/邀请/队列纯数据实体
│   │   ├── limits.py       # 命令令牌桶与单 IP 连接配额
│   │   └── rooms.py        # 自建房邀请、匹配队列、排位结算、时钟、重连、再战
├── migrations/             # Alembic 版本化迁移（0001 baseline、0002 赛季/Glicko/举报）
├── alembic.ini             # 迁移脚本路径与数据库 URL 占位
├── Dockerfile              # 服务端生产镜像（非 root、健康检查）
├── tests/                  # REST + 双 WebSocket + Glicko-2/赛季/举报/审计/迁移/可观测性测试
├── requirements.txt              # 运行依赖（版本范围）
├── requirements.lock             # 运行依赖锁定（含哈希）
├── requirements-dev.txt          # 开发/测试依赖
├── requirements-dev.lock         # 开发/测试依赖锁定（含哈希）
├── requirements-postgres.txt     # PostgreSQL 驱动
├── requirements-postgres.lock    # PostgreSQL 驱动锁定（含哈希）
└── manage.py               # 管理员重置密码
```

## 快速启动（Windows，本地测试）

从项目根目录执行：

```powershell
cd C:\Users\a1325\Desktop\Gomoku

# 项目已包含 server\.venv；若从零安装（可复现，推荐）：
py -3 -m venv server\.venv
server\.venv\Scripts\python.exe -m pip install --require-hashes -r server\requirements-dev.lock

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
- 排位赛固定每手 30 秒（客户端提交的时限设置会被忽略）；自建房可选每手倒计时或
  Fischer 总时长/加秒。认输/超时/五连/平局都由服务器裁决。
- 终局由服务器执行一次 **Glicko-2** 结算：
  - 起始 1200 分，最低 100；初始 RD 100、波动率 0.06；
  - 每局根据双方积分/RD/波动率与期望得分更新；平局按 0.5 分；
  - 更新 `users.rating/peak_rating/rating_rd/rating_volatility/...rated_games`，并为双方写入
    `rating_events`（含对手、赛前/赛后积分与增减、RD/波动率变化、赛季归属）。
- 赛季默认 90 天（`GOMOKU_SEASON_LENGTH_DAYS`）：到期把参赛账号最终积分归档到
  `season_ratings`，积分软重置为 `1200 + (rating-1200)*0.5`，RD 抬高到至少 120，
  当前连胜清零；生涯胜场/勋章不重置。`GET /api/v1/seasons/current` 返回当前赛季与个人排名。
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
| `GET /api/v1/profile/me` | 当前账号积分资料、当前赛季/赛季排名 + 最近 10 场排位记录 |
| `GET /api/v1/seasons/current` | 当前赛季名称/起止/剩余天数、个人赛季排名与 RD |
| `GET /api/v1/seasons/{number}/leaderboard` | 历史赛季归档排行榜（`season_ratings`） |
| `GET /api/v1/games/recent?limit=20&offset=0` | 当前账号参与过的已结束对局（排位 + 自建房），按结束时间倒序 |
| `GET /api/v1/games/{game_id}` | 单局完整棋谱与落子序列；仅对局参与者可读，非参与者返回 404 |
| `GET /api/v1/games/{game_id}/audit` | 回放审计：落子序列/座位/终局一致性/时间异常；参与者或管理员可读 |
| `GET /api/v1/games/live` | 当前正在进行中的对局列表（观战入口）：双方昵称、模式、手数；不含已结束对局 |
| `POST /api/v1/reports` | 举报已结束对局中的对手（`game_id`、`category`、`description`），每局每人一次 |
| `GET /api/v1/reports/mine` | 自己提交的举报 |
| `GET /api/v1/admin/reports?status=pending` | 管理员待处理/已处理举报列表（含审计异常数） |
| `POST /api/v1/admin/reports/{id}/review` | 管理员复核：`dismiss` / `warn` / `uphold` + 备注 |
| `GET /api/v1/auth/me` | 当前账号资料（含 avatar/rating/peak/rank_name/win_rate 等计算字段） |
| `GET /api/v1/users/{user_id}` | 公开资料、在线状态与好友关系状态 |
| `GET /api/v1/users/search?q=` | 按用户名搜索活跃账号（最多 20 条） |
| `PATCH /api/v1/users/me/avatar` | 更新预置头像；同时移除自定义图片并把 `avatar_version` 归零 |
| `PUT /api/v1/users/me/avatar-image` | 上传自定义头像（`{"image": "data:image/jpeg;base64,..."}`，仅 JPEG/PNG，解码后 ≤256KB、边长 ≤1024） |
| `DELETE /api/v1/users/me/avatar-image` | 移除自定义头像，回落到预置印章 |
| `GET /api/v1/users/{user_id}/avatar` | 公开读取自定义头像图片；`?v=<avatar_version>` 参与缓存键，可长缓存 |
| `GET /api/v1/friends` | 好友列表、收到的申请、发出的申请与未读数 |
| `POST /api/v1/friends/requests` | 发送好友申请（`{"user_id": ...}`） |
| `POST /api/v1/friends/requests/{id}/accept` | 同意好友申请 |
| `POST /api/v1/friends/requests/{id}/decline` | 拒绝好友申请 |
| `DELETE /api/v1/friends/{user_id}` | 删除好友 |
| `GET /api/v1/messages/{user_id}?limit=50` | 与好友的最近私聊；只把本次返回的消息标记已读 |
| `POST /api/v1/messages/{user_id}` | 发送私聊（`{"content": ...}`），在线时实时推送 |
| `WS auth` | 首帧认证；可携带 `capabilities` 声明客户端能力，响应返回服务端 `capabilities` |
| `WS create_room` | 自建房；`allow_spectators=false` 可关闭观战；`time_control` 可选 `per_move` 或 `fischer`，兼容旧字段 `turn_seconds` |
| `WS quick_match` | 加入排位匹配队列；可选 `time_control`（或旧 `turn_seconds`），只与完全相同的设置配对 |
| `WS cancel_match` | 离开排位队列 |
| `WS queue_status` | 服务端返回 queued/idle、队列人数、当前积分/段位 |
| `WS match_found` | 匹配成功，返回 game_id、执子颜色、对手昵称和双方赛前积分 |
| `WS game_state` | 权威快照包含 `rated`、`your_rating_before/after`、`opponent_rating_before/after`、`rating_delta`、双方 user_id/头像版本/已解锁成就、`your_new_achievements`；`state_version` 标识回滚、`result_reason` 标识终局原因、`undo_requester_id` 与 `undo_remaining/undo_limit/undo_cooldown_*` 标识悔棋；`time_control`、`turn_deadline_ms`、`server_now_ms`、`black/white_remaining_ms` 标识时限与 Fischer 双方余量 |
| `WS undo_request` / `undo_response` | 当前回合玩家发起悔棋请求，对手同意或拒绝 |
| `WS undo_status` | 悔棋状态：requested/accepted/declined/expired/cancelled |
| `WS invite_to_room` | 房主邀请在线好友加入自建房 |
| `WS respond_room_invite` | 被邀请者接受/拒绝邀请 |
| `WS room_invitation` | 服务端推送给被邀请者的对局邀请 |
| `WS room_invite_result` | 邀请发送/接受/拒绝/取消结果 |
| `WS friend_event` | 好友申请、同意、删除与在线状态推送 |
| `WS direct_message` | 收到好友私聊时实时推送 |

## 联机规则与协议

- 创建自建房者执黑先手，被邀请并接受的玩家执白。
- 排位固定每手 30 秒；自建房创建时可选 15/30/60/90 秒，或 Fischer
  总时长/加秒（例如 5 分 + 3 秒）。Fischer 模式双方各有余量，落子后扣除本手耗时并加回
  固定秒数；余量耗尽由服务器判负。快照同时下发双方余量，客户端只做本地平滑显示。
- 客户端只发送落子意图；服务器校验座位/回合/坐标/时钟后，先把落子和局面写入数据库，再广播权威快照。
- 认证帧可携带 `capabilities`，服务端在认证响应中返回自身能力；未知能力会被忽略，
  但权限仍由服务端独立校验，不能只依赖能力声明。
- 断线不立即结束服务端房间：服务器保留房间与时钟，客户端可在宽限期内重连；重连后服务器下发完整快照。
- 掉线超过 `GOMOKU_DISCONNECT_FORFEIT_SECONDS`（默认 20 秒）仍未重连，判对手获胜，
  终局原因记为 `escape`；排位赛照常结算。
- 进行中的对局不支持单方面悔棋：当前回合玩家可发起悔棋请求，对手同意后服务器删除
  请求者最后一手及其后的落子（数据库同步删除），拒绝或 30 秒未回应则保持原局面。
  每方每局有悔棋次数上限，两次请求之间有冷却。
- 支持认输；客户端会二次确认，服务端仍以认输消息为准。
- 自建房可以在创建时关闭观战；关闭后不出现在广场，`watch_game` 也会被拒绝。
  每局观战人数有上限，避免慢连接拖慢落子广播。
- 单 IP 的已认证 WebSocket 连接数有上限；非心跳命令使用令牌桶限流。
  生产位于可信反向代理后时，开启 `GOMOKU_TRUST_PROXY_HEADERS` 才能按真实客户端 IP 限流。
- 自建房双方都申请再战后，服务器创建新对局（不计分）并交换黑白；排位房拒绝再战。
- 服务重启时未终局对局统一标记为 `abandoned`，客户端回到大厅；不伪装成崩溃恢复。
- 自建房使用过期时间 60 秒的定向邀请；邀请要求双方为好友且被邀请者在线。内部仍保留随机房间码用于数据库记录，但客户端不再展示或要求输入。
- 匹配队列在断线时移除该玩家；重连并重新入队即可。

## 数据库兼容与迁移

- 开发默认 SQLite（`server/data/gomoku.db`），生产可切换 PostgreSQL。
- 启动时由 [`server/app/migrations.py`](app/migrations.py) 执行 Alembic：
  - 全新数据库执行基线迁移 `0001_baseline`，再执行
    `0002_seasons_glicko_reports`（赛季/Glicko-2/时限/举报）；
  - 已登记 `alembic_version` 的数据库执行 `upgrade head`；
  - 引入 Alembic 之前的旧库会先用 `create_tables` 补齐当前模型结构，再直接登记 `head`
    （避免后续迁移重复执行 `ADD COLUMN`）。
- 后续结构变更必须新增 `server/migrations/versions/` 下的迁移，并在本地执行
  `alembic upgrade head` 验证；不要再依赖 `create_all` 修改已存在的表。
- `rating_events` 有 `(game_id, user_id)` 唯一约束，防止同一局重复入账；
  `season_ratings` 有 `(season_id, user_id)` 唯一约束，防止重复归档；
  `game_reports` 有 `(game_id, reporter_id)` 唯一约束，防止同一人重复举报同一局。
- 多实例部署前需要给迁移加数据库级 advisory lock，避免多个 worker 同时升级。
- 正式公开化前仍建议备份恢复演练和数据库连接池监控。

常用命令（在项目根目录执行）：

```powershell
# 查看当前版本
server\.venv\Scripts\alembic.exe -c server\alembic.ini current

# 升级到最新版本
server\.venv\Scripts\alembic.exe -c server\alembic.ini upgrade head

# 生成新迁移（先设置 GOMOKU_DATABASE_URL）
$env:GOMOKU_DATABASE_URL = "sqlite+aiosqlite:///server/data/gomoku.db"
server\.venv\Scripts\alembic.exe -c server\alembic.ini revision --autogenerate -m "描述"
```

### PostgreSQL

本机没有安装 PostgreSQL，因此开发默认 SQLite。生产建议安装 PostgreSQL 并执行：

```powershell
server\.venv\Scripts\python.exe -m pip install --require-hashes -r server\requirements.lock
server\.venv\Scripts\python.exe -m pip install --require-hashes -r server\requirements-postgres.lock
$env:GOMOKU_DATABASE_URL = "postgresql+asyncpg://user:password@host:5432/gomoku"
```

修改 `requirements*.txt` 后，用 uv 重新生成锁文件：

```powershell
.\.venv\Scripts\uv.exe pip compile --universal --generate-hashes --no-header server\requirements.txt -o server\requirements.lock
.\.venv\Scripts\uv.exe pip compile --universal --generate-hashes --no-header server\requirements-dev.txt -o server\requirements-dev.lock
.\.venv\Scripts\uv.exe pip compile --universal --generate-hashes --no-header server\requirements-postgres.txt -o server\requirements-postgres.lock
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
| `GOMOKU_DISCONNECT_FORFEIT_SECONDS` | `20` | 掉线后等待重连的宽限时间，超时判逃跑，0～300 |
| `GOMOKU_WS_AUTH_TIMEOUT` | `10` | WebSocket 首帧认证超时 |
| `GOMOKU_WS_IDLE_TIMEOUT` | `75` | 客户端心跳间隔需短于此 |
| `GOMOKU_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `GOMOKU_LOG_FORMAT` | `text` | `text` 开发可读；`json` 生产采集 |
| `GOMOKU_UNDO_LIMIT_PER_GAME` | `3` | 每方每局悔棋次数上限，0 表示关闭悔棋 |
| `GOMOKU_UNDO_COOLDOWN_SECONDS` | `10` | 同一玩家两次悔棋请求的最小间隔，0 表示无冷却 |
| `GOMOKU_MAX_SPECTATORS_PER_ROOM` | `50` | 每局观战人数上限 |
| `GOMOKU_MAX_CONNECTIONS_PER_IP` | `20` | 单个 IP 的已认证 WebSocket 连接上限 |
| `GOMOKU_WS_COMMAND_BURST` | `40` | 命令令牌桶容量（心跳不计入） |
| `GOMOKU_WS_COMMAND_WINDOW_SECONDS` | `10` | 命令令牌桶补充窗口，1～3600 秒 |
| `GOMOKU_TRUST_PROXY_HEADERS` | `0` | 位于可信反向代理后时设为 1，读取 X-Forwarded-For/X-Real-IP |
| `GOMOKU_SEASON_LENGTH_DAYS` | `90` | 排位赛季长度，1～3650 天 |
| `GOMOKU_ADMIN_USERNAMES` | 空 | 逗号分隔的管理员用户名白名单，可访问举报复核接口 |

起始积分、Glicko-2 参数、段位阈值与赛季软重置公式定义在 `server/app/ratings.py`、
`server/app/glicko2.py` 与 `server/app/seasons.py`，修改后需同步更新文档；
不通过环境变量开放，避免部署时不同实例积分规则漂移。

## 日志、健康检查与指标

- 日志只记录请求元数据（方法、路由、状态码、耗时、请求 ID、用户/对局 ID），
  不记录请求体、Authorization 头、密码或 token。
- HTTP 响应会回写 `X-Request-ID`；客户端传入非法值时服务器生成新的 UUID。
- 健康检查：
  - `GET /api/v1/health`、`GET /api/v1/health/live`：进程存活；
  - `GET /api/v1/health/ready`：额外执行 `SELECT 1`，数据库不可用时返回 503。
- Prometheus 指标在 `GET /metrics`，包含：
  - HTTP 请求数/耗时、WebSocket 连接数、房间/观战/匹配队列数；
  - 落子确认数、落子数据库事务耗时；
  - 对局开始/结束（按终局原因）、重连数、WebSocket 消息异常数；
  - 命令限流与单 IP 连接超限被拒绝的次数。
- `/metrics` 默认无鉴权，生产应在反向代理层限制为内网或监控网段访问。
- 生产建议 `GOMOKU_LOG_FORMAT=json`，配合 Loki/ELK/云日志采集。

## Docker 部署

项目根目录提供 `docker-compose.yml`，包含服务端和 PostgreSQL 17：

```powershell
# 在项目根目录创建 .env
# GOMOKU_JWT_SECRET=至少32字符随机串
# GOMOKU_DB_PASSWORD=URL安全字符的数据库密码
docker compose up -d --build
docker compose logs -f server
```

Compose 会等待 PostgreSQL 健康后启动服务端；数据库数据保存在
`gomoku-db` volume，服务端镜像以非 root 用户运行。

只构建服务端镜像：

```powershell
docker build -f server/Dockerfile -t gomoku-server .
```

用 SQLite 直接运行镜像（开发/小规模）：

```powershell
docker run -d --name gomoku-server -p 8000:8000 `
  -e GOMOKU_SERVER_ENV=development `
  -e GOMOKU_JWT_SECRET=replace-with-a-long-random-secret-32chars+ `
  -v gomoku-data:/app/server/data `
  gomoku-server
```

生产必须通过 HTTPS/WSS 反向代理访问；不要直接暴露容器端口的明文 HTTP。

## 生产部署要点

1. 只运行 **一个 Uvicorn worker**；当前房间、匹配队列和身份互斥状态在进程内存中。
   多实例需要 Redis/房间与队列路由后再开启。
2. 使用 Caddy/Nginx 终止 TLS，对外只开放 HTTPS/WSS 443；不要把开发 HTTP/WS
   服务直接暴露到公网。
3. 固定随机 `GOMOKU_JWT_SECRET`，限制配置文件权限，不把 secret 写入客户端或仓库。
4. 配置每日 PostgreSQL 备份、健康检查和日志收集；设置防火墙，仅开放必要端口。
5. 客户端 token 目前只存内存，应用重启需要重新登录；后续可用 Windows DPAPI 安全保存 refresh token。
6. 排位已通过“仅匹配计分、自建房不计分、排位不可直接再战”降低刷分面；举报与回放审计
   只提供客观异常摘要，仍无法自动识别排队串通、故意送分和外部 AI 辅助。管理员需通过
   `GOMOKU_ADMIN_USERNAMES` 白名单人工复核，处罚不会由服务端自动执行。
7. 若未来在中国大陆公开运营，需单独评估 ICP/公安备案、实名/未成年人保护、隐私与游戏出版等要求；
   当前定位仅为内测。

## 测试

```powershell
cd C:\Users\a1325\Desktop\Gomoku
server\.venv\Scripts\python.exe -m unittest discover -s server/tests -v
```

当前 **121 项**测试覆盖：

- 用户名 + 密码注册、登录、refresh 轮换/重放、登出、认证响应积分/成就字段；
- 双 WebSocket 建房/邀请/加入/落子/拒绝/认输/再战、服务器五连判定、断线重连；
- 悔棋请求的同意/拒绝、回滚数据库落子、次数/冷却、非本回合请求拒绝、掉线宽限后的逃跑判负；
- 单 IP 连接上限、WS 命令令牌桶、观战开关与人数上限、协议能力协商、自建房每手时间；
- 排位固定每手 30 秒、自建房每手/Fischer 时限、非法时限拒绝、Fischer 加秒与双方余量、per-move 回合重置；
- Alembic 全新库迁移到 head、旧库自动补齐并登记 head、重复启动幂等；
- 健康检查 live/ready、请求 ID 回写与非法值清洗、Prometheus 核心指标、日志级别/格式配置；
- 快速匹配、取消匹配、排位房、随机黑白、排位固定 30 秒、终局 Glicko-2 结算、自建房不计分；
- 排位资料、赛季排名、当前赛季接口、赛季到期归档与积分软重置、历史赛季排行榜 404；
- 排行榜、个人资料与最近战绩；
- 最近对局/单局棋谱的参与者权限、分页、认输终局；
- 回放审计：序列/颜色/座位/重复落点/终局一致性/时间倒流/秒回警告，参与者与管理员权限；
- 举报提交、重复举报拒绝、只能举报已结束对局、我的举报、管理员列表与复核；
- 预置头像更新、自定义头像上传/读取/移除、非法格式/过大/超尺寸图片拒绝；
- 用户搜索、好友申请/同意/删除、在线状态推送、私聊历史与实时推送；
- 自建房邀请接受/拒绝、非好友/离线好友拒绝，以及快照中的双方 user_id/头像/勋章；
- 排位首胜/连胜结算与新解锁成就快照；
- Glicko-2 对称性/平局/下克上/RD 收敛/波动率稳定、段位边界/最低分保护、赛季软重置；
- 观战（第三客户端旁观）：只读快照与实时更新、落子/认输/悔棋/再战/发言一律被拒、
  玩家不能观战自己的对局、断线与退出观战不影响对局双方、断线重连补发快照、
  `GET /games/live` 进行中对局列表，以及开局/终局时向空闲客户端推送 `live_games_changed`。

另做过本机真实 Uvicorn + 两个 WebSocket 客户端完整排位对局验证：两名 1200 分玩家
对局后变为 1220/1180，排行榜与最近战绩同步更新。
另做过本机真实 Uvicorn + 三个 WebSocket 客户端的观战验证：两名玩家对局、
第三方旁观，旁观者操作无效，旁观者退出或断线后玩家照常对局。
