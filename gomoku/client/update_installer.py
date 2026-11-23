"""自更新安装器：生成并异步执行 Windows 批处理脚本替换主程序。

模块只依赖标准库，不导入 PySide6。整体流程：

1. 下载完成后把新安装包放在临时目录（见 :func:`staged_download_path`）；
2. 用 :func:`build_update_script` 生成一段批处理脚本，先等待当前进程退出，
   再把新安装包覆盖到目标 exe，可选地重新启动主程序；
3. 用 :func:`schedule_install` 把脚本以后台进程方式启动，主进程即可退出。

所有对外函数都不会抛出异常（失败时返回 ``False`` 并写日志），
以免在退出流程中把应用带崩。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

__all__ = [
    "build_update_script",
    "current_executable_path",
    "is_self_update_supported",
    "schedule_install",
    "staged_download_path",
]

#: 模块级日志记录器（避免在库代码里直接调用 logging.basicConfig）。
_LOGGER = logging.getLogger(__name__)

#: 等待目标进程退出的最长轮数；每轮约 2 秒，默认上限约 2 分钟。
#: 超时后仍然继续走复制流程——复制本身还有重试，实在不行会留下暂存包
#: 让用户手动替换，而不会永远卡在这里。
_MAX_WAIT_ROUNDS: int = 60

#: 版本号里允许出现在文件名中的字符；其余字符一律替换成 "-"。
#: Windows 文件名不允许 ``:`` ``\`` ``/`` 等字符，而版本号来自服务端清单。
_UNSAFE_VERSION_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def current_executable_path() -> Optional[Path]:
    """返回当前运行的可执行文件路径。

    仅在 PyInstaller 打包（``sys.frozen``）环境下有值，否则返回 ``None``。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


def is_self_update_supported() -> bool:
    """判断当前环境是否支持自更新（打包后的 Windows 构建）。"""
    return bool(getattr(sys, "frozen", False)) and os.name == "nt"


def _quote_path(path: Path | str) -> str:
    """把路径转成可以安全写进批处理脚本的带引号形式。"""
    text = str(path)
    if '"' in text:
        raise ValueError(f"路径不允许包含双引号：{text!r}")
    return f'"{text}"'


def _batch_encoding() -> str:
    """返回写出批处理脚本应使用的代码页。

    cmd.exe 按 **OEM** 代码页解析批处理文件（简体中文系统是 936），
    而 Python 的 ``mbcs`` 是 ANSI 代码页：两者在部分系统上并不一致，
    含中文的路径会被解析成乱码导致替换失败。这里取真正的 OEM 代码页。
    """
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes

        oem_code_page = int(ctypes.windll.kernel32.GetOEMCP())
    except Exception:  # pragma: no cover - 极少见的系统异常
        _LOGGER.warning("读取 OEM 代码页失败，回退到 mbcs", exc_info=True)
        return "mbcs"
    if oem_code_page <= 0:
        return "mbcs"
    return f"cp{oem_code_page}"


def build_update_script(
    new_exe: Path,
    target_exe: Path,
    pid: int,
    restart: bool = True,
    extra_pids: Sequence[int] = (),
) -> str:
    """生成替换主程序的批处理脚本文本。

    脚本依次执行：等待 ``pid``（以及 ``extra_pids``）对应的进程退出 →
    ``copy /Y`` 覆盖目标 exe（文件仍被占用时重试）→（可选）重新启动目标 exe →
    删除暂存安装包与脚本自身。

    ``extra_pids`` 用来一并等待 PyInstaller onefile 的引导进程：应用子进程退出
    后，引导进程还会继续持有 exe 文件句柄，此时直接覆盖会失败。

    路径中如果含有双引号会抛出 ``ValueError``（无法安全转义）。
    """
    try:
        pid_value = int(pid)
    except (TypeError, ValueError) as error:
        raise ValueError(f"pid 必须是整数：{pid!r}") from error
    if pid_value < 0:
        raise ValueError(f"pid 必须是非负整数：{pid!r}")

    new_quoted = _quote_path(new_exe)
    target_quoted = _quote_path(target_exe)

    wait_pids: list[int] = [pid_value]
    for extra in extra_pids:
        try:
            extra_value = int(extra)
        except (TypeError, ValueError) as error:
            raise ValueError(f"pid 必须是整数：{extra!r}") from error
        if extra_value > 0 and extra_value not in wait_pids:
            wait_pids.append(extra_value)

    # 脚本文本保持纯 ASCII：cmd.exe 按系统 ANSI 代码页解析，
    # 混排 UTF-8 中文注释在部分机器上会乱码甚至破坏命令解析。
    lines = [
        "@echo off",
        "setlocal",
        "rem Wait for the running application to exit.",
        "set /a WAIT_ROUNDS=0",
        ":WAIT_FOR_EXIT",
        "set /a WAIT_ROUNDS+=1",
        f"if %WAIT_ROUNDS% GEQ {_MAX_WAIT_ROUNDS} goto WAIT_DONE",
    ]
    for wait_pid in wait_pids:
        lines.extend(
            [
                f'tasklist /FI "PID eq {wait_pid}" | findstr /C:"{wait_pid}" >nul',
                "if not errorlevel 1 (",
                "    ping -n 2 127.0.0.1 >nul",
                "    goto WAIT_FOR_EXIT",
                ")",
            ]
        )
    # 等待必须有上限：进程万一卡住不退出，不能永远停在这里。
    # 超时后照样进入复制流程，由复制重试兜底。
    lines.extend(
        [
            "rem All watched processes have exited.",
            ":WAIT_DONE",
        ]
    )

    lines.extend(
        [
            "rem Replace the application executable, retrying while it is locked.",
            "set /a ATTEMPT=0",
            ":COPY_RETRY",
            f"copy /Y {new_quoted} {target_quoted}",
            "if not errorlevel 1 goto COPY_DONE",
            "set /a ATTEMPT+=1",
            "if %ATTEMPT% GEQ 10 (",
            "    rem Copy failed: keep the staged installer for a manual retry.",
            "    exit /b 1",
            ")",
            "ping -n 2 127.0.0.1 >nul",
            "goto COPY_RETRY",
            ":COPY_DONE",
        ]
    )

    if restart:
        lines.append("rem Relaunch the updated application.")
        lines.append(f"start \"\" {target_quoted}")

    lines.extend(
        [
            "rem Clean up the staged installer and this script.",
            f"del /F /Q {new_quoted}",
            'del /F /Q "%~f0"',
            "exit /b 0",
        ]
    )
    return "\r\n".join(lines) + "\r\n"


def schedule_install(
    new_exe: Path,
    target_exe: Path,
    pid: int | None = None,
    restart: bool = True,
) -> bool:
    """把更新脚本写进临时文件并以后台进程启动。

    成功返回 ``True``；任何失败都只记录日志并返回 ``False``，绝不抛出异常。
    """
    try:
        if pid is None:
            pid = os.getpid()

        # PyInstaller onefile 模式下，引导进程仍然持有 exe 文件句柄；
        # 必须等它一起退出，覆盖目标文件才不会报“拒绝访问”。
        extra_pids: list[int] = []
        if getattr(sys, "frozen", False):
            parent_pid = os.getppid()
            if parent_pid > 0:
                extra_pids.append(parent_pid)

        script_text = build_update_script(
            new_exe,
            target_exe,
            pid,
            restart=restart,
            extra_pids=extra_pids,
        )

        handle, script_path = tempfile.mkstemp(prefix="gomoku_update_", suffix=".bat")
        # 路径可能包含中文：按 cmd.exe 的 OEM 代码页写出，保证路径被
        # 正确解析（详见 _batch_encoding 的说明）。
        encoding = _batch_encoding()
        with os.fdopen(handle, "w", encoding=encoding, newline="") as stream:
            stream.write(script_text)

        if os.name == "nt":
            # 用 ShellExecute 启动批处理：它像在资源管理器里双击一样处理
            # 含空格的路径，不会踩到 cmd /c 的引号解析规则。
            # show_cmd 必须用关键字传：3.15 调整了位置参数的顺序。
            os.startfile(script_path, "open", show_cmd=0)  # type: ignore[attr-defined]
            return True

        # 非 Windows 平台（例如开发环境）只作为兜底路径。
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        subprocess.Popen(
            ["cmd", "/c", script_path],
            creationflags=creation_flags,
            close_fds=True,
        )
        return True
    except Exception:  # noqa: BLE001 - 退出流程中绝不能因安装失败而崩溃
        _LOGGER.exception("安排自更新脚本执行失败")
        return False


def staged_download_path(directory: Path, version: str) -> Path:
    """返回暂存安装包的路径，并确保父目录存在。

    版本号来自服务端清单，必须先净化再拼进文件名：Windows 的文件名
    不允许 ``:`` ``\\`` ``/`` 等字符，直接拼接会让下载以"无法创建文件"
    失败，而用户看到的只是一句笼统的提示。
    """
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_version = _UNSAFE_VERSION_CHARS.sub("-", str(version)).strip(".-_")
    if not safe_version:
        safe_version = "unknown"
    return target_dir / f"Gomoku-{safe_version}-setup.exe"
