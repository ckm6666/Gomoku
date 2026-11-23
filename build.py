"""客户端打包脚本。

依据 README「打包成 exe」一节，等价于执行：

    pyinstaller --noconfirm --clean Gomoku.spec

产物为 dist/Gomoku.exe（onefile + windowed）。

用法：
    python build.py            # 正常打包
    python build.py --fast     # 跳过 --clean（复用上次的缓存）
    python build.py --install  # 先安装 requirements-dev.txt 依赖再打包

固化内置配置（打包前写入 build/build_info.json，运行时自动生效）：
    --server-url URL         固化联机/接口服务地址（http/https）
    --update-url URL         固化更新清单地址（优先级高于按服务地址推导）
    --auto-update-check      固化为「启动时自动检查更新」
    --no-auto-update-check   固化为「启动时不自动检查更新」
    两个开关都不给时不写入该项，运行时沿用默认行为（开启）。

示例：
    python build.py --server-url https://s.example
    python build.py --server-url https://s.example \\
        --update-url https://s.example/api/v1/updates/latest \\
        --no-auto-update-check

附加参数会原样透传给 PyInstaller，例如：
    python build.py -- --windowed --debug
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "Gomoku.spec"
APP_NAME = "Gomoku"

#: 打包前写入、随 exe 一起分发的内置配置文件。
BUILD_INFO_PATH = ROOT / "build" / "build_info.json"


class _ArgumentError(Exception):
    """命令行参数不合法。"""


class _Parser(argparse.ArgumentParser):
    """把 argparse 的英文报错转成异常，便于输出中文提示。"""

    def error(self, message: str) -> NoReturn:
        raise _ArgumentError(message)


def _run(cmd: list[str], **kwargs) -> int:
    print(f"$ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(ROOT), **kwargs)


def _ensure_pyinstaller() -> bool:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "[!] 未检测到 PyInstaller，请先安装构建依赖：\n"
            "    pip install -r requirements-dev.txt",
            file=sys.stderr,
        )
        return False
    return True


def _install_deps() -> int:
    req = ROOT / "requirements-dev.txt"
    if not req.exists():
        print(f"[!] 缺少依赖清单：{req}", file=sys.stderr)
        return 1
    return _run([sys.executable, "-m", "pip", "install", "-r", str(req)])


def _build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = _Parser(
        prog="build.py",
        description="打包 Gomoku 客户端，并可选地把服务地址固化进 exe。",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="跳过 --clean，复用上次的构建缓存",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="打包前先安装 requirements-dev.txt 依赖",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        help="固化的联机/接口服务地址（http/https）",
    )
    parser.add_argument(
        "--update-url",
        default=None,
        help="固化的更新清单地址（http/https），优先级高于服务地址推导",
    )
    parser.add_argument(
        "--auto-update-check",
        dest="auto_update_check",
        action="store_true",
        default=None,
        help="固化为「启动时自动检查更新」",
    )
    parser.add_argument(
        "--no-auto-update-check",
        dest="auto_update_check",
        action="store_false",
        help="固化为「启动时不自动检查更新」",
    )
    return parser


def _split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """把 ``--`` 之后的参数拆出来，原样透传给 PyInstaller。"""
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1 :]
    return argv, []


def _is_http_url(value: str) -> bool:
    """判断是否是合法的 http/https URL。"""
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _collect_build_info(
    server_url: str, update_url: str, auto_update_check: Optional[bool]
) -> dict:
    """整理要固化的内置配置；只包含显式提供的键。"""
    payload: dict = {}
    if server_url:
        payload["api_base_url"] = server_url.rstrip("/")
    if update_url:
        payload["update_manifest_url"] = update_url.rstrip("/")
    if auto_update_check is not None:
        payload["auto_update_check"] = bool(auto_update_check)
    return payload


def _write_build_info(payload: dict) -> None:
    """写入 build/build_info.json（UTF-8，无 BOM）。"""
    BUILD_INFO_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with open(BUILD_INFO_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _summary_text(payload: dict) -> str:
    """生成中文的固化配置摘要。"""
    if not payload:
        return "已固化内置配置：无（运行时仅可用环境变量）"
    parts = []
    if "api_base_url" in payload:
        parts.append(f"服务地址={payload['api_base_url']}")
    if "update_manifest_url" in payload:
        parts.append(f"更新清单={payload['update_manifest_url']}")
    if "auto_update_check" in payload:
        state = "开启" if payload["auto_update_check"] else "关闭"
        parts.append(f"启动自动检查={state}")
    return "已固化内置配置：" + " ".join(parts)


def main(argv: list[str]) -> int:
    head, extra = _split_passthrough(list(argv))
    parser = _build_parser()
    try:
        args = parser.parse_args(head)
    except _ArgumentError as exc:
        print(f"[!] 参数错误：{exc}", file=sys.stderr)
        parser.print_help(sys.stderr)
        return 2

    if not SPEC.exists():
        print(f"[!] 缺少 spec 文件：{SPEC}", file=sys.stderr)
        return 1

    server_url = (args.server_url or "").strip()
    update_url = (args.update_url or "").strip()
    for flag, value in (
        ("--server-url", server_url),
        ("--update-url", update_url),
    ):
        if value and not _is_http_url(value):
            print(
                f"[!] {flag} 必须是合法的 http/https URL：{value}",
                file=sys.stderr,
            )
            return 2

    if args.install:
        code = _install_deps()
        if code != 0:
            return code

    if not _ensure_pyinstaller():
        return 1

    payload = _collect_build_info(
        server_url, update_url, args.auto_update_check
    )
    try:
        _write_build_info(payload)
    except OSError as exc:
        print(f"[!] 写入内置配置失败：{exc}", file=sys.stderr)
        return 1

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm"]
    if not args.fast:
        cmd.append("--clean")
    cmd.extend(extra)
    cmd.append(SPEC.name)

    code = _run(cmd)
    if code != 0:
        print("[!] 打包失败，请查看上方 PyInstaller 日志。", file=sys.stderr)
        return code

    exe = ROOT / "dist" / f"{APP_NAME}.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"[OK] 打包完成：{exe}（约 {size_mb:.1f} MB）")
        print(
            "     分发时记得把 gomoku/client/assets/THIRD_PARTY_NOTICES.md "
            "与字体目录中的 OFL 文本一并放到 exe 旁边。"
        )
    else:
        print(f"[OK] 打包结束，产物应位于 {ROOT / 'dist'}")
    print(_summary_text(payload))
    return 0


if __name__ == "__main__":
    # 避免打包过程中被工作目录影响
    os.chdir(ROOT)
    raise SystemExit(main(sys.argv[1:]))
