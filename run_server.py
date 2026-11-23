"""联机服务端快捷启动脚本。

等价于：
    python -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000

但额外做了这些事：

1. 自动挑选解释器：优先使用独立的服务端虚拟环境 ``server/.venv``，
   其次回退到当前解释器（前提是已安装 fastapi / uvicorn）。
2. 自动补齐环境变量：``GOMOKU_SERVER_ENV`` 与持久化的 ``GOMOKU_JWT_SECRET``，
   重启服务后客户端的登录 token 依然有效。
3. ``--production``：使用独立的 ``server/data/.prod_jwt_secret``，默认信任
   Tailscale Funnel / Caddy / Nginx 等反向代理传来的 ``X-Forwarded-For``，
   并关闭热重载。经代理却未信任代理头时，所有客户端会共享代理 IP，
   单 IP 连接上限会退化成全局上限。
4. ``--background`` / ``--status`` / ``--stop``：后台静默运行、查看状态与停止，
   PID 与日志写入 ``server/data/``。

用法：
    python run_server.py                     # 开发：前台 + 热重载
    python run_server.py --production        # 生产：前台、固定 secret、信任代理
    python run_server.py --background --production
    python run_server.py --status
    python run_server.py --stop
    python run_server.py --no-trust-proxy    # 不经代理直连时关闭代理头信任
"""

from __future__ import annotations

import argparse
import os
import secrets
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional, Sequence

ROOT = Path(__file__).resolve().parent
SERVER_VENV = ROOT / "server" / ".venv"
DATA_DIR = ROOT / "server" / "data"
DEV_SECRET_FILE = DATA_DIR / ".dev_jwt_secret"
PROD_SECRET_FILE = DATA_DIR / ".prod_jwt_secret"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_ENV = "development"


def _say(message: str = "", *, error: bool = False) -> None:
    """pythonw.exe 没有控制台，打印时静默跳过而不是抛异常。"""
    stream = sys.stderr if error else sys.stdout
    if stream is None:
        return
    print(message, file=stream, flush=True)


def _venv_python(venv_dir: Path, *, windowless: bool = False) -> Optional[Path]:
    """返回虚拟环境中的解释器路径，不存在则返回 None。"""
    if windowless:
        candidates = (
            venv_dir / "Scripts" / "pythonw.exe",  # Windows
            venv_dir / "bin" / "python",  # POSIX
        )
    else:
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
    """挑选用于前台启动服务的解释器。"""
    server_python = _venv_python(SERVER_VENV)
    if server_python is not None:
        return server_python
    current = Path(sys.executable)
    if _has_server_deps(current):
        return current
    return None


def _background_interpreter() -> Optional[Path]:
    """挑选用于后台启动服务的无控制台解释器。"""
    server_python = _venv_python(SERVER_VENV, windowless=True)
    if server_python is not None:
        return server_python
    current = Path(sys.executable)
    if not _has_server_deps(current):
        return None
    if os.name == "nt":
        windowless = current.with_name("pythonw.exe")
        if windowless.exists():
            return windowless
    return current


def _read_or_create_secret(path: Path) -> str:
    """读取（必要时生成）持久化的 JWT secret。"""
    try:
        cached = path.read_text(encoding="utf-8").strip()
    except OSError:
        cached = ""
    if len(cached) >= 32:
        return cached
    secret = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError:
        _say(
            f"[!] 无法写入 {path}，本次 secret 为一次性值，"
            "重启服务后客户端需要重新登录。",
            error=True,
        )
    return secret


def _build_env(environment: str, *, trust_proxy: Optional[bool]) -> dict[str, str]:
    """构造服务端进程的环境变量（不修改当前进程）。"""
    env = dict(os.environ)
    is_prod = environment.strip().lower() == "production"
    env["GOMOKU_SERVER_ENV"] = environment
    if not env.get("GOMOKU_JWT_SECRET", "").strip():
        secret_file = PROD_SECRET_FILE if is_prod else DEV_SECRET_FILE
        env["GOMOKU_JWT_SECRET"] = _read_or_create_secret(secret_file)
    if trust_proxy is True:
        env["GOMOKU_TRUST_PROXY_HEADERS"] = "1"
    elif trust_proxy is False:
        env["GOMOKU_TRUST_PROXY_HEADERS"] = "0"
    elif is_prod and not env.get("GOMOKU_TRUST_PROXY_HEADERS", "").strip():
        # 生产默认位于 Tailscale Funnel / Caddy / Nginx 等反向代理之后。
        env["GOMOKU_TRUST_PROXY_HEADERS"] = "1"
    root_entry = str(ROOT)
    existing = env.get("PYTHONPATH", "")
    if root_entry not in existing.split(os.pathsep):
        env["PYTHONPATH"] = (
            f"{root_entry}{os.pathsep}{existing}" if existing else root_entry
        )
    return env


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
        "--production",
        action="store_true",
        help="生产模式：固定 secret、默认信任代理头、默认关闭热重载",
    )
    parser.add_argument(
        "--reload",
        dest="reload",
        action="store_true",
        default=None,
        help="开启热重载（仅前台；开发环境默认开启）",
    )
    parser.add_argument(
        "--no-reload",
        dest="reload",
        action="store_false",
        help="关闭热重载（生产或压测时使用）",
    )
    parser.add_argument(
        "--background",
        action="store_true",
        help="后台静默运行（不弹出控制台窗口）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="查看后台/前台服务的健康状态",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="停止由 --background 启动的服务",
    )
    parser.add_argument(
        "--trust-proxy",
        dest="trust_proxy",
        action="store_true",
        default=None,
        help="信任 X-Forwarded-For（位于 Tailscale Funnel/Caddy/Nginx 之后时开启）",
    )
    parser.add_argument(
        "--no-trust-proxy",
        dest="trust_proxy",
        action="store_false",
        help="不信任代理头（服务直接暴露给客户端时使用）",
    )
    return parser.parse_args(argv)


def _probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("", "0.0.0.0", "::") else host


def _health_url(host: str, port: str) -> str:
    return f"http://{_probe_host(host)}:{port}/api/v1/health"


def _health(host: str, port: str, timeout: float = 1.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(
            _health_url(host, port), timeout=timeout
        ) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status == 200, body
    except Exception as exc:  # 连接拒绝、超时、端口没有服务等
        return False, str(exc)


def _pid_path(port: str) -> Path:
    return DATA_DIR / f"server-{port}.pid"


def _log_paths(port: str) -> tuple[Path, Path]:
    return (
        DATA_DIR / f"server-{port}.out.log",
        DATA_DIR / f"server-{port}.err.log",
    )


def run_foreground(
    args: argparse.Namespace, environment: str, argv: list[str]
) -> int:
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
        command = [str(interpreter), str(Path(__file__).resolve()), *argv]
        print(f"使用服务端虚拟环境启动：{interpreter}")
        try:
            return subprocess.run(command, cwd=str(ROOT)).returncode
        except KeyboardInterrupt:
            return 0
        except OSError as exc:
            print(f"启动服务端失败：{exc}", file=sys.stderr)
            return 1

    os.environ.update(_build_env(environment, trust_proxy=args.trust_proxy))

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

    default_reload = environment.strip().lower() != "production"
    reload = args.reload if args.reload is not None else default_reload
    app_ref = "server.app.main:app"
    print(f"服务地址：http://{args.host}:{args.port}")
    print(f"健康检查：http://{args.host}:{args.port}/api/v1/health")
    print(f"WebSocket：ws://{args.host}:{args.port}/api/v1/ws")
    print(f"运行环境：{environment}")
    print(f"热重载：{'开启' if reload else '关闭'}")
    print("按 Ctrl+C 停止服务。")

    uvicorn.run(
        app_ref,
        host=args.host,
        port=args.port,
        reload=reload,
        reload_dirs=[str(ROOT)] if reload else None,
        log_level="info",
    )
    return 0


def run_background(args: argparse.Namespace, environment: str) -> int:
    if args.reload:
        _say(
            "[!] --background 不支持 --reload，请在前台使用热重载。",
            error=True,
        )
        return 2
    host = args.host
    port = str(args.port)
    if _health(host, port)[0]:
        _say(f"[OK] 服务器已在运行：{_health_url(host, port)}")
        return 0
    python = _background_interpreter()
    if python is None:
        _say(
            "[!] 未找到可用的后台解释器。请先创建 server\\.venv 并安装依赖：\n"
            "    py -3 -m venv server\\.venv\n"
            "    server\\.venv\\Scripts\\python.exe -m pip install "
            "-r server\\requirements.txt",
            error=True,
        )
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_log, err_log = _log_paths(port)
    pid_path = _pid_path(port)
    env = _build_env(environment, trust_proxy=args.trust_proxy)
    cmd = [
        str(python),
        "-m",
        "uvicorn",
        "server.app.main:app",
        "--host",
        host,
        "--port",
        port,
        "--log-level",
        "info",
    ]

    kwargs: dict[str, object] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": env,
    }
    if os.name == "nt":
        # 完全脱离当前控制台，避免后台服务弹出窗口。
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True

    with open(out_log, "ab") as out, open(err_log, "ab") as err:
        process = subprocess.Popen(
            cmd,
            stdout=out,
            stderr=err,
            **kwargs,
        )
    pid_path.write_text(str(process.pid), encoding="utf-8")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _health(host, port, timeout=0.4)[0]:
            _say(f"[OK] 后台服务器已启动：PID {process.pid}")
            _say(f"     运行环境：{environment}")
            _say(f"     健康检查：{_health_url(host, port)}")
            _say(f"     日志：{out_log} / {err_log}")
            _say(f"     停止：python run_server.py --stop --port {port}")
            return 0
        time.sleep(0.2)

    if process.poll() is not None:
        pid_path.unlink(missing_ok=True)
        _say("[!] 后台服务器启动失败，请查看日志：", error=True)
        _say(f"     {err_log}", error=True)
        return 1

    _say(
        f"[OK] 后台服务器进程已启动（PID {process.pid}），"
        "健康检查稍后可用。"
    )
    _say(f"     日志：{out_log} / {err_log}")
    return 0


def _taskkill(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False
    # 只结束 python 进程，避免 PID 被复用时误杀其他程序。
    try:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.lower()
    except Exception:
        return False
    if "python" not in listing:
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def run_stop(args: argparse.Namespace) -> int:
    port = str(args.port)
    pid_path = _pid_path(port)
    if not pid_path.exists():
        _say(
            "没有找到后台服务器的 PID 文件；"
            "如果服务器在前台运行，请在终端按 Ctrl+C。"
        )
        return 1
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        _say("PID 文件无效，已清理。", error=True)
        return 1
    if not _taskkill(pid):
        pid_path.unlink(missing_ok=True)
        _say("后台服务器已不在运行，已清理 PID 文件。")
        return 0
    pid_path.unlink(missing_ok=True)
    _say(f"[OK] 已停止后台服务器（PID {pid}）。")
    return 0


def run_status(args: argparse.Namespace) -> int:
    host = args.host
    port = str(args.port)
    running, detail = _health(host, port)
    if not running:
        _say(f"[未运行] {_health_url(host, port)}（{detail}）")
        return 1
    pid_text = ""
    pid_path = _pid_path(port)
    if pid_path.exists():
        try:
            pid_text = (
                f"，PID {pid_path.read_text(encoding='utf-8').strip()}"
            )
        except OSError:
            pass
    _say(f"[运行中] {_health_url(host, port)}{pid_text}")
    _say(f"         健康检查：{detail}")
    out_log, err_log = _log_paths(port)
    _say(f"         日志：{out_log} / {err_log}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = _parse_args(raw_argv)
    if args.stop:
        return run_stop(args)
    if args.status:
        return run_status(args)

    environment = "production" if args.production else args.env
    if args.background:
        return run_background(args, environment)
    return run_foreground(args, environment, raw_argv)


if __name__ == "__main__":
    raise SystemExit(main())
