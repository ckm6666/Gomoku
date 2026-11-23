#!/usr/bin/env python
"""客户端打包自检脚本。

按「构建 → 校验烘焙配置 → 校验产物 → 离屏冒烟启动」四步验证一次打包：

    python tools/build_test.py --server-url https://gomoku.example.com
    python tools/build_test.py --skip-build          # 只校验已有产物（CI 用）

脚本不依赖当前工作目录：仓库根目录由 ``__file__`` 推导。

注意：Windows 下 Qt 用 ``SHGetKnownFolderPath`` 解析
``QStandardPaths.AppLocalDataLocation``，不认 ``LOCALAPPDATA`` 环境变量，
所以冒烟测试不能靠改环境变量来隔离日志，而是直接算出真实日志路径，
并只检查本次启动新追加的内容。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
BUILD_PY = ROOT / "build.py"
EXE = ROOT / "dist" / "Gomoku.exe"
BUILD_INFO = ROOT / "build" / "build_info.json"

# 服务端更新清单接口路径（与 gomoku/client/updater.py 保持一致）
UPDATE_MANIFEST_PATH = "/api/v1/updates/latest"

# 构建阶段给足时间（真实 PyInstaller 打包通常需要数分钟），
# 冒烟阶段只需要把程序拉起来并读到启动日志。
DEFAULT_BUILD_TIMEOUT = 1800.0
DEFAULT_SMOKE_TIMEOUT = 20.0
GRACE_SECONDS = 3.0
POLL_INTERVAL = 0.25

STARTUP_MARK = "应用启动 version="
UPDATE_MARK = "更新清单地址"
NOT_CONFIGURED = "未配置"

# build_info.json 里允许出现的键（build.py 的固定契约）
KNOWN_KEYS = frozenset({"api_base_url", "update_manifest_url", "auto_update_check"})

# 客户端在 LocalAppData 下的相对路径：<LOCALAPPDATA>/Gomoku/Gomoku/logs/gomoku.log
LOG_RELATIVE = Path("Gomoku") / "Gomoku" / "logs" / "gomoku.log"

# 期望地址与 exe 里的地址对不上时，多半是 dist 里的 exe 是旧的
_STALE_HINT = "\n      （如果 build/build_info.json 之后被改过，说明 dist 里的 exe 是旧的，需要重新打包。）"

# 运行时环境变量的优先级高于烘焙配置，冒烟测试必须清掉它们，
# 否则测到的是环境变量而不是 exe 里烘焙的配置。
RUNTIME_ENV_VARS = (
    "GOMOKU_API_BASE_URL",
    "GOMOKU_UPDATE_MANIFEST_URL",
    "GOMOKU_AUTO_UPDATE_CHECK",
)


class BuildTestError(RuntimeError):
    """自检失败（总是带中文说明）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _say(message: str) -> None:
    print(message, flush=True)


def _step(index: int, total: int, message: str) -> None:
    _say(f"[{index}/{total}] {message}")


def _norm_url(value: str) -> str:
    """比较 URL 时忽略首尾空白与结尾斜杠。"""
    return value.strip().rstrip("/")


def _display(path: Path) -> str:
    """尽量以仓库相对路径展示，便于阅读。"""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _terminate(proc: Optional[subprocess.Popen]) -> None:
    """结束子进程（Windows 下连同子进程树一起结束）。"""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# 第 1 步：构建
# ---------------------------------------------------------------------------


def _build_command(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, str(BUILD_PY)]
    if args.fast:
        cmd.append("--fast")
    if args.server_url:
        cmd += ["--server-url", args.server_url]
    if args.update_url:
        cmd += ["--update-url", args.update_url]
    if args.auto_update_check is True:
        cmd.append("--auto-update-check")
    elif args.auto_update_check is False:
        cmd.append("--no-auto-update-check")
    return cmd


def _run_build(args: argparse.Namespace) -> None:
    cmd = _build_command(args)
    _say("      $ " + " ".join(cmd))
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except OSError as exc:
        raise BuildTestError(f"无法启动构建进程：{exc}") from exc

    lines: "queue.Queue[Optional[str]]" = queue.Queue()

    def _reader() -> None:
        stream = proc.stdout
        assert stream is not None
        try:
            for line in stream:
                lines.put(line)
        finally:
            lines.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    deadline = time.monotonic() + args.timeout
    completed = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BuildTestError(
                    f"构建超时（超过 {args.timeout:g} 秒仍未结束），已终止构建进程。"
                )
            try:
                item = lines.get(timeout=min(POLL_INTERVAL, remaining))
            except queue.Empty:
                continue
            if item is None:
                completed = True
                break
            _say(item.rstrip("\n"))
    finally:
        # 只有没跑完（超时或异常）时才去结束构建进程树。
        if not completed:
            _terminate(proc)

    code = proc.wait()
    if code != 0:
        raise BuildTestError(f"构建失败：`build.py` 退出码 {code}，请查看上方打包日志。")


# ---------------------------------------------------------------------------
# 第 2 步：校验烘焙配置
# ---------------------------------------------------------------------------


def _load_build_info() -> dict[str, Any]:
    if not BUILD_INFO.exists():
        raise BuildTestError(
            f"缺少烘焙配置文件：{_display(BUILD_INFO)}。"
            "请先执行 `python build.py`（或去掉 --skip-build 让脚本自行构建）。"
        )
    raw = BUILD_INFO.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        _say("      [警告] build_info.json 带有 UTF-8 BOM，建议写成无 BOM 的 UTF-8。")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildTestError(f"烘焙配置不是合法的 UTF-8 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise BuildTestError("烘焙配置必须是一个 JSON 对象。")
    return data


def _expected_manifest_url(
    server_url: Optional[str], update_url: Optional[str]
) -> Optional[str]:
    """由命令行参数推导期望的更新清单地址。

    运行时会把地址末尾的斜杠去掉，比较时统一按去掉斜杠的形式。
    """
    if update_url:
        return _norm_url(update_url)
    if server_url:
        return _norm_url(server_url) + UPDATE_MANIFEST_PATH
    return None


def _manifest_url_from_build_info(info: dict[str, Any]) -> Optional[str]:
    """由磁盘上的烘焙配置推导运行时会使用的更新清单地址。

    运行时优先级：环境变量 → 烘焙的 update_manifest_url →
    烘焙的 api_base_url 推导 → 无。
    """
    explicit = info.get("update_manifest_url")
    if isinstance(explicit, str) and explicit.strip():
        return _norm_url(explicit)
    api = info.get("api_base_url")
    if isinstance(api, str) and api.strip():
        return _norm_url(api) + UPDATE_MANIFEST_PATH
    return None


def _check_key(actual: dict[str, Any], key: str, expected: Any) -> Optional[str]:
    """比对单个键，返回错误说明（None 表示通过）。"""
    if key not in actual:
        return f"缺少键 `{key}`（期望值 {expected!r}）"
    got = actual[key]
    if isinstance(expected, str) and isinstance(got, str):
        if _norm_url(got) != _norm_url(expected):
            return f"`{key}` 不匹配：期望 {expected!r}，实际 {got!r}"
        return None
    if got != expected:
        return f"`{key}` 不匹配：期望 {expected!r}，实际 {got!r}"
    return None


def _verify_build_info(args: argparse.Namespace) -> tuple[dict[str, Any], Optional[str]]:
    """校验 build/build_info.json，返回（实际内容，期望的更新清单地址）。"""
    info = _load_build_info()
    _say(f"      烘焙配置：{json.dumps(info, ensure_ascii=False)}")

    if args.skip_build:
        # --skip-build 的语义是「验证已有的构建」，期望值以磁盘上的文件为准，
        # 命令行参数只用于交叉校验（CI 里用来确认 build.py 真的烘焙了参数）。
        expected_manifest = _manifest_url_from_build_info(info)
        problems: list[str] = []
        if args.server_url:
            problem = _check_key(info, "api_base_url", args.server_url)
            if problem:
                problems.append(problem)
        if args.update_url:
            problem = _check_key(info, "update_manifest_url", args.update_url)
            if problem:
                problems.append(problem)
        if args.auto_update_check is not None:
            problem = _check_key(info, "auto_update_check", args.auto_update_check)
            if problem:
                problems.append(problem)
        if problems:
            raise BuildTestError(
                "烘焙配置与命令行参数不一致：\n      - "
                + "\n      - ".join(problems)
                + f"\n      实际文件：{json.dumps(info, ensure_ascii=False)}"
            )
        extra = sorted(set(info) - KNOWN_KEYS)
        if extra:
            _say(f"      [警告] build_info.json 含有预期外的键：{', '.join(extra)}")
        _say(f"      期望的更新清单地址：{expected_manifest or NOT_CONFIGURED}")
        return info, expected_manifest

    # 正常模式：期望值由命令行参数推导，build_info.json 必须与之一致。
    expected: dict[str, Any] = {}
    if args.server_url:
        expected["api_base_url"] = args.server_url
    if args.update_url:
        expected["update_manifest_url"] = args.update_url
    if args.auto_update_check is not None:
        expected["auto_update_check"] = args.auto_update_check

    problems = []
    for key, value in expected.items():
        problem = _check_key(info, key, value)
        if problem:
            problems.append(problem)
    extra = sorted(set(info) - set(expected))
    if problems:
        raise BuildTestError(
            "烘焙配置与构建参数不一致：\n      - "
            + "\n      - ".join(problems)
            + f"\n      实际文件：{json.dumps(info, ensure_ascii=False)}"
        )
    if extra:
        _say(f"      [警告] build_info.json 含有预期外的键：{', '.join(extra)}")

    expected_manifest = _expected_manifest_url(args.server_url, args.update_url)
    actual_manifest = _manifest_url_from_build_info(info)
    if expected_manifest != actual_manifest:
        raise BuildTestError(
            "更新清单地址不一致："
            f"按构建参数应为 {expected_manifest or NOT_CONFIGURED}，"
            f"按烘焙配置推导为 {actual_manifest or NOT_CONFIGURED}。"
        )
    _say(f"      期望的更新清单地址：{expected_manifest or NOT_CONFIGURED}")
    return info, expected_manifest


# ---------------------------------------------------------------------------
# 第 3 步：校验产物
# ---------------------------------------------------------------------------


def _verify_artifact() -> float:
    if not EXE.exists():
        raise BuildTestError(f"缺少产物：{_display(EXE)}，请先执行 `python build.py`。")
    size = EXE.stat().st_size
    if size <= 0:
        raise BuildTestError(f"产物为空文件：{_display(EXE)}。")
    size_mb = size / 1024 / 1024
    _say(f"      产物：{_display(EXE)}（{size_mb:.1f} MB）")
    return size_mb


# ---------------------------------------------------------------------------
# 第 4 步：冒烟测试
# ---------------------------------------------------------------------------


def _resolve_log_path() -> Path:
    """返回客户端真实的日志路径。

    Windows 下 Qt 通过 ``SHGetKnownFolderPath(FOLDERID_LocalAppData)`` 解析
    ``QStandardPaths.AppLocalDataLocation``，**不认** ``LOCALAPPDATA`` 环境变量，
    所以不能靠「改环境变量再找临时目录」定位日志，必须算出真实路径。
    客户端在创建路径前把 applicationName 与 organizationName 都设为 Gomoku，
    因此真实路径是 ``<LocalAppData>/Gomoku/Gomoku/logs/gomoku.log``。
    """
    try:  # 优先用与客户端完全一致的方式解析
        from PySide6.QtCore import QCoreApplication, QStandardPaths

        QCoreApplication.setOrganizationName("Gomoku")
        QCoreApplication.setApplicationName("Gomoku")
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        if base:
            return Path(base) / "logs" / "gomoku.log"
    except Exception:  # pragma: no cover - 没有 PySide6 时走下面的兜底
        pass
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / LOG_RELATIVE


def _read_appended(path: Path, offset: int) -> str:
    """读取日志文件在 ``offset`` 之后新追加的内容。"""
    try:
        if not path.is_file():
            return ""
        size = path.stat().st_size
        if size < offset:
            # 日志滚动过，从头读。
            offset = 0
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _check_log_content(text: str, expected_manifest: Optional[str]) -> None:
    """校验本次启动新写入的日志内容。"""
    lines = text.splitlines()
    if not any(STARTUP_MARK in line for line in lines):
        raise BuildTestError(
            f"日志中缺少启动标记「{STARTUP_MARK}」，程序可能没有正常启动。"
        )
    if any("Traceback" in line for line in lines):
        raise BuildTestError(
            "日志中出现 Traceback，程序启动过程抛出异常：\n" + "\n".join(lines[-20:])
        )

    update_lines = [line for line in lines if UPDATE_MARK in line]
    if not update_lines:
        raise BuildTestError(f"日志中缺少「{UPDATE_MARK}」行，无法确认更新清单地址。")
    update_line = update_lines[-1]
    if expected_manifest:
        if expected_manifest not in update_line:
            raise BuildTestError(
                "更新清单地址不符合预期："
                f"期望 {expected_manifest}，实际日志行「{update_line.strip()}」。"
                + _STALE_HINT
            )
    else:
        if NOT_CONFIGURED not in update_line:
            raise BuildTestError(
                "更新清单地址不符合预期："
                f"期望「{NOT_CONFIGURED}」，实际日志行「{update_line.strip()}」。"
                + _STALE_HINT
            )
    _say(f"      日志中的更新清单地址：{update_line.strip()}")


def _run_smoke(expected_manifest: Optional[str], timeout: float) -> Path:
    temp_dir = Path(tempfile.mkdtemp(prefix="gomoku-build-test-"))
    env = os.environ.copy()
    # 只改离屏平台：Qt 的 LOCALAPPDATA 走 SHGetKnownFolderPath，改环境变量没用，
    # 日志仍然写在真实用户目录里（见 _resolve_log_path）。
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUTF8"] = "1"
    for name in RUNTIME_ENV_VARS:
        env.pop(name, None)

    log_path = _resolve_log_path()
    # 记录启动前的日志大小，之后只检查新追加的内容，避免和历史日志混在一起。
    offset = log_path.stat().st_size if log_path.is_file() else 0

    proc: Optional[subprocess.Popen] = None
    try:
        try:
            proc = subprocess.Popen(
                [str(EXE)],
                cwd=str(temp_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise BuildTestError(f"无法启动 {_display(EXE)}：{exc}") from exc
        _say(f"      已启动 {_display(EXE)}（pid={proc.pid}），等待启动日志…")
        _say(f"      日志文件：{log_path}")

        deadline = time.monotonic() + timeout
        started = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise BuildTestError(
                    f"程序启动后立刻退出（退出码 {proc.returncode}），"
                    "这是一个 windowed 程序，崩溃不会弹窗，请查看日志。"
                )
            if STARTUP_MARK in _read_appended(log_path, offset):
                started = True
                break
            time.sleep(POLL_INTERVAL)

        if not started:
            raise BuildTestError(
                f"在 {timeout:g} 秒内没有等到启动日志（{STARTUP_MARK}），"
                "程序可能卡在启动阶段或无法创建窗口。"
            )

        # 崩溃检查：再观察一小段时间，程序必须还活着。
        time.sleep(GRACE_SECONDS)
        if proc.poll() is not None:
            raise BuildTestError(
                f"程序在启动后 {GRACE_SECONDS:g} 秒内退出（退出码 {proc.returncode}），"
                "疑似启动即崩溃。"
            )
        _say(f"      程序保持运行中（pid={proc.pid}），未崩溃。")

        _check_log_content(_read_appended(log_path, offset), expected_manifest)
        return log_path
    finally:
        _terminate(proc)
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/build_test.py",
        description=(
            "客户端打包自检：构建 → 校验烘焙配置 → 校验产物 → 离屏冒烟启动。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/build_test.py --server-url https://gomoku.example.com\n"
            "  python tools/build_test.py --skip-build --server-url https://x.example\n"
        ),
    )
    parser.add_argument("--server-url", help="烘焙进 exe 的联机/API 基址")
    parser.add_argument("--update-url", help="烘焙进 exe 的更新清单地址")
    auto = parser.add_mutually_exclusive_group()
    auto.add_argument(
        "--auto-update-check",
        dest="auto_update_check",
        action="store_true",
        help="烘焙「启动时自动检查更新 = 开」",
    )
    auto.add_argument(
        "--no-auto-update-check",
        dest="auto_update_check",
        action="store_false",
        help="烘焙「启动时自动检查更新 = 关」",
    )
    parser.set_defaults(auto_update_check=None)
    parser.add_argument("--fast", action="store_true", help="构建时跳过 PyInstaller --clean")
    parser.add_argument(
        "--skip-build", action="store_true", help="跳过构建，直接校验已有产物"
    )
    parser.add_argument("--skip-smoke", action="store_true", help="跳过冒烟启动测试")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_BUILD_TIMEOUT,
        help=f"构建阶段超时上限（秒，默认 {DEFAULT_BUILD_TIMEOUT:g}）",
    )
    parser.add_argument(
        "--smoke-timeout",
        type=float,
        default=DEFAULT_SMOKE_TIMEOUT,
        help=f"冒烟阶段超时上限（秒，默认 {DEFAULT_SMOKE_TIMEOUT:g}）",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    total = 4

    _step(1, total, "构建客户端…")
    if args.skip_build:
        _say("      已指定 --skip-build，跳过构建，直接校验已有产物。")
    else:
        _run_build(args)

    _step(2, total, "校验烘焙配置…")
    info, expected_manifest = _verify_build_info(args)

    _step(3, total, "校验产物…")
    size_mb = _verify_artifact()

    _step(4, total, "冒烟测试（离屏启动 exe）…")
    if args.skip_smoke:
        _say("      已指定 --skip-smoke，跳过冒烟启动测试。")
        log_path = None
    else:
        log_path = _run_smoke(expected_manifest, args.smoke_timeout)

    _say("")
    _say("[OK] 打包自检全部通过")
    _say(f"     烘焙配置：{json.dumps(info, ensure_ascii=False)}")
    _say(f"     产物：{_display(EXE)}（{size_mb:.1f} MB）")
    if log_path is not None:
        _say(f"     日志：{log_path}")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError):
            pass
    try:
        raise SystemExit(main(sys.argv[1:]))
    except BuildTestError as _exc:
        print(f"[失败] {_exc}", file=sys.stderr)
        raise SystemExit(1) from _exc
    except KeyboardInterrupt:
        print("[失败] 已被用户中断。", file=sys.stderr)
        raise SystemExit(130) from None
