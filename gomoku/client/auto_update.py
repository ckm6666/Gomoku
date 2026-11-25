"""自动更新：下载、解压并替换客户端的安装目录。

本模块刻意只依赖标准库（不含 Qt），这样解压校验、下载地址白名单、
替换脚本生成这些关键逻辑都能被单元测试直接覆盖。

替换流程
--------
1. 客户端把发布产物（zip）下载到临时目录（见
   :mod:`gomoku.client.net.update_download`）；
2. :func:`extract_archive` 校验并解压到暂存目录；
3. :func:`write_helper_script` 生成一个 PowerShell 辅助脚本；
4. :func:`launch_helper` 以脱离主进程的方式启动该脚本，客户端随即退出；
5. 辅助脚本等待客户端进程结束，把新版本文件替换进安装目录，再重新
   启动客户端。

之所以要一个外部脚本：Windows 不允许覆盖正在运行的 exe，替换只能在
客户端退出之后进行，而退出之后原进程已经不能再做这件事。

安全约束
--------
* 只接受 ``github.com`` 上本仓库的发布产物地址；
* 解压时逐个成员校验路径，拒绝绝对路径与 ``..``，杜绝压缩包里的
  路径穿越把文件写到暂存目录之外；
* 辅助脚本只把暂存目录里的文件复制到安装目录，不执行任何解压包里的
  可执行内容。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional
from urllib.parse import urlsplit

from gomoku.client.frozen import is_frozen

#: 发布产物里的主程序文件名。
APP_EXE_NAME: Final[str] = "Gomoku.exe"

#: 本仓库发布产物的下载路径前缀（``browser_download_url`` 的路径部分）。
RELEASE_DOWNLOAD_PREFIX: Final[str] = "/ckm6666/Gomoku/releases/download/"

#: 允许下载产物的域名。
#:
#: 只放行 ``github.com``：发布产物的地址本来就来自 github.com，
#: ``github.com → objects.githubusercontent.com`` 的 302 由 Qt 内部跟随，
#: 不需要把后者加进白名单——加进来反而会让「只下载本仓库产物」这条
#: 保证出现缺口（该域名下的任何路径都会被放行）。
_ALLOWED_DOWNLOAD_HOSTS: Final[frozenset[str]] = frozenset({"github.com"})

#: 允许的端口：只放行 HTTPS 的默认端口。
_ALLOWED_DOWNLOAD_PORTS: Final[frozenset[int]] = frozenset({443})

#: 临时工作目录名前缀。
WORKSPACE_PREFIX: Final[str] = "Gomoku-update-"

#: PyInstaller onefile 用来传递解包目录的环境变量前缀。
#:
#: 打包产物的子进程带着这些变量运行（``_PYI_PARENT_PROCESS_LEVEL`` 表示
#: 「我是一次解包的子进程，目录在 ``_PYI_APPLICATION_HOME_DIR``」）。如果
#: 辅助脚本原样继承它们，新启动的客户端会误以为自己也是子进程，于是跳过
#: 解包、直接去找上一个实例留下的、早已被删除的临时目录，进程会永远卡住。
#: 所以启动辅助脚本时必须把这些变量清掉。
_PYINSTALLER_ENV_PREFIX: Final[str] = "_PYI_"

#: PyInstaller 5.x 使用的旧变量名，一并清掉。
_PYINSTALLER_LEGACY_ENV: Final[tuple[str, ...]] = ("_MEIPASS2",)

#: Windows 上让辅助脚本不弹出控制台窗口所用的创建标志。
_CREATE_NO_WINDOW: Final[int] = 0x08000000
_CREATE_NEW_PROCESS_GROUP: Final[int] = 0x00000200


class UpdateError(Exception):
    """更新流程中的可展示错误。"""


# ---------------------------------------------------------------------------
# 运行环境
# ---------------------------------------------------------------------------
def install_directory() -> Optional[Path]:
    """当前客户端的安装目录。

    只有打包产物才谈得上「原地替换」：源码运行时替换的是工作目录里的
    源码，那不是用户装的客户端，因此返回 ``None``。
    """
    if not is_frozen():
        return None
    try:
        return Path(sys.executable).resolve().parent
    except OSError:  # pragma: no cover - 极端环境下 sys.executable 不可用
        return None


def is_supported() -> bool:
    """当前环境是否支持「下载并原地替换」。

    只有 Windows 上的打包产物支持；其余情况（源码运行、其他平台）
    由界面层退回到「打开发布页手动下载」。
    """
    if os.name != "nt":
        return False
    return install_directory() is not None


# ---------------------------------------------------------------------------
# 地址校验
# ---------------------------------------------------------------------------
def asset_download_url(url: str) -> str:
    """校验发布产物的下载地址；不合法时返回空串。

    地址来自 GitHub API 的响应，理论上可被劫持改写，因此协议、域名、
    端口与路径前缀都要校验：只有本仓库在 ``github.com`` 上的发布
    产物才会被放行，避免被诱导到任意外部站点。
    """
    candidate = (url or "").strip()
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
        # ``port`` 在端口非法时抛 ValueError。
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https":
        return ""
    if port is not None and port not in _ALLOWED_DOWNLOAD_PORTS:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_DOWNLOAD_HOSTS:
        return ""
    if not parsed.path.startswith(RELEASE_DOWNLOAD_PREFIX):
        return ""
    if not parsed.path.endswith(".zip"):
        return ""
    return candidate


# ---------------------------------------------------------------------------
# 解压
# ---------------------------------------------------------------------------
def _safe_relative_path(name: str) -> Optional[str]:
    """把压缩包成员名转成安全的相对路径；不安全时返回 ``None``。

    拒绝绝对路径、盘符、``..`` 以及空名字：这些都可能是「压缩包路径
    穿越」，解压时会把文件写到暂存目录之外。
    """
    raw = (name or "").replace("\\", "/").strip()
    if not raw:
        return None
    if raw.startswith("/"):
        return None
    if len(raw) > 1 and raw[1] == ":":
        # 形如 ``C:/...`` 的盘符路径。
        return None
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    if not parts:
        return None
    return "/".join(parts)


def extract_archive(archive: Path, staging: Path) -> Path:
    """把更新包解压到暂存目录，并校验其中含有主程序。

    返回暂存目录路径；内容异常时抛 :class:`UpdateError`。
    """
    if not archive.is_file():
        raise UpdateError("更新包不存在，请重新下载")
    staging.mkdir(parents=True, exist_ok=True)
    root = staging.resolve()
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members:
                raise UpdateError("更新包是空的，请重新下载")
            for info in members:
                relative = _safe_relative_path(info.filename)
                if relative is None:
                    raise UpdateError("更新包内容异常，已终止安装")
                destination = (root / relative).resolve()
                if root != destination and root not in destination.parents:
                    raise UpdateError("更新包内容异常，已终止安装")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source:
                    with open(destination, "wb") as target:
                        shutil.copyfileobj(source, target)
    except zipfile.BadZipFile:
        raise UpdateError("更新包已损坏，请重新下载") from None

    exe = staging / APP_EXE_NAME
    if not exe.is_file():
        raise UpdateError("更新包中缺少主程序，已终止安装")
    try:
        with open(exe, "rb") as handle:
            if handle.read(2) != b"MZ":
                raise UpdateError("更新包中的主程序无效，已终止安装")
    except OSError:
        raise UpdateError("无法读取更新包，已终止安装") from None
    return staging


# ---------------------------------------------------------------------------
# 替换
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class UpdatePlan:
    """一次「替换并重启」需要的全部路径。"""

    staging_dir: Path
    install_dir: Path
    exe_path: Path
    script_path: Path
    log_path: Path


#: 辅助脚本：等待客户端退出 → 逐个文件替换 → 重新启动客户端。
_HELPER_SCRIPT: Final[str] = """\
param(
    [string]$Stage,
    [string]$Target,
    [string]$Exe,
    [string]$Log,
    [string]$Workspace,
    [int]$WaitPid
)

$ErrorActionPreference = 'Stop'

function Write-UpdateLog {
    param([string]$Message)
    try {
        Add-Content -LiteralPath $Log -Encoding UTF8 -Value (
            "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        )
    } catch { }
}

function Show-UpdateError {
    param([string]$Message)
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        [System.Windows.Forms.MessageBox]::Show(
            $Message, '五子棋 更新失败'
        ) | Out-Null
    } catch { }
}

try {
    Write-UpdateLog '等待客户端退出'
    if ($WaitPid -gt 0) {
        # 不直接用 Wait-Process：它没有超时参数，客户端万一退不出来
        # 就会永远等下去，用户只能看着一个关掉的窗口干等。这里改成
        # 自己轮询，并给一个上限。
        $deadline = (Get-Date).AddSeconds(120)
        while (Get-Process -Id $WaitPid -ErrorAction SilentlyContinue) {
            if ((Get-Date) -gt $deadline) {
                throw '等待客户端退出超时，请重新启动客户端后再试'
            }
            Start-Sleep -Milliseconds 500
        }
    }

    $files = Get-ChildItem -LiteralPath $Stage -Recurse -File -Force
    if (-not $files) {
        throw '更新包内容为空'
    }

    Write-UpdateLog ("正在替换 {0} 个文件" -f $files.Count)
    $lastError = $null
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        try {
            foreach ($file in $files) {
                $relative = $file.FullName.Substring($Stage.Length).TrimStart('\\', '/')
                $destination = Join-Path $Target $relative
                $destinationDir = Split-Path -Parent $destination
                if (-not (Test-Path -LiteralPath $destinationDir)) {
                    New-Item -ItemType Directory -Path $destinationDir -Force | Out-Null
                }
                # 先写临时文件再改名：中途失败不会破坏正在使用的旧文件。
                $temp = $destination + '.new'
                Copy-Item -LiteralPath $file.FullName -Destination $temp -Force
                Move-Item -LiteralPath $temp -Destination $destination -Force
            }
            $lastError = $null
            break
        } catch {
            $lastError = $_
            Write-UpdateLog ("第 {0} 次替换失败：{1}" -f $attempt, $_.Exception.Message)
            Start-Sleep -Seconds 1
        }
    }
    if ($lastError) {
        throw $lastError
    }

    Write-UpdateLog '替换完成，正在启动新版本'
    Remove-Item -LiteralPath $Stage -Recurse -Force -ErrorAction SilentlyContinue
    # 清掉 PyInstaller 的解包目录变量：新客户端必须自己重新解包，否则会
    # 去找上一个实例留下的、已经被删除的临时目录，进程会卡在启动阶段。
    foreach ($name in @(
        '_PYI_PARENT_PROCESS_LEVEL',
        '_PYI_APPLICATION_HOME_DIR',
        '_PYI_ARCHIVE_FILE',
        '_MEIPASS2'
    )) {
        Remove-Item -LiteralPath ("Env:\\" + $name) -ErrorAction SilentlyContinue
    }
    Start-Process -FilePath $Exe
    # 清掉整个工作目录（含本脚本与日志）：临时目录留着会越攒越多。
    # 脚本自身可能被占用，删不掉也无妨——下一次更新会另建目录。
    if ($Workspace) {
        Remove-Item -LiteralPath $Workspace -Recurse -Force -ErrorAction SilentlyContinue
    }
    exit 0
} catch {
    $message = $_.Exception.Message
    Write-UpdateLog ("更新失败：{0}" -f $message)
    Show-UpdateError ("更新失败：{0}`n日志：{1}" -f $message, $Log)
    exit 1
}
"""


def create_workspace(version: str = "") -> Path:
    """在临时目录里建一个本次更新专用的工作目录。

    目录名带随机后缀：同一进程里可能先后下载多个版本，每次都要有独立
    的暂存目录，避免上一次的残留文件混进安装包。
    """
    safe_version = "".join(
        char for char in (version or "") if char.isalnum() or char in ".-_"
    )
    prefix = f"{WORKSPACE_PREFIX}{safe_version}-" if safe_version else WORKSPACE_PREFIX
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup_workspace(workspace: Optional[Path]) -> None:
    """删除工作目录；失败不影响主流程。"""
    if workspace is None:
        return
    try:
        shutil.rmtree(workspace, ignore_errors=True)
    except OSError:  # pragma: no cover - 清理失败无需打扰用户
        pass


def prepare_update(staging: Path, workspace: Optional[Path] = None) -> UpdatePlan:
    """生成替换所需的脚本与路径；环境不支持时抛 :class:`UpdateError`。"""
    target = install_directory()
    if target is None:
        raise UpdateError("当前环境不支持自动替换，请手动下载新版本")
    if not staging.is_dir():
        raise UpdateError("更新包已丢失，请重新下载")

    root = workspace if workspace is not None else staging.parent
    root.mkdir(parents=True, exist_ok=True)
    plan = UpdatePlan(
        staging_dir=staging,
        install_dir=target,
        exe_path=target / APP_EXE_NAME,
        script_path=root / "apply-update.ps1",
        log_path=root / "apply-update.log",
    )
    write_helper_script(plan)
    return plan


def write_helper_script(plan: UpdatePlan) -> Path:
    """把辅助脚本写到磁盘并返回其路径。"""
    # 用 UTF-8 带 BOM 写出：Windows PowerShell 5.1 只有带 BOM 才会把
    # 脚本按 UTF-8 解析。
    plan.script_path.write_text(_HELPER_SCRIPT, encoding="utf-8-sig")
    return plan.script_path


def launch_helper(plan: UpdatePlan) -> int:
    """以脱离主进程的方式启动辅助脚本，返回其进程号。

    脚本会等待当前进程退出后再替换文件，因此必须在客户端退出之前
    启动它。
    """
    command = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-File",
        str(plan.script_path),
        "-Stage",
        str(plan.staging_dir),
        "-Target",
        str(plan.install_dir),
        "-Exe",
        str(plan.exe_path),
        "-Log",
        str(plan.log_path),
        "-Workspace",
        str(plan.script_path.parent),
        "-WaitPid",
        str(os.getpid()),
    ]
    creation_flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(  # noqa: S603 - 固定参数，不经过 shell
        command,
        creationflags=creation_flags,
        close_fds=True,
        # 清掉 PyInstaller 的解包目录变量：辅助脚本拉起的新客户端必须
        # 自己重新解包，否则会卡在已经失效的旧临时目录上（见上面的说明）。
        env=_clean_environment(),
        # 客户端是窗口程序，没有可用的控制台；子进程的输出直接丢弃，
        # 免得脚本里的报错写到无效句柄上。
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process.pid


def _clean_environment() -> dict[str, str]:
    """返回一份去掉 PyInstaller 解包变量的环境变量副本。"""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_PYINSTALLER_ENV_PREFIX)
        and key not in _PYINSTALLER_LEGACY_ENV
    }


__all__ = [
    "APP_EXE_NAME",
    "RELEASE_DOWNLOAD_PREFIX",
    "WORKSPACE_PREFIX",
    "UpdateError",
    "UpdatePlan",
    "asset_download_url",
    "cleanup_workspace",
    "create_workspace",
    "extract_archive",
    "install_directory",
    "is_supported",
    "launch_helper",
    "prepare_update",
    "write_helper_script",
]
