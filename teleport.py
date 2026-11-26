# -*- coding: utf-8 -*-
"""
传送脚本 —— 传送到目标地图（图片识别定位版）
================================
【作用】
    由 UI 界面在「启动刷怪脚本」之前调用。
    用「图片识别」在屏幕上定位并点击元素，不再依赖固定坐标，
    能适应游戏窗口位置/大小的变化。

【调用方式】
    python teleport.py 淬火池

【如何新增一个传送点】
    通过 UI「配置传送点」操作（会写入 teleport_user.py 的 TELEPORT_POINTS），
    或手动编辑 teleport_user.py：
    1. 截一张「传送点图标」小图，存到 tp_imgs/（用拼音命名，如 xxx_point.png）
    2. 在 teleport_user.py 的 TELEPORT_POINTS 字典里加一行：
           "传送点名": {"region": "所在小地图名", "point": "xxx_point"},
       —— 只需这一行！「打开地图→点世界→点小地图→缩小→点传送点→点传送」
          这条公共链路会自动拼接，无需复制动作序列。
    3. 若该传送点在「新小地图」里，先在 REGION_ENTRANCES 里给这个新小地图
       配一张入口图（点世界地图后选中它）。
    4. 可选：若缩放地图后该传送点附近紧挨着休息点/监测塔/锚定点/残片等图标，
       单靠传送点图容易误点相邻元素，可再加一张「相邻确认图」：
           "传送点名": {"region": "...", "point": "xxx_point", "confirm": "xxx_point_confirm"},
       confirm 是相邻确认图模板名（tp_imgs/ 下，不含 .png）。配置了 confirm 后，
       传送链路会在「点传送点」之后、「点传送」之前，再找一次确认图并点击，
       作为二次定位，确保点中的是正确目标。由 UI「配置传送点」时勾选
       「缩放后附近有相邻图标」自动生成，一般无需手动添加。

【用户数据外置说明】
    TELEPORT_POINTS / LOCAL_TELEPORTS 是用户数据，外置到 teleport_user.py，
    升级安装时 teleport.py 覆盖更新、teleport_user.py 保留，二者不冲突。

【模板图命名规则】
    模板图放在 tp_imgs/ 目录：
      1. 通用图（所有传送点共用）：common_ 前缀，如 common_world_btn.png、
         common_teleport_btn.png
      2. 缩小按钮（两种，背景色不同，不可混用）：
           common_world_zoom_out.png —— 世界地图的缩小按钮（浅灰底）
           common_zoom_out.png       —— 小地图的缩小按钮（深黑底）
      3. 小地图入口图：common_ 前缀 + 要塞名拼音，如 common_valken.png、
         common_aolumu.png（瓦尔肯要塞 / 奥鲁姆要塞）
      4. 传送点专属图：用「传送点拼音」命名，如 cuihuochi_point.png
      5. 相邻确认图：用「传送点拼音 + _confirm」命名，如 xxx_point_confirm.png
    新增传送点时，通常只需新截「传送点图标」这一张图，其余全部复用。
"""

import sys
import time
import os
import threading
from pynput import keyboard as kb
from pynput.keyboard import Controller as KbController, Key

import imgfind
from pynput.mouse import Controller as MouseController, Button as MouseButton

# 强制 stdout 用 UTF-8 输出：本脚本由 UI 以子进程方式启动，输出重定向到 teleport_log.txt。
# Windows 下子进程 stdout 默认 GBK，若不强制 UTF-8，中文日志会写成 GBK 字节，UI 按 UTF-8 读取就乱码。
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

mouse = MouseController()

# 窗口切换相关（确保传送时游戏窗口在前台，否则截屏截到的是别的窗口）
try:
    import win32gui
    import win32con
    import win32process
    import win32api
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

keyboard = KbController()

# 全局停止标志：Esc 按下后置 True，动作循环据此中断
stop_flag = False


def focus_game_window():
    """切换焦点到游戏窗口（标题含 Lumin），返回是否成功"""
    if not _HAS_WIN32:
        return False
    result = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if title and "lumin" in title.lower():
            result.append(hwnd)

    try:
        win32gui.EnumWindows(enum_cb, None)
    except Exception:
        pass
    if not result:
        return False

    hwnd = result[0]
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.1)
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
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        if attached:
            try:
                win32process.AttachThreadInput(current_thread, fg_thread, False)
            except Exception:
                pass
        return True
    except Exception:
        return False


def _on_press(key):
    """监听 Esc，按下立即终止整个传送流程"""
    global stop_flag
    if key == kb.Key.esc:
        stop_flag = True
        print("[传送] 检测到 Esc，停止传送")
        # 用非零退出码，让 UI 能识别为「被中断」而非「成功」
        threading.Thread(target=lambda: (time.sleep(0.15), os._exit(1)), daemon=True).start()
        return False  # 停止监听

# ------------------------- 传送点配置 -------------------------
DEFAULT_MAP = "淬火池"

# ------------------------------------------------------------------
# 一、小地图（要塞/区域）入口配置
# ------------------------------------------------------------------
# 世界地图上可点进去的「小地图」，每个小地图对应一张入口截图（点世界地图后选中它）。
# 名字 → 入口模板图名（tp_imgs/ 下，不含 .png）
# 世界地图上共 6 个小地图：纽特基地、瓦尔肯要塞、巨坑底部、因弗内斯巨坑、奥鲁姆要塞、镜影岩洞
REGION_ENTRANCES = {
    # 入口图支持「列表」：同一要塞在角色「在/不在」时有细微外观差异（多一个定位图钉），
    # 传列表即可任一命中，无需精确区分状态。
    "瓦尔肯要塞":   ["common_valken", "common_valken_in"],
    "奥鲁姆要塞":   ["common_aolumu", "common_aolumu_in"],
    "纽特基地":     ["common_niute", "common_niute_in"],
    "巨坑底部":     ["common_jukengdibu", "common_jukengdibu_in"],
    "因弗内斯巨坑": ["common_yinfuneisi", "common_yinfuneisi_in"],
    "镜影岩洞":     ["common_jingyingyandong", "common_jingyingyandong_in"],
}

# ------------------------------------------------------------------
# 二、传送点配置（用户数据，外置于 teleport_user.py）
# ------------------------------------------------------------------
# 跨区域传送点（TELEPORT_POINTS）与就近传送点（LOCAL_TELEPORTS）是「用户数据」，
# 用户在 UI 里一个个配置，升级安装时必须保留、不能随版本覆盖。故外置到
# teleport_user.py，这里 import 进来。teleport_user.py 不存在时用空字典兜底，
# 保证程序仍能启动（此时 UI 里没有任何传送点，需用户重新配置）。
try:
    from teleport_user import TELEPORT_POINTS, LOCAL_TELEPORTS
except Exception:
    TELEPORT_POINTS = {}
    LOCAL_TELEPORTS = {}

# 就近传送各阶段等待时长（秒）
LOCAL_WAIT_OPEN_MAP = 1.2   # 按 M 后等地图加载
LOCAL_WAIT_SELECT = 0.8     # 点休息点图标后等选中
LOCAL_WAIT_AFTER = 3.5      # 点「传送」后等传送完成（默认值）


# 缩小地图需要点的次数
ZOOM_OUT_TIMES = 8          # 进入小地图后，缩小小地图的次数
WORLD_ZOOM_OUT_TIMES = 3    # 点「世界」后，先缩小世界地图的次数（世界地图默认是放大状态）

# 传送链路各阶段等待时长（秒）
WAIT_OPEN_MAP = 1.5     # 按 M 后等地图加载
WAIT_TO_WORLD = 1.5     # 点「世界」后等世界地图加载
WAIT_WORLD_ZOOM = 0.5   # 世界地图缩小后等待
WAIT_TO_REGION = 1.5    # 点小地图后等区域地图加载
WAIT_AFTER_ZOOM = 1.2   # 缩小完成后等地图缩放到完整
WAIT_SELECT_POINT = 1.0 # 点传送点后等选中
WAIT_AFTER_TP = 3.5     # 点「传送」后等传送完成


# ------------------------------------------------------------------
# 「世界」按钮匹配（单独放松：降阈值 + 支持多张模板图）
# ------------------------------------------------------------------
# 「世界」按钮在打开地图(M)后出现，其文字/图标颜色浅、且随地图背景色变化，
# 归一化相关系数(TM_CCOEFF_NORMED)对这类低对比度目标不够稳，默认 0.8 会点不到。
# 故为它单独放低阈值；其他按钮（传送点、传送按钮等）仍用默认 0.8，保持严格。
# 背景色随场景变化会导致同一张图在部分地图匹配分数偏低，因此：
#   1) 阈值放低到 WORLD_BTN_THRESHOLD；
#   2) 支持「多张模板图」——把不同地图背景下截到的「世界」按钮图都放进
#      WORLD_BTN_IMGS 列表（tp_imgs/ 下、不含 .png），匹配时取相似度最高的那张，
#      任一命中即点击。补图后点不上世界的问题通常即可解决。
WORLD_BTN_THRESHOLD = 0.5
WORLD_BTN_IMGS = [
    "common_world_btn",
    "common_world_btn_2",
    "common_world_btn_3",
]


# ------------------------------------------------------------------
# 缩小按钮：位置固定，直接用固定坐标点击（不再依赖图片匹配）
# ------------------------------------------------------------------
# 世界地图/小地图的缩小按钮是半透明、低对比度、背景随缩放变化的圆形按钮，
# 用模板匹配（TM_CCOEFF_NORMED）极不稳定：点一下后背景变化，匹配分数就跌破阈值，
# 甚至误匹配到并排的放大按钮。而该按钮位置固定（缩放条左端），
# 故改为「固定坐标点击」，彻底绕开图片匹配的不确定性。
# 从实测（1920×1080 全屏）看：缩小按钮中心约 (708, 976)，放大按钮约 (1209, 976)。
ZOOM_OUT_POS = (708, 976)   # 缩小按钮固定点击坐标（中心）
ZOOM_OUT_REGION = (550, 900, 1000, 1050)  # 图片匹配时的搜索区域（已弃用，保留兼容）


def build_actions(point_name):
    """根据传送点名称，自动拼接完整的传送动作序列"""
    info = TELEPORT_POINTS.get(point_name)
    if not info:
        return None

    region = info["region"]
    entrance = REGION_ENTRANCES.get(region)
    if not entrance:
        print(f"[错误] 小地图「{region}」未配置入口图，请在 REGION_ENTRANCES 里添加")
        return None

    point_img = info["point"]

    actions = [
        ("tap", 'm'),                    # 1. 按 M 打开地图
        ("wait", WAIT_OPEN_MAP),         # 2. 等地图加载
        # 3. 找「世界」按钮并点击
        #    「世界」按钮文字颜色浅、且地图背景色随场景变化，对比度不稳定，
        #    故单独放松阈值（WORLD_BTN_THRESHOLD）并支持多张模板图（WORLD_BTN_IMGS）。
        ("find", WORLD_BTN_IMGS, WORLD_BTN_THRESHOLD),
        ("wait", WAIT_TO_WORLD),         # 4. 等世界地图加载
    ]
    # 5. 世界地图默认是放大状态，先缩小 WORLD_ZOOM_OUT_TIMES 次看到完整世界地图
    #    缩小按钮位置固定、半透明低对比度，改用固定坐标点击（zoom 动作），
    #    不再用脆弱的图片匹配。
    actions += [("zoom",)] * WORLD_ZOOM_OUT_TIMES
    actions += [
        ("wait", WAIT_WORLD_ZOOM),       # 6. 等世界地图缩放到完整
        ("find", entrance),              # 7. 找小地图入口并点击
        ("wait", WAIT_TO_REGION),        # 8. 等区域地图加载
    ]
    # 9. 连续缩小小地图 ZOOM_OUT_TIMES 次（同样用固定坐标点击）
    actions += [("zoom",)] * ZOOM_OUT_TIMES
    actions += [
        ("wait", WAIT_AFTER_ZOOM),       # 10. 等地图缩放到完整
        ("find", point_img),             # 11. 找传送点并点击
        ("wait", WAIT_SELECT_POINT),     # 12. 等选中传送点
    ]
    # 12.5 可选：传送点附近有相邻图标（休息点/监测塔/锚定点/残片等）时，
    # 单靠传送点图可能误点相邻元素。若该传送点配置了 confirm（相邻确认图），
    # 这里再找一次确认图并点击，作为「二次定位确认」，确保点中的是正确目标。
    confirm_img = info.get("confirm")
    if confirm_img:
        actions += [
            ("find", confirm_img),       # 找相邻确认图并点击（二次定位）
            ("wait", WAIT_SELECT_POINT), # 再等选中稳定
        ]
    actions += [
        ("find", "common_teleport_btn"), # 13. 找「传送」按钮并点击
        ("wait", WAIT_AFTER_TP),         # 14. 等传送完成
    ]
    return actions


def build_local_actions(name):
    """根据就近传送点名称，构建「地图内就近传回休息点」的动作序列"""
    info = LOCAL_TELEPORTS.get(name)
    if not info:
        return None
    point_img = info["point"]
    wait_after = info.get("wait_after", LOCAL_WAIT_AFTER)

    return [
        ("tap", 'm'),                    # 1. 按 M 打开地图
        ("wait", LOCAL_WAIT_OPEN_MAP),   # 2. 等地图加载
        ("find", point_img),             # 3. 找休息点传送图标并点击
        ("wait", LOCAL_WAIT_SELECT),     # 4. 等选中
        ("find", "common_teleport_btn"), # 5. 找「传送」按钮并点击
        ("wait", wait_after),            # 6. 等传送完成
    ]


# ------------------------- 动作执行 -------------------------
def _resolve_key(k):
    if isinstance(k, str):
        if len(k) == 1:
            return k
        special = {
            "space": Key.space, "shift": Key.shift, "ctrl": Key.ctrl,
            "alt": Key.alt, "enter": Key.enter, "esc": Key.esc,
            "tab": Key.tab, "up": Key.up, "down": Key.down,
            "left": Key.left, "right": Key.right,
        }
        if k.lower() in special:
            return special[k.lower()]
        kl = k.lower()
        if kl.startswith("f") and kl[1:].isdigit():
            return getattr(Key, kl, k)
        return k
    return k


def _do_tap(key):
    keyboard.press(key)
    keyboard.release(key)


def _click_zoom_out():
    """点击缩小按钮（固定坐标）。

    缩小按钮位置固定（缩放条左端），半透明低对比度导致模板匹配不稳，
    故直接点击固定坐标 ZOOM_OUT_POS。点击后短暂停顿让地图完成缩放。
    """
    mouse.position = ZOOM_OUT_POS
    time.sleep(0.05)
    mouse.click(MouseButton.left, 1)
    print(f"[传送] 点击缩小按钮 {ZOOM_OUT_POS}")


def run_teleport(actions):
    """执行一串传送动作（每步前检查是否被 Esc 中断）"""
    global stop_flag
    # 启动 Esc 监听线程
    listener = kb.Listener(on_press=_on_press)
    listener.start()

    for i, item in enumerate(actions, 1):
        if stop_flag:
            print(f"[传送] 在第 {i} 步前被 Esc 中断")
            break
        act = item[0]
        if act == "tap":
            _do_tap(_resolve_key(item[1]))
            time.sleep(0.1)
        elif act == "wait":
            # 可中断的等待
            _interruptible_sleep(item[1])
        elif act == "zoom":
            # 缩小按钮：位置固定，直接用固定坐标点击，绕开半透明按钮匹配不稳的问题
            _click_zoom_out()
            time.sleep(0.15)
        elif act == "find":
            name = item[1]
            # 支持可选的第三个元素作为自定义阈值，第四个元素作为搜索区域
            # 如 ("find", "xxx", 0.6) 或 ("find", "xxx", 0.6, (x1,y1,x2,y2))
            threshold = item[2] if len(item) >= 3 else 0.8
            region = item[3] if len(item) >= 4 else None
            ok = imgfind.find_and_click(name, threshold=threshold, timeout=6.0,
                                        interval=0.4, stop_check=lambda: stop_flag,
                                        region=region)
            if not ok:
                print(f"[警告] 第 {i} 步找不到「{name}」，跳过继续")
        else:
            print(f"[警告] 未知动作类型：{act}")
        time.sleep(0.05)

    listener.stop()


def _interruptible_sleep(seconds):
    """可被 stop_flag 打断的等待"""
    global stop_flag
    for _ in range(int(seconds * 20)):
        if stop_flag:
            return
        time.sleep(0.05)


def main():
    # 解析参数：支持「就近传送」和「跨区域传送」两种模式
    #   python teleport.py 淬火池              —— 跨区域传送
    #   python teleport.py --local 西侧通道休息点 —— 就近传送（地图内传回休息点）
    args = sys.argv[1:]
    is_local = False
    if args and args[0] == "--local":
        is_local = True
        args = args[1:]

    target = args[0] if args else DEFAULT_MAP

    if is_local:
        actions = build_local_actions(target)
        if actions is None:
            print(f"[错误] 未配置就近传送点：{target}")
            print(f"已配置：{list(LOCAL_TELEPORTS.keys()) or '（无）'}")
            sys.exit(1)
        mode_desc = "就近传送"
    else:
        if target not in TELEPORT_POINTS:
            print(f"[错误] 未配置传送点：{target}")
            print(f"已配置：{list(TELEPORT_POINTS.keys()) or '（无）'}")
            sys.exit(1)
        actions = build_actions(target)
        if actions is None:
            sys.exit(1)
        mode_desc = "跨区域传送"

    # 先切换焦点到游戏窗口，确保截屏能截到游戏画面
    if focus_game_window():
        print("[传送] 已切换到游戏窗口")
        time.sleep(0.5)  # 等窗口完全置前
    else:
        print("[传送] 警告：未能切换游戏窗口，请手动把游戏窗口调到前台")

    print(f"[{time.strftime('%H:%M:%S')}] 开始{mode_desc}：{target}（图片识别定位）")
    run_teleport(actions)
    print(f"[{time.strftime('%H:%M:%S')}] {mode_desc}完成：{target}")


if __name__ == "__main__":
    main()
