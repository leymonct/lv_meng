# -*- coding: utf-8 -*-
"""
脚本管理器 —— 后端逻辑（纯逻辑，不含界面）
============================================
本模块只负责后端能力，供 Flet 版界面（macro_ui_flet.py）复用，本身不依赖任何
GUI 库（tkinter 等），因此可以安全地在无 GUI 的打包环境下 import。

包含：
    - is_admin / relaunch_as_admin  权限检查与提权
    - find_game_window / focus_game_window  游戏窗口定位与前台切换
    - MacroManager  脚本子进程 / 传送 / 录制器的启动、停止与管理
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
import shutil

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
    def _reload_teleport_module():
        """让内存里的 teleport 模块失效，下次 import 时重新从磁盘加载。

        因为 get_available_regions / get_available_teleport_points 用 `import teleport` 读配置，
        一旦 add_teleport_point 改写了 teleport.py，已缓存的模块就会是旧数据。
        这里把它从 sys.modules 移除，下次 import 即重新加载。
        """
        try:
            sys.modules.pop("teleport", None)
        except Exception:
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

    def start(self, script, max_loops=0):
        """启动脚本子进程，返回 (成功, 信息)

        max_loops：最大循环次数（0 或负数 = 无限循环），通过环境变量 MAX_LOOPS 传给脚本。
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
                return True, f"已启动脚本：{script}"
            except Exception as e:
                self._running = False
                self._cleanup()
                return False, f"启动失败：{e}"

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

        # 2. 删除关联的休息点图 / 传送点图（仅当是"按时间戳命名"的图才删，避免误删共享图）
        for suffix in ("_rest_point.png", "_point.png"):
            img_path = os.path.join(tp_dir, base + suffix)
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                    deleted.append(os.path.basename(img_path))
                except Exception as e:
                    errors.append(f"{os.path.basename(img_path)}：{e}")

        # 3. 清理 names.json 里的显示名映射
        self.clear_script_name(script_fname)

        # 4. 清理 teleports.json 里的传送点映射
        self.clear_script_teleport(script_fname)

        if errors:
            return False, "部分文件删除失败：\n" + "\n".join(errors) + "\n\n已删除：\n" + "\n".join(deleted)
        return True, "已删除：" + "、".join(deleted)

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
        为读到最新配置，先清掉可能存在的旧模块缓存再 import。
        """
        self._reload_teleport_module()
        try:
            import teleport
            return list(teleport.TELEPORT_POINTS.keys())
        except Exception:
            return []

    def get_script_teleport(self, script_fname):
        """返回某个录制脚本配置的传送点名（无配置返回空字符串）。"""
        return self.load_script_teleports().get(script_fname, "")

    def get_teleport_point_info(self, teleport_name):
        """返回某个传送点的配置信息 {region, point}，不存在返回 None。

        用于配置对话框回显：用户修改传送点地区/名称时，正确回显当前地区，
        并在用户不重新选图时保留原来的 point 图。
        """
        if not teleport_name:
            return None
        self._reload_teleport_module()
        try:
            import teleport
            info = teleport.TELEPORT_POINTS.get(teleport_name)
            if info:
                return {"region": info.get("region", ""), "point": info.get("point", "")}
        except Exception:
            pass
        return None

    def get_available_regions(self):
        """返回世界地图五个地区的列表（供 UI 下拉选择）。

        直接 import teleport 读 REGION_ENTRANCES 的 key；失败时返回内置默认列表。
        为读到最新配置，先清掉可能存在的旧模块缓存再 import。
        """
        self._reload_teleport_module()
        try:
            import teleport
            return list(teleport.REGION_ENTRANCES.keys())
        except Exception:
            return ["纽特基地", "瓦尔肯要塞", "巨坑底部", "因弗内斯巨坑", "奥鲁姆要塞"]

    def add_teleport_point(self, teleport_name, region, point_img_name):
        """往 teleport.py 的 TELEPORT_POINTS 里新增一个传送点，返回 (成功, 信息)。

        参数：
            teleport_name  传送点名（中文，作为 TELEPORT_POINTS 的 key）
            region         所在地区（须在 REGION_ENTRANCES 里）
            point_img_name 传送点图标模板名（tp_imgs/ 下，不含 .png）

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

        # 已存在同名传送点：更新其 point 图片（用户可能重新截了图，旧图已失效）
        # 注意：不复用旧图，而是把 region/point 更新为本次传入的新值，
        # 否则用户重新配置传送点（换了新图）时，teleport.py 仍指向旧图导致传送失败。
        name_pattern = f'"{teleport_name}":'
        name_idx = content.find(name_pattern)
        if name_idx != -1:
            # 定位该条目的 "point": "xxx" 并替换为新的图片名
            line_end = content.find("\n", name_idx)
            if line_end == -1:
                line_end = len(content)
            old_entry = content[name_idx:line_end]
            new_entry = f'"{teleport_name}": {{"region": "{region}", "point": "{point_img_name}"}},'
            content = content[:name_idx] + new_entry + content[line_end:]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                return False, f"更新 teleport.py 失败：{e}"
            self._clear_pyc(path)
            self._reload_teleport_module()
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
        new_line = (
            f'    "{teleport_name}": {{"region": "{region}", "point": "{point_img_name}"}},\n'
        )
        # 找到 end 前的位置：end 是字典的 "}"，其前面可能是最后一个条目的 ",\n" 或 "}"
        insert_pos = end
        # 跳过 end 前面已有的空白，把新条目插在 "}" 之前
        content = content[:insert_pos] + new_line + content[insert_pos:]

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return False, f"写入 teleport.py 失败：{e}"
        # 清掉 teleport.py 的 pyc 缓存 + 让内存模块失效，确保改动立即生效
        self._clear_pyc(path)
        self._reload_teleport_module()
        return True, f"已新增传送点「{teleport_name}」（地区：{region}）"


