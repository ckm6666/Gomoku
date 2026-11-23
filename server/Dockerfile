# 仅服务端的生产镜像。构建上下文必须是项目根目录：
#   docker build -f server/Dockerfile -t gomoku-server .
#
# 镜像同时安装 SQLite 与 PostgreSQL 驱动；生产推荐通过环境变量连接
# PostgreSQL。运行参数和密钥只通过环境变量注入，不写入镜像。

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

RUN groupadd --system gomoku \
    && useradd --system --gid gomoku --no-create-home --home-dir /app gomoku

COPY server/requirements.lock server/requirements-postgres.lock ./server/
RUN python -m pip install --require-hashes \
    -r server/requirements.lock \
    -r server/requirements-postgres.lock

# 服务端只依赖 gomoku.shared；不把客户端代码/资源打进镜像。
COPY gomoku/__init__.py ./gomoku/__init__.py
COPY gomoku/shared ./gomoku/shared
COPY server ./server

RUN mkdir -p /app/server/data \
    && chown -R gomoku:gomoku /app

USER gomoku

ENV GOMOKU_SERVER_ENV=production

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=3)"]

# 访问日志由应用自己的中间件输出，关闭 Uvicorn 的重复访问日志。
CMD ["python", "-m", "uvicorn", "server.app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
