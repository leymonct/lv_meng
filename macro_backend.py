# -*- coding: utf-8 -*-
"""
脚本管理器 —— 后端逻辑（纯逻辑，不含界面）
============================================
本模块只负责后端能力，供 Flet 版界面（macro_ui_flet.py）复用，本身不依赖任何
GUI 库（tkinter 等），因此可以安全地在无 GUI 的打包环境下 import。

包含：
    - is_admin / relaunch_as_admin  权限检查与提权
    - find_game_window / focus_game_window / focus_ui_window  窗口定位与前台切换
    - send_f8_key  模拟 F8（自动开始）
    - check_update  检查 GitHub Releases 是否有新版本
    - MacroManager  脚本子进程 / 传送 / 录制器 / 计划（编排任务）的启动、停止与管理
    - MACROS / RECORD_DIR / GAME_TITLE_KEYWORDS 等全局配置常量

界面由 macro_ui_flet.py 实现，入口见 lumin_flet_main.py。
"""

import os
import sys
import re
import ctypes
import json
import subprocess
import threading
import time

import win32gui
import win32con
import win32process
import win32api

# 运行环境适配（源码 / 打包 exe 两种形态）：
#   打包后 __file__ 指向临时解压目录，不能用来定位 tp_imgs、recordings；
#   子进程也不能再用 sys.executable + 脚本路径（那会变成再开一个界面）。
# 具体差异统一封装在 runtime_env 里。
import runtime_env


def is_admin():
    """判断当前进程是否管理员权限"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() == 1
    except Exception:
        return False


def relaunch_as_admin():
    """以管理员权限重新启动本程序，返回是否已触发提权"""
    try:
        # 用 ShellExecute 的 runas 动词提权重启
        # 源码模式：python.exe + 本脚本路径；打包模式：exe 自己，不能再带脚本参数
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            runtime_env.elevation_params(), runtime_env.base_dir(), 1
        )
        return True
    except Exception as e:
        return False

# ------------------------- 配置 -------------------------
# 静态脚本列表（当前为空）：
#   name     界面显示名
#   script   脚本文件名（相对本目录）
#   teleport 传送点名称（对应 teleport.py 里的 TELEPORT_POINTS 键名）。
#            留空字符串 "" 表示该脚本不需要传送。
#
# 说明：早期有 farm_1/2/3.py 三个手写脚本，已于 2026-11-03 移除，改为统一用
# 录制器录制（recorder.py + record_to_script.py），走 recordings/ 目录动态管理。
# 如需新增静态脚本，可参考下方注释格式往本列表加条目。
MACROS = [
    #    {"name": "瓦尔肯要塞·淬火池",   "script": "farm_1.py", "teleport": "淬火池"},
    #    {"name": "瓦尔肯要塞·钢铁余音", "script": "farm_2.py", "teleport": "钢铁余音"},
    #    {"name": "奥鲁姆要塞·西侧通道", "script": "farm_3.py", "teleport": "西侧通道"},
]

# 传送脚本文件名
TELEPORT_SCRIPT = "teleport.py"

# 录制器脚本文件名
RECORDER_SCRIPT = "recorder.py"

# 录制产物目录（录制器把生成的 .py 脚本放这里）
RECORD_DIR = "recordings"

# 录制脚本自定义显示名映射文件：{脚本文件名: 自定义显示名}
# 只改下拉框里的显示名，不动脚本文件本身。重启 UI 后名字仍然保留。
NAMES_FILE = os.path.join(RECORD_DIR, "names.json")

# 录制脚本传送点配置：{脚本文件名: 传送点名}
# 为录制脚本指定一个传送点后，勾选「自动传送」启动时会先传送到目标地图再刷怪。
TELEPORTS_FILE = os.path.join(RECORD_DIR, "teleports.json")

# 计划（编排任务）方案文件：{方案名: {steps: [{script, loops}, ...]}}
# 一个方案 = 一组按顺序执行的「传送 → 跑 N 轮」步骤，用于无人值守地依次刷多张地图。
# 注意：步骤里不存传送点，运行时用 get_script_teleport() 取该脚本「已配置」的传送点。
PLANS_FILE = os.path.join(RECORD_DIR, "plans.json")

# 游戏窗口匹配关键词（窗口标题包含任意一个即匹配）
# 注意：不要用「绿梦」这类宽泛词，会误匹配到界面自己的标题「绿梦刷怪」
GAME_TITLE_KEYWORDS = ["Lumin"]

# 录制模板图的基准分辨率：tp_imgs/ 里的图标都是在这个分辨率下截的。
# 一旦屏幕分辨率/缩放变了，图标位置和大小都会变，图像匹配会大面积失败。
# 启动前体检发现不一致时给出警告（不阻止启动，因为外接显示器在某些设置下仍可用）。
RECORD_SCREEN = (1920, 1080)

BASE_DIR = runtime_env.base_dir()   # 源码=项目目录；打包=exe 所在目录


# ------------------------- 窗口工具 -------------------------
def find_game_window():
    """查找游戏窗口，返回 (hwnd, title) 或 (None, None)"""
    result = []
    # 记录界面自己的进程 ID，用于排除
    import ctypes as _ct
    self_pid = _ct.windll.kernel32.GetCurrentProcessId()

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        # 排除界面自己
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] == self_pid:
                return
        except Exception:
            pass
        for kw in GAME_TITLE_KEYWORDS:
            if kw.lower() in title.lower():
                result.append((hwnd, title))
                return

    win32gui.EnumWindows(enum_cb, None)
    if result:
        return result[0]
    return None, None


def focus_game_window():
    """把焦点切换到游戏窗口（Unity 无边框全屏窗口用 SetForegroundWindow 即可）

    由于本程序可能是「后台进程」尝试把游戏窗口切到前台（例如 Flet 版里，
    Python 进程并非前台进程，前台是 Flet/Flutter 引擎的窗口），Windows 的前台
    锁定机制会拒绝 SetForegroundWindow（返回错误码 0）。因此这里做了多级兜底：
    1) AttachThreadInput 附加到前台窗口线程；
    2) 模拟 Alt 键，让系统认为有用户输入，从而放行前台切换（微软官方建议）；
    3) 多次重试 + 最终校验。
    """
    hwnd, title = find_game_window()
    if not hwnd:
        return False, "未找到游戏窗口（标题含 Lumin）"

    try:
        # 若窗口最小化则先还原
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.1)

        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        # 多级兜底切换，最多尝试若干次
        last_err = None
        for attempt in range(4):
            try:
                # 第 1 级：附加到前台窗口线程后切换
                foreground_hwnd = win32gui.GetForegroundWindow()
                current_thread = win32api.GetCurrentThreadId()
                fg_thread = win32process.GetWindowThreadProcessId(foreground_hwnd)[0]

                attached = False
                if fg_thread != current_thread:
                    try:
                        win32process.AttachThreadInput(current_thread, fg_thread, True)
                        attached = True
                    except Exception:
                        pass

                win32gui.SetForegroundWindow(hwnd)

                if attached:
                    try:
                        win32process.AttachThreadInput(current_thread, fg_thread, False)
                    except Exception:
                        pass
            except Exception as e:
                last_err = e

            time.sleep(0.15)
            if win32gui.GetForegroundWindow() == hwnd:
                return True, f"已切换到游戏窗口：{title}"

            # 第 2 级：模拟 Alt 键，绕过前台锁定（仅在首次失败后执行）
            if attempt >= 1:
                _simulate_alt_key()
                time.sleep(0.1)

        # 最终校验
        if win32gui.GetForegroundWindow() == hwnd:
            return True, f"已切换到游戏窗口：{title}"
        cur = win32gui.GetWindowText(win32gui.GetForegroundWindow())
        detail = f"（{last_err}）" if last_err else ""
        return False, f"切换未生效，目标窗口：{title}，当前前台：{cur}{detail}"
    except Exception as e:
        return False, f"切换窗口失败：{e}"


def _simulate_alt_key():
    """模拟一次 Alt 键按下/抬起，绕过 Windows 前台锁定。

    Windows 的 SetForegroundWindow 文档明确说明：进程只有在「收到用户最近一次
    输入」时才能设置前台窗口。模拟一次按键可让系统认为存在用户输入，从而放行。
    """
    try:
        VK_MENU = 0x12  # Alt
        KEYEVENTF_KEYUP = 0x0002
        # 用 ctypes 直接调 keybd_event（比 win32api.keybd_event 更底层可靠）
        import ctypes
        ctypes.windll.user32.keybd_event(VK_MENU, 0, 0, 0)
        ctypes.windll.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def send_f8_key():
    """模拟按一次 F8 键，用于「自动开始」脚本。

    录制脚本（recordings/*.py）内部监听 F8：按下后把 running 置 True 开始循环。
    正常情况下需要用户手动按 F8 才开始，这里用 keybd_event 模拟按键，让脚本
    在传送完成后自动开始，无需人工介入。

    用 ctypes 直接调 keybd_event（与 _simulate_alt_key 一致，比 win32api 更底层
    可靠），按下后短暂停顿再抬起，模拟一次真实的敲击。
    """
    try:
        import ctypes
        VK_F8 = 0x77
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(VK_F8, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(VK_F8, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _parse_version(v):
    """把版本号字符串（如 "1.1.1" / "v1.1.1" / "1.1.1-beta"）解析成整数元组，
    用于比较大小。无法解析时返回空元组（视为最低版本）。"""
    if not v:
        return ()
    s = str(v).strip().lstrip("vV")
    # 只取数字和点，忽略 -beta / -rc 等后缀
    m = re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)
    if not m:
        return ()
    parts = [int(g) if g is not None else 0 for g in m.groups()]
    return tuple(parts)


def check_update(timeout=8):
    """检查是否有新版本（对比 GitHub Releases 最新 tag）。

    通过 GitHub API 获取仓库最新 release 的 tag_name（如 "v1.2.0"），
    与当前 APP_VERSION 比较。返回 dict：
        {
            "ok": True/False,          # 是否成功完成检查（网络失败为 False）
            "has_update": True/False,  # 是否有新版本
            "latest": "v1.2.0",        # 最新版本号（检查失败时为空）
            "current": "1.1.1",        # 当前版本号
            "url": "...",              # 最新 release 的下载页/详情页
            "msg": "人类可读信息",
        }
    """
    import urllib.request

    current = runtime_env.APP_VERSION
    base = {
        "ok": False,
        "has_update": False,
        "latest": "",
        "current": current,
        "url": runtime_env.GITHUB_RELEASES_URL,
        "msg": "",
    }

    try:
        req = urllib.request.Request(
            runtime_env.GITHUB_API_LATEST,
            headers={"User-Agent": "Lumin-Assistant", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))

        latest_tag = data.get("tag_name", "") or ""
        html_url = data.get("html_url", "") or runtime_env.GITHUB_RELEASES_URL

        base["latest"] = latest_tag
        base["url"] = html_url
        base["ok"] = True

        if not latest_tag:
            base["msg"] = "未能获取到最新版本信息"
            return base

        if _parse_version(latest_tag) > _parse_version(current):
            base["has_update"] = True
            base["msg"] = f"发现新版本 {latest_tag}（当前 {current}）"
        else:
            base["msg"] = f"已是最新版本 {current}"
        return base
    except Exception as e:
        base["msg"] = f"检查更新失败：{e}"
        return base


def focus_ui_window():
    """把焦点切回本程序自己的 UI 窗口（标题含「绿梦小助手」）。

    脚本运行时会调用 focus_game_window() 把游戏窗口切到前台；脚本跑完后焦点仍
    停在游戏窗口（尤其全屏游戏），用户看不到、也点不到 UI，表现为「切不回界面、
    无反应」。本函数在脚本结束后把 UI 窗口重新切回前台。

    实现：枚举所有可见窗口，找到标题含「绿梦小助手」的窗口（本程序自己的
    窗口），复用 focus_game_window 的多级兜底逻辑（AttachThreadInput + 模拟 Alt）
    把它切回前台。
    """
    import ctypes as _ct
    self_pid = _ct.windll.kernel32.GetCurrentProcessId()

    target = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        # 只找本程序自己的窗口（源码模式下 Flutter 引擎可能是独立进程，
        # 所以不能只按 self_pid 过滤；改用标题精确匹配「绿梦小助手」）
        if "绿梦小助手" in title:
            target.append(hwnd)

    win32gui.EnumWindows(enum_cb, None)
    if not target:
        return False, "未找到界面窗口（标题含「绿梦小助手」）"
    hwnd = target[0]

    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.1)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        for attempt in range(4):
            try:
                foreground_hwnd = win32gui.GetForegroundWindow()
                current_thread = win32api.GetCurrentThreadId()
                fg_thread = win32process.GetWindowThreadProcessId(foreground_hwnd)[0]
                attached = False
                if fg_thread != current_thread:
                    try:
                        win32process.AttachThreadInput(current_thread, fg_thread, True)
                        attached = True
                    except Exception:
                        pass
                win32gui.SetForegroundWindow(hwnd)
                if attached:
                    try:
                        win32process.AttachThreadInput(current_thread, fg_thread, False)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(0.15)
            if win32gui.GetForegroundWindow() == hwnd:
                return True, "已切回界面窗口"
            if attempt >= 1:
                _simulate_alt_key()
                time.sleep(0.1)
        if win32gui.GetForegroundWindow() == hwnd:
            return True, "已切回界面窗口"
        return False, "切回界面未生效"
    except Exception as e:
        return False, f"切回界面失败：{e}"


# ------------------------- 脚本子进程管理 -------------------------
class MacroManager:
    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self._running = False   # 明确的运行状态标志（单一事实来源）
        self._log_file = None
        self._recorder_proc = None  # 录制器子进程
        self._teleport_proc = None  # 传送子进程（用于「停止」时中断传送）
        self._teleport_cancelled = False  # 用户是否在传送阶段点了停止
        self._plan_running = False  # 计划是否在运行（编排任务执行中）
        self._plan_cancelled = False  # 用户是否请求取消计划

    @staticmethod
    def _clear_pyc(py_path):
        """删除指定 .py 文件对应的 __pycache__/*.pyc 字节码缓存。

        背景：Python 用 .pyc 缓存加速执行，缓存头里存的是「源文件 mtime（秒级）+ 大小」。
        当我们改写一个 .py 文件、且新旧文件在同一秒内（mtime 秒数相同、大小也碰巧相同）时，
        子进程执行 `python xxx.py` 仍会命中旧的 .pyc，导致「改了却没生效、要改两次才生效」。
        删除对应 pyc 后，下次执行会重新编译源文件，保证改动立即生效。
        """
        try:
            src_dir = os.path.dirname(py_path)
            stem = os.path.splitext(os.path.basename(py_path))[0]
            cache_dir = os.path.join(src_dir, "__pycache__")
            if not os.path.isdir(cache_dir):
                return
            for fname in os.listdir(cache_dir):
                # 匹配 <stem>.cpython-*.pyc（含 .cpython-312.pyc 等任意版本）
                if fname.startswith(stem + ".") and fname.endswith(".pyc"):
                    try:
                        os.remove(os.path.join(cache_dir, fname))
                    except Exception:
                        pass
        except Exception:
            pass

    @staticmethod
    def _sync_teleport_in_memory(teleport_name, region, point_img_name, confirm_img_name=None):
        """把新增/更新的传送点同步到内存里已加载的 teleport 模块，避免下次重新加载。

        为什么不用 sys.modules.pop("teleport") 强制重载：
        `import teleport` 会连带 import cv2、mss，并实例化 pynput 的 Controller
        （实测约 2 秒）。若配置完传送点后把它从 sys.modules 移除，下一次
        get_available_teleport_points / get_available_regions 里的 `import teleport`
        就会**完整重载一遍**（cv2 + pynput 初始化）。而配置流程选完图后会在后台
        线程里连续调用这两三个方法，反复重载导致 UI 事件循环被阻塞、界面卡死。

        正确做法：teleport 模块首次 import 后一直驻留内存（这本身没问题），
        这里只需直接改写它内存里的 TELEPORT_POINTS 字典，让当前会话立即看到新点，
        同时磁盘文件已由 add_teleport_point 写入（保证下次启动仍生效）。
        这样 `import teleport` 只在首次真正加载一次，之后全是缓存命中，不再卡顿。
        """
        try:
            import teleport
            entry = {"region": region, "point": point_img_name}
            if confirm_img_name:
                entry["confirm"] = confirm_img_name
            teleport.TELEPORT_POINTS[teleport_name] = entry
        except Exception:
            # 模块尚未加载（或加载失败）时不强求：下次 import 会读到磁盘上的新值
            pass

    # ------------------------- 启动前体检 -------------------------
    @staticmethod
    def _script_info(script_path):
        """从脚本源码里读出关键配置，供启动前体检使用。

        返回 {"exist": bool, "loop_enabled": bool, "rest_img": str}
        只做文本匹配，不执行脚本（避免导入脚本时产生任何副作用）。
        """
        info = {"exist": False, "loop_enabled": False, "rest_img": ""}
        if not os.path.exists(script_path):
            return info
        info["exist"] = True
        try:
            with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            return info

        m = re.search(r"^LOOP_ENABLED\s*=\s*(True|False)", text, re.M)
        if m:
            info["loop_enabled"] = (m.group(1) == "True")
        m = re.search(r'^LOCAL_REST_POINT_IMG\s*=\s*"([^"]+)"', text, re.M)
        if m:
            info["rest_img"] = m.group(1)
        return info

    def preflight(self, script):
        """启动前体检，返回 (blockers, warnings) 两个字符串列表。

        blockers —— 必须解决才能启动（缺脚本 / 缺模板图 / 游戏没开）
        warnings —— 可以继续，但需要用户确认（分辨率不一致 / 传送点失效）

        目的：把「启动后跑几百轮才发现图没了」提前变成「启动前一句话说清楚」。
        """
        blockers, warnings = [], []

        script_path = os.path.join(BASE_DIR, script)
        info = self._script_info(script_path)
        if not info["exist"]:
            blockers.append(f"脚本文件不存在：{script}")
            return blockers, warnings

        # 1. 游戏窗口
        hwnd, _title = find_game_window()
        if not hwnd:
            blockers.append(
                "未找到游戏窗口（标题需包含：" + "、".join(GAME_TITLE_KEYWORDS) + "）"
            )

        # 2. 屏幕分辨率（用 mss 实际截图尺寸判断，和图像匹配用的是同一来源）
        try:
            import imgfind
            shot = imgfind.screenshot()
            sh, sw = shot.shape[0], shot.shape[1]
            if (sw, sh) != RECORD_SCREEN:
                warnings.append(
                    f"当前屏幕 {sw}x{sh}，与模板图录制时的 {RECORD_SCREEN[0]}x{RECORD_SCREEN[1]} 不一致，"
                    "图像识别可能大面积失败"
                )
        except Exception as e:
            warnings.append(f"无法检测屏幕分辨率（{e}），跳过该项检查")

        # 3. 就近传送用到的模板图
        tp_dir = os.path.join(BASE_DIR, "tp_imgs")
        if info["loop_enabled"] and info["rest_img"]:
            p = os.path.join(tp_dir, info["rest_img"] + ".png")
            if not os.path.exists(p):
                blockers.append(
                    f"休息点图缺失：tp_imgs/{info['rest_img']}.png（就近传送会连续失败并熔断）"
                )
        if info["loop_enabled"]:
            p = os.path.join(tp_dir, "common_teleport_btn.png")
            if not os.path.exists(p):
                blockers.append("缺少「传送」按钮模板图：tp_imgs/common_teleport_btn.png")

        # 4. 该脚本配置的传送点是否还有效
        tp_name = self.get_script_teleport(os.path.basename(script))
        if tp_name:
            pinfo = self.get_teleport_point_info(tp_name)
            if not pinfo:
                warnings.append(f"传送点「{tp_name}」在 teleport.py 里已不存在")
            else:
                pimg = pinfo.get("point", "")
                if pimg and not os.path.exists(os.path.join(tp_dir, pimg + ".png")):
                    blockers.append(
                        f"传送点「{tp_name}」的图标图缺失：tp_imgs/{pimg}.png"
                    )

        return blockers, warnings

    def start(self, script, max_loops=0, auto_start=False, auto_start_delay=0):
        """启动脚本子进程，返回 (成功, 信息)

        max_loops：最大循环次数（0 或负数 = 无限循环），通过环境变量 MAX_LOOPS 传给脚本。

        auto_start / auto_start_delay：是否「自动开始」。脚本启动后默认处于空闲状态，
        需用户按 F8 才开始循环。当 auto_start=True 时，本方法会在脚本进程启动后
        等待 auto_start_delay 秒（给地图/加载留出时间，网络/设备不同等待时间也不同），
        再模拟按 F8 让脚本自动开始。
        """
        with self.lock:
            # 先清理可能残留的旧进程
            if self._running and self.proc is not None and self.proc.poll() is None:
                return False, "已有脚本在运行，请先停止"
            self._cleanup()

            script_path = os.path.join(BASE_DIR, script)
            if not os.path.exists(script_path):
                return False, f"脚本不存在：{script}"

            # 启动命令：源码模式 [python.exe, 脚本]；打包模式 [Lumin.exe, --run-py, 脚本]
            cmd = runtime_env.child_cmd(script_path)
            try:
                # 隐藏窗口启动脚本子进程，避免控制台窗口挡住游戏
                # 输出重定向到日志文件，既隐蔽又保留运行记录
                # 每次启动覆盖写（"w"），便于 UI 实时读取本轮完整日志。
                # buffering=1（行缓冲）：让子进程的 print 实时落到磁盘，
                # 否则 UI 的 _refresh_log 会长时间读不到新内容（表现为日志空白）。
                log_path = os.path.join(BASE_DIR, "macro_log.txt")
                self._log_file = open(log_path, "w", encoding="utf-8", buffering=1)
                # 清空上一轮的统计，保证 UI 看板只反映本次运行
                try:
                    with open(os.path.join(BASE_DIR, "runs.jsonl"), "w", encoding="utf-8"):
                        pass
                except Exception:
                    pass
                # 构建环境变量：传入循环次数
                env = dict(os.environ)
                env["MAX_LOOPS"] = str(max_loops if max_loops and max_loops > 0 else 0)
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=BASE_DIR,
                    env=env,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=self._log_file,
                    stderr=self._log_file,
                )
                self._running = True   # 启动成功，标记运行中
                # 自动开始：等待 auto_start_delay 秒后模拟按 F8，让脚本自动开始循环。
                # 放在独立线程里执行，不阻塞 start() 返回（UI 侧已用后台线程调用，
                # 但保持非阻塞更稳妥，避免影响后续流程）。
                if auto_start:
                    delay = max(0, int(auto_start_delay) if auto_start_delay else 0)
                    threading.Thread(
                        target=self._auto_start_f8, args=(delay,), daemon=True
                    ).start()
                return True, f"已启动脚本：{script}"
            except Exception as e:
                self._running = False
                self._cleanup()
                return False, f"启动失败：{e}"

    def _auto_start_f8(self, delay):
        """后台线程：等待 delay 秒后模拟按 F8 自动开始脚本。"""
        if delay > 0:
            time.sleep(delay)
        # 若期间脚本已被停止/退出，就不再按键
        if not self._running or self.proc is None or self.proc.poll() is not None:
            return
        send_f8_key()

    def _cleanup(self):
        """清理进程句柄和日志文件"""
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        self.proc = None

    def stop(self):
        """停止脚本子进程"""
        with self.lock:
            if self._running and self.proc is not None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=3)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self._running = False
                self._cleanup()
                return True, "已停止脚本"
            self._running = False
            self._cleanup()
            return False, "当前没有运行中的脚本"

    def is_running(self):
        """返回脚本是否在运行。若进程意外退出，同步更新标志。"""
        if not self._running:
            return False
        if self.proc is None:
            self._running = False
            return False
        if self.proc.poll() is None:
            return True
        # 进程已退出（如用户按 Esc），同步清空状态
        self._running = False
        self._cleanup()
        return False

    def run_teleport(self, target, on_cancel_check=None):
        """运行传送脚本并等待完成。返回 (成功, 信息)。

        on_cancel_check：可选回调，返回 True 表示用户要求取消，立即终止传送进程。

        注意：本方法是「阻塞式」的（会一直等到传送结束或超时才返回）。
        因此绝不能在 UI 主线程里直接调用（否则窗口会假死/未响应）。
        调用方应把它放到后台线程里执行，用 root.after 把结果调度回主线程更新界面。
        """
        script_path = os.path.join(BASE_DIR, TELEPORT_SCRIPT)
        if not os.path.exists(script_path):
            return False, f"传送脚本不存在：{TELEPORT_SCRIPT}"

        # 启动命令：源码模式 [python.exe, 脚本, 目标]；打包模式 [Lumin.exe, --run-py, 脚本, 目标]
        cmd = runtime_env.child_cmd(script_path, target)

        try:
            # 隐藏窗口运行传送脚本，输出写入日志文件便于排查
            log_path = os.path.join(BASE_DIR, "teleport_log.txt")
            log_file = open(log_path, "w", encoding="utf-8")
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            p = subprocess.Popen(
                cmd,
                cwd=BASE_DIR,
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            self._teleport_proc = p  # 保存句柄，供 stop_teleport() 中断

            # 非阻塞轮询，最多 90 秒；期间可被取消
            deadline = time.time() + 90
            while p.poll() is None:
                if time.time() >= deadline:
                    p.kill()
                    self._teleport_proc = None
                    log_file.close()
                    return False, "传送超时（超过 90 秒）"
                if on_cancel_check is not None and on_cancel_check():
                    p.kill()
                    self._teleport_proc = None
                    log_file.close()
                    return False, "传送已取消"
                time.sleep(0.05)
            self._teleport_proc = None
            log_file.close()

            if p.returncode == 0:
                return True, f"已传送到：{target}"
            else:
                return False, f"传送未完成（返回码 {p.returncode}）：目标点「{target}」可能未配置或已被中断"
        except Exception as e:
            self._teleport_proc = None
            return False, f"传送出错：{e}"

    def stop_teleport(self):
        """中断正在进行的传送（供「停止」按钮在传送阶段调用）。"""
        p = self._teleport_proc
        if p is not None and p.poll() is None:
            self._teleport_cancelled = True
            try:
                p.kill()
            except Exception:
                pass
            return True
        return False

    # ------------------------- 录制器管理 -------------------------
    def start_recorder(self):
        """后台启动录制器（recorder.py），返回 (成功, 信息)。

        录制器靠全局热键工作（F7 录/停、Esc 退出），
        启动后用户需自己切到游戏按 F7 录制。
        """
        with self.lock:
            # 若已有录制器在跑，直接提示
            if self._recorder_proc is not None and self._recorder_proc.poll() is None:
                return False, "录制器已在运行"

            script_path = os.path.join(BASE_DIR, RECORDER_SCRIPT)
            if not os.path.exists(script_path):
                return False, f"录制器脚本不存在：{RECORDER_SCRIPT}"

            # 启动命令：源码模式 [python.exe, 脚本]；打包模式 [Lumin.exe, --run-py, 脚本]
            cmd = runtime_env.child_cmd(script_path)

            try:
                log_path = os.path.join(BASE_DIR, "recorder_log.txt")
                log_file = open(log_path, "a", encoding="utf-8")
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE
                self._recorder_proc = subprocess.Popen(
                    cmd,
                    cwd=BASE_DIR,
                    startupinfo=startupinfo,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    stdout=log_file,
                    stderr=log_file,
                )
                return True, "录制器已启动"
            except Exception as e:
                self._recorder_proc = None
                return False, f"启动录制器失败：{e}"

    def stop_recorder(self):
        """停止录制器，返回 (成功, 信息)"""
        with self.lock:
            if self._recorder_proc is not None and self._recorder_proc.poll() is None:
                try:
                    self._recorder_proc.terminate()
                    self._recorder_proc.wait(timeout=3)
                except Exception:
                    try:
                        self._recorder_proc.kill()
                    except Exception:
                        pass
                self._recorder_proc = None
                return True, "已停止录制器"
            self._recorder_proc = None
            return False, "录制器未在运行"

    def is_recorder_running(self):
        """录制器是否在运行"""
        if self._recorder_proc is None:
            return False
        if self._recorder_proc.poll() is None:
            return True
        self._recorder_proc = None
        return False

    def scan_recorded_scripts(self):
        """扫描 recordings/ 目录，返回录制脚本列表（相对路径）。

        返回 [(显示名, 脚本文件名), ...]，供 UI 动态加入下拉列表。
        显示名优先取 names.json 里用户自定义的名字，否则用文件名。
        """
        rec_dir = os.path.join(BASE_DIR, RECORD_DIR)
        result = []
        if not os.path.isdir(rec_dir):
            return result
        custom_names = self.load_script_names()
        for fname in sorted(os.listdir(rec_dir)):
            if not fname.endswith(".py"):
                continue
            # 显示名：优先自定义，否则用文件名
            display = custom_names.get(fname, fname)
            result.append((display, fname))
        return result

    def load_script_names(self):
        """读取 names.json 里的自定义显示名映射，返回 {文件名: 显示名}。

        文件不存在或损坏时返回空字典（不报错，降级为用文件名显示）。
        """
        path = os.path.join(BASE_DIR, NAMES_FILE)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def save_script_name(self, script_fname, display_name):
        """保存某个录制脚本的自定义显示名到 names.json，返回 (成功, 信息)。

        script_fname：脚本文件名（如 20261103_110711.py）
        display_name：自定义显示名
        """
        path = os.path.join(BASE_DIR, NAMES_FILE)
        data = self.load_script_names()
        data[script_fname] = display_name
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True, f"已重命名为：{display_name}"
        except Exception as e:
            return False, f"保存名称失败：{e}"

    def clear_script_name(self, script_fname):
        """清除某个录制脚本的自定义显示名（恢复用文件名显示），返回 (成功, 信息)。"""
        path = os.path.join(BASE_DIR, NAMES_FILE)
        data = self.load_script_names()
        if script_fname in data:
            del data[script_fname]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True, "已恢复默认文件名"
        except Exception as e:
            return False, f"保存失败：{e}"

    def delete_recorded_script(self, script_fname):
        """删除一个录制脚本及其关联文件，返回 (成功, 信息)。

        会一并删除：
        - recordings/<script_fname>（脚本文件本身）
        - recordings/<base>.json（录制原始数据，若有）
        - tp_imgs/<base>_rest_point.png（就近传送休息点图，若有）
        - tp_imgs/<base>_point.png（传送点图标图，若有）
        并清理 names.json / teleports.json 里该脚本的映射。

        注意：base 取脚本文件名的去 .py 部分（即录制时间戳）。
        """
        script_path = os.path.join(BASE_DIR, RECORD_DIR, script_fname)
        if not os.path.exists(script_path):
            return False, f"脚本文件不存在：{script_fname}"

        base = os.path.splitext(script_fname)[0]  # 录制时间戳，如 20261103_110711
        tp_dir = os.path.join(BASE_DIR, "tp_imgs")

        deleted = []
        errors = []

        # 1. 删除脚本文件
        try:
            os.remove(script_path)
            deleted.append(script_fname)
        except Exception as e:
            errors.append(f"脚本文件：{e}")

        # 1a. 删除该脚本的 pyc 缓存（避免删除后残留旧字节码）
        self._clear_pyc(script_path)

        # 1b. 删除录制原始 JSON 数据（若有，文件名与脚本同时间戳）
        json_path = os.path.join(BASE_DIR, RECORD_DIR, base + ".json")
        if os.path.exists(json_path):
            try:
                os.remove(json_path)
                deleted.append(base + ".json")
            except Exception as e:
                errors.append(f"{base}.json：{e}")

        # 2. 删除关联的休息点图（脚本专属，删脚本时一并删）。
        #    注意：**_point.png（传送点图标）不能删！** 它是「全局共享」的，
        #    存放在 teleport.py 的 TELEPORT_POINTS 里、可能被其它脚本绑定复用。
        #    若删脚本时把它删了，teleport.py 里该传送点的 point 仍指向这张图，
        #    会导致后续启动检查报「传送点图标图缺失」、传送失败。
        #    因此这里只删 _rest_point.png（就近传送休息点图，脚本专属），
        #    传送点图标图留给 teleport.py 的条目继续引用。
        rest_img_path = os.path.join(tp_dir, base + "_rest_point.png")
        if os.path.exists(rest_img_path):
            try:
                os.remove(rest_img_path)
                deleted.append(os.path.basename(rest_img_path))
            except Exception as e:
                errors.append(f"{os.path.basename(rest_img_path)}：{e}")

        # 3. 清理 names.json 里的显示名映射
        self.clear_script_name(script_fname)

        # 4. 清理 teleports.json 里的传送点映射
        self.clear_script_teleport(script_fname)

        if errors:
            return False, "部分文件删除失败：\n" + "\n".join(errors) + "\n\n已删除：\n" + "\n".join(deleted)
        return True, "已删除：" + "、".join(deleted)

    # ------------------------- 导入 / 导出 -------------------------
    def export_script_zip(self, script_fname, zip_path):
        """把一个录制脚本连同它依赖的所有文件打包成 zip，返回 (成功, 信息)。

        打包内容：
        - recordings/<script_fname>（脚本本体）
        - recordings/<base>.json（录制原始数据，若有）
        - tp_imgs/<base>_rest_point.png（就近传送休息点图，若有）
        - tp_imgs/<base>_point.png / _confirm.png（脚本专属传送点图标图，若有）
        - 该脚本绑定的传送点所依赖的图标图（point / confirm）及其地区入口图（common_*）
        - manifest.json（记录显示名、传送点配置、关联图片清单，供导入时还原）

        zip 内保持 recordings/ 与 tp_imgs/ 两个目录结构，导入时按结构还原。
        """
        try:
            import zipfile
        except Exception as e:
            return False, f"导入 zipfile 失败：{e}"

        base = os.path.splitext(script_fname)[0]
        rec_dir = os.path.join(BASE_DIR, RECORD_DIR)
        tp_dir = os.path.join(BASE_DIR, "tp_imgs")

        # 脚本本体必须存在
        script_path = os.path.join(rec_dir, script_fname)
        if not os.path.exists(script_path):
            return False, f"脚本文件不存在：{script_fname}"

        # 汇总需要打包的文件：zip 内相对路径 -> 磁盘绝对路径
        files = {}  # {zip 内相对路径: 磁盘绝对路径}
        def _add(rel, abs_path):
            if os.path.exists(abs_path):
                files[rel] = abs_path

        _add(f"recordings/{script_fname}", script_path)
        _add(f"recordings/{base}.json", os.path.join(rec_dir, base + ".json"))
        _add(f"tp_imgs/{base}_rest_point.png", os.path.join(tp_dir, base + "_rest_point.png"))
        _add(f"tp_imgs/{base}_point.png", os.path.join(tp_dir, base + "_point.png"))
        _add(f"tp_imgs/{base}_point_confirm.png", os.path.join(tp_dir, base + "_point_confirm.png"))

        # 传送点配置：脚本绑定的传送点 -> 其依赖的图片
        teleport_name = self.get_script_teleport(script_fname)
        teleport_entry = None
        if teleport_name:
            pinfo = self.get_teleport_point_info(teleport_name)
            if pinfo:
                teleport_entry = {"name": teleport_name, **pinfo}
                pimg = pinfo.get("point", "")
                if pimg:
                    _add(f"tp_imgs/{pimg}.png", os.path.join(tp_dir, pimg + ".png"))
                cimg = pinfo.get("confirm", "")
                if cimg:
                    _add(f"tp_imgs/{cimg}.png", os.path.join(tp_dir, cimg + ".png"))
                # 该地区入口图（common_* 系列，跨区域传送的公共模板）
                region = pinfo.get("region", "")
                try:
                    import teleport
                    entrances = teleport.REGION_ENTRANCES.get(region, [])
                    if isinstance(entrances, str):
                        entrances = [entrances]
                    for ent in entrances:
                        _add(f"tp_imgs/{ent}.png", os.path.join(tp_dir, ent + ".png"))
                except Exception:
                    pass
            else:
                # 绑定了传送点但 teleport.py 里已不存在：仍记录名字，导入时提示
                teleport_entry = {"name": teleport_name, "region": "", "point": "", "confirm": ""}

        # 显示名
        custom_names = self.load_script_names()
        display_name = custom_names.get(script_fname, "")

        # manifest
        manifest = {
            "format": "lumin-script",
            "version": 1,
            "script": script_fname,
            "base": base,
            "display_name": display_name,
            "teleport": teleport_entry,   # None 表示未配置传送点
            "files": sorted(files.keys()),
        }

        # 写 zip
        try:
            os.makedirs(os.path.dirname(zip_path) or ".", exist_ok=True)
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for rel, abs_path in files.items():
                    zf.write(abs_path, rel)
        except Exception as e:
            return False, f"写入 zip 失败：{e}"

        total = len(files) + 1
        return True, f"已导出 {script_fname}（含 {total} 个文件）到：\n{zip_path}"

    def import_script_zip(self, zip_path):
        """导入一个脚本 zip，还原文件、重建配置，返回 (成功, 信息)。

        流程：
        1. 校验 zip 结构和 manifest 合法性（防止路径穿越、非本格式文件）
        2. 检测脚本重名冲突（recordings 下已有同名脚本则提示覆盖/改名）
        3. 解压文件到 recordings/ 与 tp_imgs/（保持结构）
        4. 还原显示名（names.json）与传送点绑定（teleports.json）
        5. 若传送点在本机 teleport.py 不存在，则一并新增（连同 region/point/confirm）
        6. 校验脚本引用的图片路径，修正脚本内 LOCAL_REST_POINT_IMG 等
        """
        try:
            import zipfile
        except Exception as e:
            return False, f"导入 zipfile 失败：{e}"

        if not os.path.exists(zip_path):
            return False, f"zip 文件不存在：{zip_path}"

        rec_dir = os.path.join(BASE_DIR, RECORD_DIR)
        tp_dir = os.path.join(BASE_DIR, "tp_imgs")

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                # 1. 读取 manifest
                if "manifest.json" not in names:
                    return False, "不是有效的脚本导出包（缺少 manifest.json）"
                try:
                    manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                except Exception as e:
                    return False, f"manifest.json 解析失败：{e}"
                if manifest.get("format") != "lumin-script":
                    return False, "不是本工具的脚本导出包（format 不匹配）"

                script_fname = manifest.get("script", "")
                if not script_fname or not script_fname.endswith(".py"):
                    return False, "manifest 里的 script 字段非法"
                # 防路径穿越：script 只取 basename
                script_fname = os.path.basename(script_fname)
                base = os.path.splitext(script_fname)[0]

                # 2. 校验 zip 内文件路径合法（只允许 recordings/ 和 tp_imgs/ 两个前缀）
                allowed = ("recordings/", "tp_imgs/", "manifest.json")
                for n in names:
                    if n == "manifest.json":
                        continue
                    if not n.startswith(allowed[:2]):
                        return False, f"zip 内含非法路径：{n}"

                # 3. 重名检测
                target_script = os.path.join(rec_dir, script_fname)
                if os.path.exists(target_script):
                    # 返回特殊标记，让 UI 决定是否覆盖
                    return False, ("__CONFLICT__", script_fname)

                # 4. 解压
                written = []
                for n in names:
                    if n == "manifest.json":
                        continue
                    # 安全：只取 basename 拼接，杜绝 ../ 穿越
                    if n.startswith("recordings/"):
                        dest = os.path.join(rec_dir, os.path.basename(n))
                    elif n.startswith("tp_imgs/"):
                        dest = os.path.join(tp_dir, os.path.basename(n))
                    else:
                        continue
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with zf.open(n) as src, open(dest, "wb") as dst:
                        dst.write(src.read())
                    written.append(os.path.basename(n))
        except zipfile.BadZipFile:
            return False, "zip 文件损坏或不是有效的 zip"
        except Exception as e:
            return False, f"解压失败：{e}"

        # 5. 还原显示名
        if manifest.get("display_name"):
            self.save_script_name(script_fname, manifest["display_name"])

        # 6. 还原传送点配置
        teleport_entry = manifest.get("teleport")
        if teleport_entry and teleport_entry.get("name"):
            tp_name = teleport_entry["name"]
            region = teleport_entry.get("region", "")
            point = teleport_entry.get("point", "")
            confirm = teleport_entry.get("confirm", "")
            # 若本机没有该传送点，则新增（连同其图标图已在 zip 里一并解压）
            if region and point:
                existing = self.get_teleport_point_info(tp_name)
                if not existing:
                    ok, msg = self.add_teleport_point(tp_name, region, point, confirm or None)
                    if not ok:
                        # 传送点新增失败不阻断整体导入，仅在信息里提示
                        pass
                # 绑定脚本 -> 传送点
                self.save_script_teleport(script_fname, tp_name)
            elif region or point:
                # 传送点信息不全（导出时 teleport.py 已缺），仅提示
                pass

        # 7. 校验：脚本引用的图片是否都已就位
        missing = []
        tp_name = manifest.get("teleport", {}).get("name", "") if manifest.get("teleport") else ""
        pinfo = self.get_teleport_point_info(tp_name) if tp_name else None
        if pinfo:
            for key in ("point", "confirm"):
                img = pinfo.get(key, "")
                if img and not os.path.exists(os.path.join(tp_dir, img + ".png")):
                    missing.append(f"tp_imgs/{img}.png")
        # 休息点图
        info = self._script_info(target_script)
        if info.get("rest_img") and not os.path.exists(
            os.path.join(tp_dir, info["rest_img"] + ".png")
        ):
            missing.append(f"tp_imgs/{info['rest_img']}.png")

        written_summary = "、".join(written[:5]) + ("…" if len(written) > 5 else "")
        if missing:
            return True, (
                f"已导入 {script_fname}（{len(written)} 个文件：{written_summary}）\n\n"
                f"⚠ 以下图片缺失，脚本可能无法正常传送：\n" + "\n".join("  · " + m for m in missing)
            )
        return True, f"已导入 {script_fname}（{len(written)} 个文件：{written_summary}）"

    def force_import_script_zip(self, zip_path):
        """覆盖导入：先删除同名脚本再导入（用于用户确认覆盖重名脚本的场景）。"""
        import zipfile
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            script_fname = os.path.basename(manifest.get("script", ""))
        except Exception:
            return False, "无法读取 zip 的 manifest，无法覆盖导入"
        base = os.path.splitext(script_fname)[0]
        # 删除同名脚本及其专属文件（复用 delete_recorded_script 的清理逻辑）
        if os.path.exists(os.path.join(BASE_DIR, RECORD_DIR, script_fname)):
            self.delete_recorded_script(script_fname)
        # 删除可能残留的专属图片（delete 已删 rest_point，这里补删 point/confirm）
        tp_dir = os.path.join(BASE_DIR, "tp_imgs")
        for suf in ("_point.png", "_point_confirm.png"):
            p = os.path.join(tp_dir, base + suf)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        return self.import_script_zip(zip_path)

    # ------------------------- 传送点配置 -------------------------
    def load_script_teleports(self):
        """读取 teleports.json 里的录制脚本传送点映射，返回 {脚本文件名: 传送点名}。"""
        path = os.path.join(BASE_DIR, TELEPORTS_FILE)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def save_script_teleport(self, script_fname, teleport_name):
        """保存某个录制脚本的传送点，返回 (成功, 信息)。

        script_fname：脚本文件名（如 20261103_110711.py）
        teleport_name：传送点名（如 淬火池），须是 teleport.py 里已配置的传送点。
        """
        path = os.path.join(BASE_DIR, TELEPORTS_FILE)
        data = self.load_script_teleports()
        data[script_fname] = teleport_name
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True, f"已配置传送点：{teleport_name}"
        except Exception as e:
            return False, f"保存传送点失败：{e}"

    def clear_script_teleport(self, script_fname):
        """清除某个录制脚本的传送点配置，返回 (成功, 信息)。"""
        path = os.path.join(BASE_DIR, TELEPORTS_FILE)
        data = self.load_script_teleports()
        if script_fname in data:
            del data[script_fname]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True, "已清除传送点"
        except Exception as e:
            return False, f"保存失败：{e}"

    def get_available_teleport_points(self):
        """返回 teleport.py 里已配置的传送点列表（供 UI 下拉选择）。

        直接 import teleport 读 TELEPORT_POINTS 的 key；失败时返回空列表。

        注意：这里**不要**每次 reload。import teleport 会连带加载 cv2 并实例化
        pynput 的 Controller（实测约 1.9 秒），而本方法会被 UI 在事件循环线程里
        反复调用（每次点「配置传送点」会连调两三次）。若每次都 reload，会反复
        阻塞事件循环，导致后续文件选择器无法响应、session 关闭（界面卡死）。
        缓存由 add_teleport_point 改写文件后统一失效，读的时候直接用缓存即可。
        """
        try:
            import teleport
            return list(teleport.TELEPORT_POINTS.keys())
        except Exception:
            return []

    def get_script_teleport(self, script_fname):
        """返回某个录制脚本配置的传送点名（无配置返回空字符串）。"""
        return self.load_script_teleports().get(script_fname, "")

    def get_teleport_point_info(self, teleport_name):
        """返回某个传送点的配置信息 {region, point, confirm}，不存在返回 None。

        用于配置对话框回显：用户修改传送点地区/名称时，正确回显当前地区、传送点图
        和相邻确认图，并在用户不重新选图时保留原来的图。
        """
        if not teleport_name:
            return None
        try:
            import teleport
            info = teleport.TELEPORT_POINTS.get(teleport_name)
            if info:
                return {
                    "region": info.get("region", ""),
                    "point": info.get("point", ""),
                    "confirm": info.get("confirm", ""),
                }
        except Exception:
            pass
        return None

    def get_available_regions(self):
        """返回世界地图五个地区的列表（供 UI 下拉选择）。

        直接 import teleport 读 REGION_ENTRANCES 的 key；失败时返回内置默认列表。

        注意：同 get_available_teleport_points，这里**不要**每次 reload，避免在
        事件循环线程里反复 import teleport（含 cv2 + pynput 实例化）阻塞 UI。
        """
        try:
            import teleport
            return list(teleport.REGION_ENTRANCES.keys())
        except Exception:
            return ["纽特基地", "瓦尔肯要塞", "巨坑底部", "因弗内斯巨坑", "奥鲁姆要塞"]

    def add_teleport_point(self, teleport_name, region, point_img_name, confirm_img_name=None):
        """往 teleport.py 的 TELEPORT_POINTS 里新增一个传送点，返回 (成功, 信息)。

        参数：
            teleport_name  传送点名（中文，作为 TELEPORT_POINTS 的 key）
            region         所在地区（须在 REGION_ENTRANCES 里）
            point_img_name 传送点图标模板名（tp_imgs/ 下，不含 .png）
            confirm_img_name 可选：相邻确认图模板名（tp_imgs/ 下，不含 .png）。
                             缩放后传送点附近有休息点/监测塔/锚定点/残片等相邻图标时，
                             用这张图做二次定位确认，避免误点相邻元素。

        通过字符串定位 TELEPORT_POINTS 字典并插入新条目，不改动其它代码。
        """
        path = os.path.join(BASE_DIR, TELEPORT_SCRIPT)
        if not os.path.exists(path):
            return False, f"传送脚本不存在：{TELEPORT_SCRIPT}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return False, f"读取 teleport.py 失败：{e}"

        # 构造条目的 value 部分：point 必有，confirm 可选
        def _entry_value():
            if confirm_img_name:
                return (f'{{"region": "{region}", "point": "{point_img_name}", '
                        f'"confirm": "{confirm_img_name}"}}')
            return f'{{"region": "{region}", "point": "{point_img_name}"}}'

        # 已存在同名传送点：更新其 point 图片（用户可能重新截了图，旧图已失效）
        # 注意：不复用旧图，而是把 region/point/confirm 更新为本次传入的新值，
        # 否则用户重新配置传送点（换了新图）时，teleport.py 仍指向旧图导致传送失败。
        name_pattern = f'"{teleport_name}":'
        name_idx = content.find(name_pattern)
        if name_idx != -1:
            # 定位该条目的 "point": "xxx" 并替换为新的图片名
            line_end = content.find("\n", name_idx)
            if line_end == -1:
                line_end = len(content)
            new_entry = f'"{teleport_name}": {_entry_value()},'
            content = content[:name_idx] + new_entry + content[line_end:]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                return False, f"更新 teleport.py 失败：{e}"
            self._clear_pyc(path)
            self._sync_teleport_in_memory(teleport_name, region, point_img_name, confirm_img_name)
            return True, f"传送点「{teleport_name}」已更新（地区：{region}）"

        # 定位 TELEPORT_POINTS 字典：找到 "TELEPORT_POINTS = {" 到与之匹配的 "}"
        marker = "TELEPORT_POINTS = {"
        start = content.find(marker)
        if start == -1:
            return False, "未找到 TELEPORT_POINTS 定义"
        # 从 { 开始找配对的 }
        brace_idx = content.find("{", start)
        depth = 0
        end = -1
        for i in range(brace_idx, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end == -1:
            return False, "TELEPORT_POINTS 字典格式异常，无法定位"

        # 在字典内最后一个条目后插入新行
        # 新条目格式与现有保持一致（4 空格缩进 + key: {"region":..., "point":...}）
        new_line = f'    "{teleport_name}": {_entry_value()},\n'
        # 找到 end 前的位置：end 是字典的 "}"，其前面可能是最后一个条目的 ",\n" 或 "}"
        insert_pos = end
        # 跳过 end 前面已有的空白，把新条目插在 "}" 之前
        content = content[:insert_pos] + new_line + content[insert_pos:]

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return False, f"写入 teleport.py 失败：{e}"
        # 清掉 teleport.py 的 pyc 缓存 + 同步内存里的 TELEPORT_POINTS，确保改动立即生效
        self._clear_pyc(path)
        self._sync_teleport_in_memory(teleport_name, region, point_img_name, confirm_img_name)
        return True, f"已新增传送点「{teleport_name}」（地区：{region}）"

    def set_teleport_confirm(self, teleport_name, confirm_img_name):
        """独立设置/更新某个传送点的「相邻确认图」，返回 (成功, 信息)。

        与 add_teleport_point 分离：后者负责 region + point（传送点图），本方法
        只负责 confirm 字段（相邻确认图），二者是独立的配置步骤，避免在同一个
        文件选择器流程里连续弹两次图造成混淆。

        参数：
            teleport_name    传送点名（须已在 TELEPORT_POINTS 里）
            confirm_img_name 相邻确认图模板名（tp_imgs/ 下，不含 .png）；
                             传 None 或空串表示清除该传送点的相邻确认图。
        """
        path = os.path.join(BASE_DIR, TELEPORT_SCRIPT)
        if not os.path.exists(path):
            return False, f"传送脚本不存在：{TELEPORT_SCRIPT}"
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return False, f"读取 teleport.py 失败：{e}"

        # 定位该传送点条目
        name_pattern = f'"{teleport_name}":'
        name_idx = content.find(name_pattern)
        if name_idx == -1:
            return False, f"传送点「{teleport_name}」不存在，请先配置传送点"

        line_end = content.find("\n", name_idx)
        if line_end == -1:
            line_end = len(content)
        old_line = content[name_idx:line_end]

        # 读取该条目当前的 region / point，保留不变，只改 confirm
        try:
            import teleport
            info = teleport.TELEPORT_POINTS.get(teleport_name) or {}
        except Exception:
            info = {}
        region = info.get("region", "")
        point = info.get("point", "")
        # 若内存里没有（模块未加载），回退从旧行文本里提取 region/point
        if not region or not point:
            import re
            m = re.search(r'"region":\s*"([^"]*)"', old_line)
            region = m.group(1) if m else region
            m = re.search(r'"point":\s*"([^"]*)"', old_line)
            point = m.group(1) if m else point

        # 构造新条目：region/point 不变，confirm 按需写入或省略
        if confirm_img_name:
            new_entry = (f'"{teleport_name}": {{"region": "{region}", "point": "{point}", '
                         f'"confirm": "{confirm_img_name}"}},')
        else:
            new_entry = f'"{teleport_name}": {{"region": "{region}", "point": "{point}"}},'

        content = content[:name_idx] + new_entry + content[line_end:]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return False, f"更新 teleport.py 失败：{e}"

        self._clear_pyc(path)
        # 同步内存：保留原 region/point，只改 confirm
        self._sync_teleport_in_memory(teleport_name, region, point, confirm_img_name)
        if confirm_img_name:
            return True, f"已为传送点「{teleport_name}」配置相邻确认图"
        return True, f"已清除传送点「{teleport_name}」的相邻确认图"

    # ------------------------- 计划（编排任务）管理 -------------------------
    def load_plans(self):
        """读取 plans.json 里的所有方案，返回 {方案名: 方案数据}。

        方案数据形如 {"steps": [{"script": "recordings/xxx.py", "loops": 30}, ...]}
        文件不存在或损坏时返回空字典。
        """
        path = os.path.join(BASE_DIR, PLANS_FILE)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def save_plan(self, plan_name, steps):
        """保存（新增或覆盖）一个方案，返回 (成功, 信息)。

        plan_name：方案名（作为 key）
        steps：步骤列表，每个元素 {"script": 相对路径, "loops": 循环次数}
               传送点不在此处存储，运行时用 get_script_teleport() 取脚本已配置的传送点。
        """
        plan_name = (plan_name or "").strip()
        if not plan_name:
            return False, "方案名不能为空"
        if not steps:
            return False, "方案至少需要一个步骤"
        # 规范化步骤：loops 转 int（<=0 视为 1，避免「跑 0 次」这种无意义配置）
        normalized = []
        for s in steps:
            script = (s.get("script") or "").strip()
            try:
                loops = int(s.get("loops", 0))
            except (TypeError, ValueError):
                loops = 0
            if loops <= 0:
                loops = 1
            if not script:
                return False, "步骤中存在未指定脚本的项"
            normalized.append({"script": script, "loops": loops})

        path = os.path.join(BASE_DIR, PLANS_FILE)
        data = self.load_plans()
        data[plan_name] = {"steps": normalized}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True, f"已保存方案「{plan_name}」（{len(normalized)} 个步骤）"
        except Exception as e:
            return False, f"保存方案失败：{e}"

    def delete_plan(self, plan_name):
        """删除一个方案，返回 (成功, 信息)。"""
        path = os.path.join(BASE_DIR, PLANS_FILE)
        data = self.load_plans()
        if plan_name not in data:
            return False, f"方案「{plan_name}」不存在"
        del data[plan_name]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True, f"已删除方案「{plan_name}」"
        except Exception as e:
            return False, f"删除方案失败：{e}"

    def is_plan_running(self):
        """计划是否在运行。"""
        return self._plan_running

    def request_plan_stop(self):
        """请求停止正在运行的计划（供 UI 的「停止」按钮调用）。"""
        self._plan_cancelled = True

    def stop_plan_now(self):
        """立即停止计划：终止当前正在运行的脚本/传送进程，并取消计划。"""
        self._plan_cancelled = True
        # 中断正在进行的传送
        if self._teleport_proc is not None:
            try:
                self.stop_teleport()
            except Exception:
                pass
        # 终止正在运行的脚本
        if self._running and self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self._running = False
        self._cleanup()

    def run_plan(self, plan_name, progress_cb=None, auto_start=False, auto_start_delay=0):
        """执行一个计划（阻塞式，须在后台线程调用）。

        依次执行方案的每个步骤：传送（若配置）→ 启动脚本跑 N 轮 → 等脚本自然结束。
        每步之间脚本进程由 MAX_LOOPS 控制自然退出，本方法通过轮询 is_running() 检测。

        参数：
            plan_name   方案名
            progress_cb 可选回调 progress_cb(dict)，每进入一步 / 状态变化时调用，
                        传入 {"phase": "step_started"|"step_done"|"teleporting"|"running"|"finished"|"cancelled",
                              "index": 当前步骤序号(0-based), "total": 总步数,
                              "msg": 人类可读信息}。回调在后台线程调用，UI 侧需自行线程安全。
            auto_start / auto_start_delay：每一步脚本是否「自动开始」（传送完成后
                        等待 auto_start_delay 秒自动按 F8），见 start() 同名参数。
        返回 (成功, 信息)。
        """
        data = self.load_plans()
        plan = data.get(plan_name)
        if not plan:
            return False, f"方案「{plan_name}」不存在"
        steps = plan.get("steps", [])
        if not steps:
            return False, "方案没有步骤"

        self._plan_running = True
        self._plan_cancelled = False
        total = len(steps)

        def _emit(phase, index, msg):
            if progress_cb:
                try:
                    progress_cb({
                        "phase": phase, "index": index, "total": total, "msg": msg,
                    })
                except Exception:
                    pass

        try:
            for i, step in enumerate(steps):
                if self._plan_cancelled:
                    _emit("cancelled", i, f"计划已取消（第 {i+1}/{total} 步前）")
                    return False, "计划已取消"

                script = step["script"]
                loops = step.get("loops", 1)

                script_path = os.path.join(BASE_DIR, script)
                if not os.path.exists(script_path):
                    _emit("step_done", i, f"第 {i+1}/{total} 步：脚本不存在 {script}，跳过")
                    continue

                # 传送点直接取该脚本「已配置」的传送点（teleports.json），
                # 计划步骤里不单独存传送点，避免配置重复、也保证与脚本配置一致。
                teleport = self.get_script_teleport(os.path.basename(script))

                _emit("step_started", i, f"第 {i+1}/{total} 步：{script}（{loops} 轮）")

                # 0. 先把游戏窗口切到前台（与单脚本运行流程一致）。
                #    注意：传送脚本内部也会 focus_game_window，但若本步脚本
                #    没有配置传送点（teleport 为空）就会跳过传送直接启动脚本，
                #    此时必须由这里负责切窗口，否则焦点会停在 UI 上。
                focus_game_window()

                # 1. 传送（每步强制传送，用脚本已配置的传送点）
                if teleport:
                    _emit("teleporting", i, f"第 {i+1}/{total} 步：传送到「{teleport}」……")
                    ok_tp, msg_tp = self.run_teleport(
                        teleport, on_cancel_check=lambda: self._plan_cancelled
                    )
                    if not ok_tp:
                        if self._plan_cancelled:
                            _emit("cancelled", i, "计划已取消")
                            return False, "计划已取消"
                        _emit("step_done", i, f"第 {i+1}/{total} 步：传送失败（{msg_tp}），跳过")
                        continue

                # 2. 启动脚本跑 loops 轮
                _emit("running", i, f"第 {i+1}/{total} 步：运行 {script}（{loops} 轮）……")
                ok_start, msg_start = self.start(
                    script, max_loops=loops,
                    auto_start=auto_start, auto_start_delay=auto_start_delay,
                )
                if not ok_start:
                    _emit("step_done", i, f"第 {i+1}/{total} 步：启动失败（{msg_start}），跳过")
                    continue

                # 3. 等脚本自然跑完（进程退出）
                while self.is_running():
                    if self._plan_cancelled:
                        self.stop_plan_now()
                        _emit("cancelled", i, "计划已取消")
                        return False, "计划已取消"
                    time.sleep(0.3)

                _emit("step_done", i, f"第 {i+1}/{total} 步：完成")

            _emit("finished", total, f"计划「{plan_name}」全部完成（{total} 步）")
            return True, f"计划「{plan_name}」全部完成（{total} 步）"
        finally:
            self._plan_running = False
            self._plan_cancelled = False

