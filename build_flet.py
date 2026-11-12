# -*- coding: utf-8 -*-
"""
Flet 版打包 —— 把绿梦小助手（Flet 新 UI）打成免安装 exe
================================================================
用法：
    python build_flet.py                 # 打包到 dist_flet/Lumin/
    python build_flet.py --verify        # 打包后立刻跑 exe 自检
    python build_flet.py --zip           # 额外生成 dist_flet/Lumin-win64.zip
    python build_flet.py --installer     # 额外生成 NSIS 安装程序（复用 installer.nsi）

打包说明：
    - 入口是 lumin_flet_main.py（Flet 版），用 `flet pack` 打包，
      它会自动带上 Flet 渲染所需的 Web 静态资源（这是裸 PyInstaller 最容易漏的）。
    - 资源复制逻辑（tp_imgs / recordings / 脚本）复用 runtime_env 的
      「exe 同级目录」约定，保证运行时能读到可编辑资源。

体积优化要点：
    1. opencv 用 headless 版（无 GUI / 精简视频 I/O），比完整版省 ~25MB；
    2. --onedir 目录模式（不用 onefile，避免启动慢 + 临时目录解压）；
    3. --exclude 掉 matplotlib/pandas/scipy 等用不到的大包。
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

PROJ = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Lumin"
APP_DISPLAY = "绿梦小助手"
ENTRY = os.path.join(PROJ, "lumin_flet_main.py")
ICON = os.path.join(PROJ, "Lumin.ico")
NSI = os.path.join(PROJ, "installer.nsi")
DIST_ROOT = os.path.join(PROJ, "dist_flet")
OUT_DIR = os.path.join(DIST_ROOT, APP_NAME)
SETUP_NAME = "Lumin-Setup.exe"

# 需要跟着 exe 一起发布的资源目录（运行时写、可编辑，放 exe 同级，不打进 exe 内部）
COPY_DIRS = ["tp_imgs", "recordings"]
# 需要跟着 exe 一起发布的脚本（子进程用 runpy 执行，必须在磁盘上）
COPY_FILES = [
    "imgfind.py",          # 图像识别
    "teleport.py",         # 跨区域传送
    "recorder.py",         # 录制器
    "record_to_script.py", # 录制 → 脚本生成
    "runtime_env.py",      # 运行环境适配
    "macro_backend.py",    # 后端（Flet 版复用 MacroManager）
    "macro_ui_flet.py",    # Flet 版界面（--run-py 模式可能被引用）
    "app_icon.png",        # UI 内嵌 Logo（标题栏）
    "Lumin.ico",           # 窗口标题栏图标
]

# pynput 后端是运行时按平台导入，PyInstaller 静态分析不到
HIDDEN_IMPORTS = [
    "win32timezone",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pynput._util.win32",
]

# 明显用不到的大包，排除掉能省几十 MB（tkinter 是旧版 UI 遗留，本项目已纯 Flet 化）
EXCLUDES = [
    "matplotlib", "pandas", "scipy", "IPython", "notebook", "jupyter",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx", "tkinter",
    "pytest", "sphinx", "docutils", "test", "unittest",
    # PIL/Pillow：只有 make_icon.py 构建时用，运行时完全用不到
    "PIL",
    # 与本项目无关的重型依赖（cnstd/rapidocr/ultralytics 是别的项目装的，
    # 它们会连带把 torch 等巨型包拖进来，务必排除）
    "torch", "torchvision", "ultralytics", "cnstd", "rapidocr",
    "onnx", "onnxruntime", "paddle", "paddleocr",
]

# 打包完成后，从产物目录里删掉这些确认无用的文件（相对 _internal 的路径）。
# 为什么用「打包后删」而不是 exclude：
#   opencv 的 videoio dll（ffmpeg）是 cv2 的「视频读写」后端，本项目只用
#   图片匹配（matchTemplate/imdecode），实测删除后功能完全正常，省 28MB。
# 注意：只删「单个大文件」，不删目录——删目录会触发批量删除保护，
# 且 tcl/tk 数据只有几 MB，不值得冒险。
STRIP_PATHS = [
    "cv2/opencv_videoio_ffmpeg4130_64.dll",   # 视频读写后端（28MB，用不到）
    "cv2/opencv_videoio_ffmpeg4120_64.dll",   # 同上（旧版本残留，若有）
]

README = """绿梦自动化助手（Flet 版）· 使用说明
================================================================

一、启动
    1. 双击 Lumin.exe（程序会请求管理员权限，点「是」；否则游戏收不到模拟按键）。
    2. 界面打开后，下拉框选择一个脚本，点「启动」。

二、目录说明
    Lumin.exe              主程序（Flet 新界面）
    recordings/            录好的脚本（.py）+ 录制数据（.json）+ 名称/传送点配置
    tp_imgs/               图像识别模板图（休息点、传送按钮、传送点图标等）
    macro_log.txt          每次运行的日志（启动时覆盖）
    runs.jsonl             每轮统计（轮次/耗时/传送是否成功）
    teleport_log.txt       跨区域传送日志
    recorder_log.txt       录制器日志

三、常见问题
    1. 启动后一直提示「未找到休息点图标」
       → 检查当前分辨率是否为 1920x1080，缩放比例 100%。
    2. 连续 3 轮传送失败会自动暂停（熔断）
       → 看 macro_log.txt，通常是地图没打开或休息点图过期。
    3. 双击没反应
       → 看同目录下的 lumin_error.log。
"""


def log(msg):
    print(msg, flush=True)


def find_flet_exe():
    """定位 flet CLI（flet.exe），它在 Python Scripts 目录。"""
    candidates = [
        os.path.join(os.path.dirname(sys.executable), "flet.exe"),
        os.path.join(os.path.dirname(sys.executable), "Scripts", "flet.exe"),
        shutil.which("flet"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def find_makensis():
    """定位 NSIS 的 makensis.exe，用于生成安装程序。"""
    candidates = [
        shutil.which("makensis"),
        r"C:\Program Files (x86)\NSIS\makensis.exe",
        r"C:\Program Files\NSIS\makensis.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def check_sources():
    missing = [p for p in [ENTRY, ICON] + COPY_FILES
               if not os.path.exists(os.path.join(PROJ, p))]
    missing += [d for d in COPY_DIRS if not os.path.isdir(os.path.join(PROJ, d))]
    if missing:
        sys.exit("缺少必要文件/目录：" + "、".join(missing))
    n_scripts = len([f for f in os.listdir(os.path.join(PROJ, "recordings"))
                     if f.endswith(".py")])
    log(f"源文件检查通过：{n_scripts} 个脚本，"
        f"{len(os.listdir(os.path.join(PROJ, 'tp_imgs')))} 张模板图")


def park_old_output():
    """把上一次产物挪到临时目录（避免批量删除保护 / 残留）。"""
    if not os.path.isdir(OUT_DIR):
        return
    dest = os.path.join(tempfile.gettempdir(),
                        f"{APP_NAME}_flet旧构建_{time.strftime('%Y%m%d_%H%M%S')}")
    try:
        shutil.move(OUT_DIR, dest)
        log(f"旧构建已移到：{dest}")
    except Exception as e:
        log(f"旧构建挪动失败（{e}），尝试原地覆盖")


def build():
    flet_exe = find_flet_exe()
    if not flet_exe:
        sys.exit("未找到 flet CLI，请先安装：pip install flet")

    # flet pack 底层调 PyInstaller，透传参数：
    #   --exclude-module 逐个传（通过 --pyinstaller-build-args）
    cmd = [
        flet_exe, "pack", ENTRY,
        "--name", APP_NAME,
        "--onedir",                      # 目录模式
        "--icon", ICON,
        "--distpath", DIST_ROOT,
        # 注意：不用 --uac-admin。它会给 exe 打 UAC manifest，强制所有身份
        # （包括 --selftest 自检、--run-py 子进程运行器）都要管理员权限，
        # 导致自检/脚本子进程在普通 shell 下无法启动。提权改由入口代码里的
        # relaunch_as_admin() 负责（只在正常启动 UI 时触发）。
        "--product-name", APP_DISPLAY,
        "--product-version", "1.1.1",
        "--file-description", "绿梦小助手（Flet 版）—— 键鼠脚本自动化",
        "--company-name", "leymonct",
        "-y",                            # flet 自动确认（不交互询问）
    ]
    for m in HIDDEN_IMPORTS:
        cmd += ["--hidden-import", m]
    # PyInstaller 透传参数：--pyinstaller-build-args 需要 = 语法，且整体透传。
    # 把所有 --exclude-module + --noconfirm 拼成一个空格分隔的字符串，一次性传入。
    # --noconfirm 关键：否则 PyInstaller 检测到旧 build/ 目录时会交互询问 y/n 卡死。
    pbuild = " ".join(f"--exclude-module={m}" for m in EXCLUDES)
    pbuild += " --noconfirm"
    cmd += ["--pyinstaller-build-args=" + pbuild]

    log("\n开始打包 Flet 版（首次约 2~5 分钟）……")
    log("命令：" + " ".join(cmd[:8]) + " …（省略长参数）")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=PROJ, env=env)
    if r.returncode != 0:
        sys.exit(f"打包失败（退出码 {r.returncode}）")
    log(f"flet pack 完成，用时 {time.time() - t0:.1f}s")


def _sync_tree(src, dst):
    """把 src 目录镜像到 dst（robocopy 优先，退回逐文件复制）。"""
    os.makedirs(dst, exist_ok=True)
    copied = False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["robocopy", src, dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS",
                 "/NP", "/R:1", "/W:1", "/XD", "__pycache__", "/XF", "*.pyc", "*.pyo"],
                capture_output=True,
            )
            copied = r.returncode < 8
        except Exception:
            copied = False

    keep = set()
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        rel = os.path.relpath(root, src)
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            rel_f = f if rel == "." else os.path.join(rel, f)
            keep.add(os.path.normcase(rel_f))
            if not copied:
                target = dst if rel == "." else os.path.join(dst, rel)
                os.makedirs(target, exist_ok=True)
                shutil.copy2(os.path.join(root, f), os.path.join(target, f))

    if not keep:
        return 0
    removed = 0
    for root, _dirs, files in os.walk(dst):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, dst)
            if os.path.normcase(rel) not in keep:
                try:
                    os.remove(full)
                    removed += 1
                except OSError:
                    pass
    return removed


def copy_resources():
    log("\n复制资源文件……")
    for d in COPY_DIRS:
        src = os.path.join(PROJ, d)
        dst = os.path.join(OUT_DIR, d)
        stale = _sync_tree(src, dst)
        extra = f"（清理残留 {stale} 个）" if stale else ""
        log(f"  {d}/  →  {dst}{extra}")

    for f in COPY_FILES:
        shutil.copy2(os.path.join(PROJ, f), os.path.join(OUT_DIR, f))
        log(f"  {f}")

    with open(os.path.join(OUT_DIR, "使用说明.txt"), "w", encoding="utf-8") as fp:
        fp.write(README)
    log("  使用说明.txt")


def strip_bloat():
    """打包后精简：删除确认无用的文件（opencv 的视频读写 videoio dll）。

    这些 dll 是 PyInstaller 连带打进来的冗余，本项目只用图片匹配
    （matchTemplate/imdecode），不需要视频读写，实测删除后功能正常。
    删除前做存在性判断，删不掉也不中断（只警告）。
    """
    log("\n精简产物（删除冗余依赖）……")
    internal = os.path.join(OUT_DIR, "_internal")
    saved = 0
    for rel in STRIP_PATHS:
        path = os.path.join(internal, rel)
        if os.path.isfile(path):
            try:
                sz = os.path.getsize(path)
                saved += sz
                os.remove(path)
                log(f"  删除文件 {rel}（{sz / 1024 / 1024:.1f} MB）")
            except OSError as e:
                log(f"  [警告] 删除 {rel} 失败：{e}")
        elif os.path.isdir(path):
            try:
                sz = dir_size(path)
                saved += sz
                shutil.rmtree(path)
                log(f"  删除目录 {rel}（{sz / 1024 / 1024:.1f} MB）")
            except OSError as e:
                log(f"  [警告] 删除 {rel} 失败：{e}")
        else:
            log(f"  [跳过] {rel} 不存在")
    log(f"精简完成，节省约 {saved / 1024 / 1024:.1f} MB")


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def summary():
    exe = os.path.join(OUT_DIR, APP_NAME + ".exe")
    if not os.path.exists(exe):
        sys.exit("打包产物缺失：" + exe)
    size = dir_size(OUT_DIR)
    log("\n" + "=" * 56)
    log("产物目录：" + OUT_DIR)
    log(f"总大小：{size / 1024 / 1024:.1f} MB"
        f"，其中 exe {os.path.getsize(exe) / 1024 / 1024:.1f} MB")
    log("双击运行：" + exe)
    log("=" * 56)
    return exe


def verify(exe):
    log("\n运行 exe 自检（--selftest）……")
    env = dict(os.environ, LUMIN_NO_ELEVATE="1", PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([exe, "--selftest"], cwd=OUT_DIR, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
    except subprocess.TimeoutExpired:
        log("自检超时（180s）")
        return False
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.strip().splitlines():
        log("  " + line)
    ok = r.returncode == 0 and "全部通过" in out
    log(("自检通过 ✅" if ok else f"自检失败 ❌（退出码 {r.returncode}）"))
    return ok


def make_zip():
    zip_path = os.path.join(DIST_ROOT, f"{APP_NAME}-win64.zip")
    log(f"\n生成压缩包 {zip_path} ……")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(OUT_DIR):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, DIST_ROOT))
    log(f"压缩包大小：{os.path.getsize(zip_path) / 1024 / 1024:.1f} MB")
    return zip_path


def make_installer():
    """调用 makensis 生成 NSIS 安装程序（Lumin-Setup.exe）。"""
    makensis = find_makensis()
    if not makensis:
        log("\n[跳过] 未找到 makensis（NSIS），无法生成安装程序。"
            "请安装 NSIS：https://nsis.sourceforge.io/Download")
        return None

    if not os.path.isdir(OUT_DIR):
        log("\n[跳过] 产物目录不存在，无法生成安装程序")
        return None

    log(f"\n生成 NSIS 安装程序（{makensis}）……")
    try:
        r = subprocess.run(
            [makensis, "/V2", NSI],
            cwd=PROJ,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=600,
        )
    except subprocess.TimeoutExpired:
        log("生成安装程序超时（600s）")
        return None

    if r.returncode != 0:
        log("生成安装程序失败：")
        for line in (r.stdout or "").splitlines()[-15:]:
            log("  " + line)
        for line in (r.stderr or "").splitlines()[-5:]:
            log("  " + line)
        return None

    setup = os.path.join(DIST_ROOT, SETUP_NAME)
    if os.path.exists(setup):
        log(f"安装程序：{setup}（{os.path.getsize(setup) / 1024 / 1024:.1f} MB）")
        return setup
    log("安装程序未生成（OutFile 路径可能不对）")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="额外生成 zip 压缩包")
    ap.add_argument("--installer", action="store_true", help="额外生成 NSIS 安装程序")
    ap.add_argument("--verify", action="store_true", help="打包后运行 exe 自检")
    ap.add_argument("--no-build", action="store_true", help="跳过打包，只做验证/压缩")
    args = ap.parse_args()

    if not args.no_build:
        check_sources()
        park_old_output()
        build()
        copy_resources()
        strip_bloat()
    exe = summary()

    ok = True
    if args.verify or args.no_build:
        ok = verify(exe)
    if args.zip:
        make_zip()
    if args.installer:
        make_installer()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
