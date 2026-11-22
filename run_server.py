"""联机服务端快捷启动脚本。

等价于：
    python -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000

但额外做了三件事：

1. 自动挑选解释器：优先使用独立的服务端虚拟环境 ``server/.venv``，
   其次回退到当前解释器（前提是已安装 fastapi / uvicorn）。
2. 自动补齐开发环境变量：``GOMOKU_SERVER_ENV`` 与 ``GOMOKU_JWT_SECRET``。
   开发环境的 secret 持久化到 ``server/data/.dev_jwt_secret``，
   这样重启服务后客户端的登录 token 依然有效。
3. 无论从哪个目录调用，都会把项目根目录加入 ``sys.path``，
   保证 ``server`` 与 ``gomoku.shared`` 可以正常导入。

用法：
    python run_server.py                 # 127.0.0.1:8000（默认开启热重载）
    python run_server.py --port 9000     # 换端口
    python run_server.py --no-reload     # 关闭热重载（生产 / 压测）
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parent
SERVER_VENV = ROOT / "server" / ".venv"
DATA_DIR = ROOT / "server" / "data"
DEV_SECRET_FILE = DATA_DIR / ".dev_jwt_secret"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_ENV = "development"


def _venv_python(venv_dir: Path) -> Optional[Path]:
    """返回虚拟环境中的解释器路径，不存在则返回 None。"""
    candidates = (
        venv_dir / "Scripts" / "python.exe",  # Windows
        venv_dir / "bin" / "python",  # POSIX
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _has_server_deps(python: Path) -> bool:
    """检查指定解释器是否安装了 fastapi 与 uvicorn。"""
    probe = "import fastapi, uvicorn"
    try:
        result = subprocess.run(
            [str(python), "-c", probe],
            cwd=str(ROOT),
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _resolve_interpreter() -> Optional[Path]:
    """挑选用于启动服务的解释器。"""
    server_python = _venv_python(SERVER_VENV)
    if server_python is not None:
        return server_python
    current = Path(sys.executable)
    if _has_server_deps(current):
        return current
    return None


def _read_or_create_dev_secret() -> str:
    """读取（必要时生成）持久化的开发环境 JWT secret。"""
    try:
        cached = DEV_SECRET_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        cached = ""
    if len(cached) >= 32:
        return cached
    secret = secrets.token_urlsafe(48)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEV_SECRET_FILE.write_text(secret, encoding="utf-8")
    except OSError:
        # 落盘失败时退化为一次性 secret，重启后 token 失效但仍可正常启动。
        return secret
    return secret


def _prepare_env(environment: str) -> None:
    """补齐服务端所需的环境变量。"""
    os.environ.setdefault("GOMOKU_SERVER_ENV", environment)
    if not os.environ.get("GOMOKU_JWT_SECRET"):
        if environment.lower() == "production":
            raise SystemExit(
                "生产环境必须设置 GOMOKU_JWT_SECRET（至少 32 个字符）"
            )
        os.environ["GOMOKU_JWT_SECRET"] = _read_or_create_dev_secret()
    root_entry = str(ROOT)
    existing = os.environ.get("PYTHONPATH", "")
    if root_entry not in existing.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            f"{root_entry}{os.pathsep}{existing}" if existing else root_entry
        )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="快捷启动 Gomoku 联机服务端",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("GOMOKU_SERVER_HOST", DEFAULT_HOST),
        help=f"监听地址（默认 {DEFAULT_HOST}）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GOMOKU_SERVER_PORT", DEFAULT_PORT)),
        help=f"监听端口（默认 {DEFAULT_PORT}）",
    )
    parser.add_argument(
        "--env",
        default=os.environ.get("GOMOKU_SERVER_ENV", DEFAULT_ENV),
        help=f"运行环境（默认 {DEFAULT_ENV}）",
    )
    parser.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=True,
        help="代码变更时自动重启（默认开启）",
    )
    parser.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help="关闭热重载（生产或压测时使用）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)

    interpreter = _resolve_interpreter()
    if interpreter is None:
        print(
            "未找到可用的服务端环境。请先创建服务端虚拟环境并安装依赖：\n"
            "    py -3 -m venv server\\.venv\n"
            "    server\\.venv\\Scripts\\python.exe -m pip install -r server\\requirements.txt",
            file=sys.stderr,
        )
        return 1

    # 解释器不是当前进程时，转交给目标解释器重新执行本脚本。
    if Path(interpreter).resolve() != Path(sys.executable).resolve():
        command = [str(interpreter), str(Path(__file__).resolve()), *sys.argv[1:]]
        print(f"使用服务端虚拟环境启动：{interpreter}")
        try:
            return subprocess.run(command, cwd=str(ROOT)).returncode
        except KeyboardInterrupt:
            return 0
        except OSError as exc:
            print(f"启动服务端失败：{exc}", file=sys.stderr)
            return 1

    try:
        _prepare_env(args.env)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    try:
        import uvicorn
    except ImportError:
        print(
            "当前解释器缺少 uvicorn，请执行：\n"
            "    python -m pip install -r server\\requirements.txt",
            file=sys.stderr,
        )
        return 1

    app_ref = "server.app.main:app"
    print(f"服务地址：http://{args.host}:{args.port}")
    print(f"健康检查：http://{args.host}:{args.port}/api/v1/health")
    print(f"WebSocket：ws://{args.host}:{args.port}/api/v1/ws")
    print(f"热重载：{'开启' if args.reload else '关闭'}")
    print("按 Ctrl+C 停止服务。")

    uvicorn.run(
        app_ref,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT)] if args.reload else None,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
