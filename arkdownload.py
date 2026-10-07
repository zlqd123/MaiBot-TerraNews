"""语料下载与落盘。

语料来自 ``ArknightsSearch/ArknightsSearch-resource``——那是源项目 CI 产出的**构建
成品**，与本地 ArkSearch ``data/story`` 逐字节一致（8 个 JSON，47MB）。下载走
``codeload.github.com`` 的 zip 快照，一��� 13.6MB，解压即得全部数据。

刻意不碰 GitHub 的 contents API（匿名配额 60 次/小时，还容易撞限流）：
版本探测只读 Atom feed 的最新 commit SHA，几 KB 就够。

所有网络操作都是**阻塞的同步函数**，由调用方放进 ``asyncio.to_thread``，不占事件循环。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from arkdata import REQUIRED_FILES

__all__ = [
    "detect_proxy",
    "fetch_latest_commit",
    "download_story",
    "read_manifest",
    "write_manifest",
    "is_valid_json",
    "DEFAULT_REPO",
    "DEFAULT_BRANCH",
]

DEFAULT_REPO = "ArknightsSearch/ArknightsSearch-resource"
DEFAULT_BRANCH = "main"

# 常见本地代理端口，挨个探一遍
COMMON_PROXY_PORTS = (7890, 7891, 7897, 7899, 1080, 10808, 10809, 10810, 10811, 8888, 4780, 33210)

_MISSING = object()

# 连通性探测的目标。只用 github.com——codeload 和 Atom feed 都在同一 CDN 下。
_PROBE_URL = "https://github.com/robots.txt"


# --------------------------------------------------------------------------- #
# 代理
# --------------------------------------------------------------------------- #
def detect_proxy(explicit: str = "auto") -> Optional[str]:
    """探测可用的 HTTP 代理。

    顺序：**先试直连**，直连能用就返回 ``None``；只有直连不通时才依次试环境变量、
    Windows 注册表和常见端口。把这步放在最前面很重要——候选代理有十几个，
    挨个探一遍要十几秒，而大多数机器压根不需要代理。

    Args:
        explicit: ``"auto"`` 自动探测，``"none"`` 不用代理，其他值当代理地址。

    Returns:
        代理地址；直连可用时返回 ``None``。
    """
    if explicit == "none":
        return None
    if explicit and explicit != "auto":
        return explicit

    if _reachable("direct"):
        return None

    candidates: List[str] = []
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key)
        if value:
            candidates.append(value.strip())
    candidates.extend(_registry_proxies())
    candidates.extend(f"http://127.0.0.1:{port}" for port in COMMON_PROXY_PORTS)

    for proxy in candidates:
        if proxy and _reachable(proxy):
            return proxy
    return None


def _registry_proxies() -> List[str]:
    """从 Windows 注册表读系统代理设置。"""
    out: List[str] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return out
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            value, _ = winreg.QueryValueEx(key, "ProxyServer")
            if value:
                out.extend(part.strip() for part in str(value).split(";") if part.strip())
    except OSError:
        pass
    return out


def _reachable(proxy: str, timeout: float = 1.5) -> bool:
    """验证这条链路真的能用——配置里配着但没开进程的代理很常见。

    探的是 ``github.com`` 而不是 ``api.github.com``：国内网络对这两个域名的可达性
    经常不一致（有的机器前者通后者不通），而我们实际只用到前者。探错目标会白等
    一个超时，还得出「直连不通」的错误结论。

    Args:
        proxy: 代理地址；传 ``"direct"`` 表示验证直连。
        timeout: 超时秒数。

    Returns:
        是否可用。
    """
    handler = urllib.request.ProxyHandler({}) if proxy == "direct" else urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    try:
        opener = urllib.request.build_opener(handler)
        request = urllib.request.Request(_PROBE_URL, method="HEAD")
        with opener.open(request, timeout=timeout) as response:
            return response.status < 500
    except Exception:
        return False


def _build_opener(proxy: Optional[str]) -> urllib.request.OpenerDirector:
    """按代理构造 opener。"""
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- #
# 版本探测
# --------------------------------------------------------------------------- #
_COMMIT_RE = re.compile(r"/commit/([0-9a-f]{40})")


def fetch_latest_commit(repo: str = DEFAULT_REPO, branch: str = DEFAULT_BRANCH, proxy: Optional[str] = None, timeout: float = 10.0) -> Optional[str]:
    """读 Atom feed 拿最新 commit SHA。

    比 GitHub API 便宜得多——feed 是静态 XML，不吃 60 次/小时的匿名配额。

    Args:
        repo: ``owner/name`` 形式。
        branch: 分支名。
        proxy: 代理地址；``None`` 表示直连。
        timeout: 超时秒数。

    Returns:
        40 位 commit SHA；失败返回 ``None``（网络问题必须静默，不能当错误）。
    """
    url = f"https://github.com/{repo}/commits/{branch}.atom"
    try:
        with _build_opener(proxy).open(url, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None
    match = _COMMIT_RE.search(body)
    return match.group(1) if match else None


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download_story(
    target_dir: str | Path,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    proxy: Optional[str] = None,
    timeout: float = 120.0,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> Dict[str, object]:
    """下载语料并解压到目标目录。

    采用「先下到临时目录、校验通过再切换」的方式，中途失败不会把已有的可用数据
    搞坏。

    Args:
        target_dir: 数据落地目录。
        repo: 语料仓库。
        branch: 分支。
        proxy: 代理地址；``None`` 直连。
        timeout: 下载超时秒数。
        on_progress: 进度回调 ``(已读字节, 总字节)``，总字节未知时为 -1。

    Returns:
        ``{"ok": bool, "message": str, "files": [...], "bytes": int}``。
    """
    target = Path(target_dir)
    staging = target.with_name(target.name + ".staging")
    url = f"https://codeload.github.com/{repo}/zip/refs/heads/{branch}"

    try:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)

        zip_path = staging / "_download.zip"
        with _build_opener(proxy).open(url, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or -1)
            done = 0
            with open(zip_path, "wb") as fh:
                while True:
                    chunk = response.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)

        if zip_path.stat().st_size < 1_000_000:
            return {"ok": False, "message": "下载内容异常，可能被代理拦截", "files": [], "bytes": 0}

        extracted = _extract_zip(zip_path, staging)
        zip_path.unlink(missing_ok=True)

        files = sorted(p.name for p in extracted.iterdir() if p.is_file())
        absent = [name for name in REQUIRED_FILES if not (extracted / name).is_file()]
        if absent:
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "message": f"仓库里没有这些数据文件：{'、'.join(absent)}", "files": [], "bytes": 0}
        bad = [name for name in files if name.endswith(".json") and not is_valid_json(extracted / name)]
        if bad:
            shutil.rmtree(staging, ignore_errors=True)
            return {"ok": False, "message": "以下文件不是合法 JSON：" + "、".join(bad), "files": [], "bytes": 0}

        # 数据齐了才切换目录
        backup = target.with_name(target.name + ".bak")
        shutil.rmtree(backup, ignore_errors=True)
        if target.exists():
            target.rename(backup)
        extracted.rename(target)
        shutil.rmtree(backup, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)

        size = sum((target / name).stat().st_size for name in files)
        return {"ok": True, "message": f"已更新 {len(files)} 个文件", "files": files, "bytes": size}
    except (urllib.error.URLError, OSError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return {"ok": False, "message": f"下载失败：{exc}", "files": [], "bytes": 0}
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return {"ok": False, "message": f"更新失败：{exc}", "files": [], "bytes": 0}


def _extract_zip(zip_path: Path, staging: Path) -> Path:
    """解压 zip 并定位语料目录。

    GitHub 的 zip 会套一层 ``<repo>-<branch>/``，语料本身可能还在更深一层
    （本项目实测是 ``data/story/``）。所以这里不假设层级，而是找出**含有
    ``story_data.json`` 的最浅目录**——那才是数据所在。

    Args:
        zip_path: zip 文件路径。
        staging: 解压目标目录。

    Returns:
        语料目录；找不到时退回解压根目录，交由调用方校验报错。
    """
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)

    marker = "story_data.json"
    best: Optional[Path] = None
    best_depth = -1
    for path in staging.rglob(marker):
        if not path.is_file():
            continue
        depth = len(path.relative_to(staging).parts)
        if best is None or depth < best_depth:
            best, best_depth = path.parent, depth
    return best if best is not None else staging


# --------------------------------------------------------------------------- #
# 校验与元数据
# --------------------------------------------------------------------------- #
def is_valid_json(path: str | Path) -> bool:
    """确认文件是能解析的 JSON——防止把 HTML 错误页当数据存下来。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            json.load(fh)
        return True
    except (OSError, ValueError):
        return False


def read_manifest(target_dir: str | Path) -> Dict[str, object]:
    """读取本地数据清单。

    Args:
        target_dir: 数据目录。

    Returns:
        清单字典；不存在时返回 ``{}``。
    """
    path = Path(target_dir) / "MANIFEST.json"
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_manifest(target_dir: str | Path, commit: str, files: List[str]) -> None:
    """记录本地数据对应的 commit，供下次比对。"""
    path = Path(target_dir) / "MANIFEST.json"
    payload = {"commit": commit, "files": files, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def plan_update(target_dir: str | Path, repo: str, branch: str, proxy: Optional[str], timeout: float) -> Tuple[str, str]:
    """判断是否需要更新。

    Args:
        target_dir: 数据目录。
        repo: 语料仓库。
        branch: 分支。
        proxy: 代理地址。
        timeout: 网络超时。

    Returns:
        ``(状态, 说明)``，状态取值 ``current`` / ``stale`` / ``unknown``。
        ``unknown`` 表示网络不通或本地还没数据——都应静默跳过。
    """
    manifest = read_manifest(target_dir)
    local = str(manifest.get("commit") or "")
    if not local:
        return "unknown", "本地还没有数据"
    remote = fetch_latest_commit(repo, branch, proxy, timeout)
    if not remote:
        return "unknown", "连不上语料仓库"
    if remote == local:
        return "current", f"已是最新（{local[:8]}）"
    return "stale", f"有新版本 {local[:8]} → {remote[:8]}"
