"""一键构建发布物（Electron 版）。

产出（都在 dist/ 下）：
    NetSwitcher-<ver>-Setup.exe       安装版（NSIS）
    NetSwitcher-<ver>-Portable.zip    免安装绿色版
    NetSwitcher-<ver>-Source.zip      源码

分工：
    内核  PyInstaller 打成控制台程序（Electron 要读它 stdout 里的 ready JSON）
    界面  Electron + electron-builder，内核作为 extraResources 塞进 resources/backend

准备：`cd frontend && npm install`（下载 Electron 与 electron-builder，只需一次）。
在构建用的独立环境里跑：
    <venv>/python.exe packaging/build.py
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
FRONTEND = ROOT / "frontend"
RELEASE = ROOT / "dist"
WORK = ROOT / "build"
KERNEL_SRC = RELEASE / "kernel" / "NetSwitcherKernel"

PORTABLE_README = """NetSwitcher {ver} · 免安装版
============================

1. 解压到任意目录（路径别带中文，省得自找麻烦）
2. 双击 NetSwitcher.exe，Windows 会弹 UAC，点「是」
   —— 启停网卡必须管理员权限，不给就没法切换
3. 关掉窗口即退出，内核会跟着一起关

配置与日志写在 %APPDATA%\\NetSwitcher，不写本目录，所以整个文件夹可以随处移动、
直接删除也不留垃圾。

不需要安装 Python，也不需要联网。
"""

# 不该进源码包的东西
SOURCE_SKIP_DIRS = {
    ".workbuddy",
    ".git",
    "node_modules",
    "__pycache__",
    "build",
    "dist",
    "release",
}
SOURCE_SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".zip", ".exe", ".blockmap"}


# --------------------------------------------------------------------- 基础
def version() -> str:
    text = (ROOT / "netswitcher" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("读不到 netswitcher/__init__.py 里的版本号")
    return match.group(1)


def clean_env() -> dict[str, str]:
    """跑 npm/node 时的干净环境。

    顺手去掉几类会坏事的变量：
      - http(s)_proxy：本机代理常常只转发浏览器，npm 走它反而 502
      - ELECTRON_RUN_AS_NODE：会让 require('electron') 返回一个路径而不是 API，报错很迷惑
    """
    env = dict(os.environ)
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(key, None)
    env.pop("ELECTRON_RUN_AS_NODE", None)
    return env


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("· " + " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True, cwd=cwd or ROOT, env=env)


def npx() -> str:
    for name in ("npx.cmd", "npx"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit("找不到 npx，请先安装 Node.js 并加进 PATH")


def ensure_icon() -> Path:
    icon = PACKAGING / "netswitcher.ico"
    if not icon.is_file():
        run([sys.executable, str(PACKAGING / "make_icon.py")])
    return icon


def fresh_out_dir() -> Path:
    """每次构建用一个全新的输出目录。

    electron-builder 会先删旧目录，而某些环境（带删除钩子的）会拦批量删除，
    与其和它较劲，不如换一个空目录。
    """
    base = RELEASE / "electron"
    if not base.exists():
        return base
    index = 2
    while (RELEASE / f"electron-{index}").exists():
        index += 1
    return RELEASE / f"electron-{index}"


# --------------------------------------------------------------------- 内核
def build_kernel(icon: Path) -> Path:
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--console",
            "--onedir",
            "--name",
            "NetSwitcherKernel",
            "--icon",
            str(icon),
            "--paths",
            str(ROOT),
            "--add-data",
            f"{ROOT / 'frontend' / 'renderer'}{os.pathsep}frontend/renderer",
            "--distpath",
            str(RELEASE / "kernel"),
            "--workpath",
            str(WORK / "pyinstaller-kernel"),
            "--specpath",
            str(WORK),
            str(PACKAGING / "entry_kernel.py"),
        ]
    )
    exe = KERNEL_SRC / "NetSwitcherKernel.exe"
    if not exe.is_file():
        raise SystemExit("内核构建失败")
    return exe


def self_check_kernel(exe: Path) -> None:
    """真的起一次内核，确认它打印 ready 且页面能取到。"""
    import json
    import time
    import urllib.request

    proc = subprocess.Popen(
        [str(exe), "serve", "--port", "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    ready = None
    deadline = time.monotonic() + 25
    try:
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if line.startswith("{"):
                ready = json.loads(line)
                break
        if not ready:
            raise SystemExit("内核自检失败：没有输出 ready JSON")
        url = f"http://127.0.0.1:{ready['port']}/?token={ready['token']}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            if resp.status != 200 or len(resp.read()) < 1000:
                raise SystemExit("内核自检失败：页面取不到")
        print(f"· 内核自检通过（端口 {ready['port']}）")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --------------------------------------------------------------------- 打包
def build_electron(ver: str) -> tuple[Path, Path]:
    if not (FRONTEND / "node_modules" / "electron-builder").is_dir():
        raise SystemExit("frontend 还没装依赖：先执行 cd frontend && npm install")

    out = fresh_out_dir()
    run(
        [npx(), "electron-builder", "--win", "nsis", f"--config.directories.output={out}"],
        cwd=FRONTEND,
        env=clean_env(),
    )

    unpacked = out / "win-unpacked" / "NetSwitcher.exe"
    setup = out / f"NetSwitcher-{ver}-Setup.exe"
    if not unpacked.is_file() or not setup.is_file():
        raise SystemExit("electron-builder 没有产出预期文件")

    # 归位到 dist/ 根上，release 时只认这两个名字
    final_setup = RELEASE / f"NetSwitcher-{ver}-Setup.exe"
    shutil.copy2(setup, final_setup)

    portable = RELEASE / f"NetSwitcher-{ver}-Portable.zip"
    src = out / "win-unpacked"
    with zipfile.ZipFile(portable, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, f"NetSwitcher-{ver}/{path.relative_to(src).as_posix()}")
        zf.writestr(
            f"NetSwitcher-{ver}/README.txt",
            PORTABLE_README.format(ver=ver).encode("utf-8"),
        )
    return portable, final_setup


def build_source(ver: str) -> Path:
    out = RELEASE / f"NetSwitcher-{ver}-Source.zip"
    arc = f"NetSwitcher-{ver}"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if set(rel.parts) & SOURCE_SKIP_DIRS:
                continue
            if path.suffix in SOURCE_SKIP_SUFFIX or path.name.startswith((".tmp_", ".shot")):
                continue
            # .npmrc 只是本机构建时指向镜像用的，不该强加给所有用户
            if path.name == ".npmrc":
                continue
            zf.write(path, f"{arc}/{rel.as_posix()}")
    return out


def human(path: Path) -> str:
    return f"{path.stat().st_size / 1024 / 1024:.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description="构建 NetSwitcher 发布物")
    parser.add_argument("--skip-electron", action="store_true", help="只构建内核和源码")
    args = parser.parse_args()

    ver = version()
    print(f"NetSwitcher {ver}（Electron 版）\n")
    RELEASE.mkdir(parents=True, exist_ok=True)

    icon = ensure_icon()
    exe = build_kernel(icon)
    self_check_kernel(exe)

    if not args.skip_electron:
        portable, setup = build_electron(ver)
        print(f"· 绿色版 {portable.name}  {human(portable)}")
        print(f"· 安装版 {setup.name}  {human(setup)}")

    source = build_source(ver)
    print(f"· 源码   {source.name}  {human(source)}")
    print(f"\n完成，产物在 {RELEASE}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
