# -*- coding: utf-8 -*-
"""
录制事件 → 模板风格脚本转换器
================================
把 recorder.py 录到的原始事件序列（key_down/key_up/mouse_click + 相对时间戳），
转换成可供 MacroManager 启动运行的录制脚本代码（recordings/ 下的 .py）：

  - 连续按键（key_down→key_up 时长够长）→ hold_key(key, 秒)
  - 短按键                        → tap_key(key)
  - 相邻事件之间的停顿             → wait(秒)
  - 鼠标连续点击                  → click_mouse(次数, 间隔)
  - 鼠标移动                     → 忽略（脚本用固定坐标无意义，移动不录制）

阈值（可调）：
  HOLD_THRESHOLD = 0.08  按键时长超过此值算「长按」，否则算「点按」
  MIN_WAIT       = 0.05  间隔小于此值忽略（噪音/事件处理抖动）
"""

HOLD_THRESHOLD = 0.15   # 秒：按键时长超过此值算「长按」，否则算「点按」
MIN_WAIT = 0.05         # 秒：间隔小于此值忽略（噪音/事件处理抖动）
COMBINE_GAP = 0.12      # 秒：两个 key_down 间隔小于此值视为「同时按」（组合键）


def _key_repr(key_name):
    """把录制的键名转成脚本里的写法：普通字母→'a'，特殊键→Key.xxx"""
    special = {
        "space": "Key.space", "shift": "Key.shift", "shift_r": "Key.shift_r",
        "ctrl": "Key.ctrl", "ctrl_l": "Key.ctrl_l", "ctrl_r": "Key.ctrl_r",
        "alt": "Key.alt", "alt_l": "Key.alt_l", "alt_r": "Key.alt_r",
        "enter": "Key.enter", "esc": "Key.esc", "tab": "Key.tab",
        "backspace": "Key.backspace", "delete": "Key.delete",
        "up": "Key.up", "down": "Key.down", "left": "Key.left", "right": "Key.right",
        "caps_lock": "Key.caps_lock", "cmd": "Key.cmd",
        "page_up": "Key.page_up", "page_down": "Key.page_down",
        "home": "Key.home", "end": "Key.end",
    }
    if key_name in special:
        return special[key_name]
    if key_name.startswith("f") and key_name[1:].isdigit():
        return f"Key.{key_name}"
    # 普通单字符键
    if len(key_name) == 1:
        return repr(key_name)
    return repr(key_name)


def _fmt_seconds(t):
    """把秒数格式化成简洁的浮点字符串。

    - 接近整数的（如 2.0）→ "2.0"
    - 有小数（如 0.5、0.35）→ "0.5" / "0.35"
    """
    t = round(t, 2)
    if abs(t - round(t)) < 0.005:
        return f"{round(t):.1f}"
    return f"{t:.2f}".rstrip("0").rstrip(".")


def events_to_steps(events):
    """把原始事件列表转成「精确时间线步骤」，用于生成高精度回放脚本。

    输入 events：list of dict，每个含 type / t（相对秒）/ 及该类型字段：
        {"type": "key_down", "t": 0.1, "key": "w"}
        {"type": "key_up",   "t": 2.1, "key": "w"}
        {"type": "mouse_click", "t": 3.0, "x": 100, "y": 200, "button": "left"}

    输出 steps：list of str，每行是 `_step(相对秒, 动作)` 调用，回放时用
    perf_counter 精确对齐每个时间点，从而最大程度还原录制时的节奏。

    与旧的 events_to_actions（压缩成 hold_key/hold_combo/tap_key）不同，本函数
    直接保留「原始事件 + 相对时间戳」，避免：
      - 组合键误合并导致节奏错乱
      - tap_key 硬编码 0.05s 延时带来的漂移
      - hold_key/hold_combo 把按键间细微时差抹平

    规则：
      - key_down   → _step(t, ("down", key))
      - key_up     → _step(t, ("up", key))
      - mouse_click→ _step(t, ("click", button))   # 左键=攻击，不移动鼠标
      - 连续的同类型事件自动合并（如连续多个点击合成一次 click_mouse(次数)），
        减少脚本体积，但保留每个动作的精确时间点。
    """
    if not events:
        return []

    events = sorted(events, key=lambda e: e["t"])

    # 第一步：过滤掉「按下后立即松开」的抖动噪音（时长 < 0.005s 的按键，
    # 通常是事件采集抖动，不是真实按键）。
    # 但这里先不激进过滤，交给回放端天然还原。只做基础清理：
    #   - 去掉 key_down 和 key_up 完全重叠（同一时刻）的重复项。

    steps = []
    for ev in events:
        t = ev["t"]
        typ = ev["type"]
        if typ == "key_down":
            steps.append(f"    ({_fmt_seconds(t)}, ('down', {_key_repr(ev['key'])})),")
        elif typ == "key_up":
            steps.append(f"    ({_fmt_seconds(t)}, ('up', {_key_repr(ev['key'])})),")
        elif typ == "mouse_click":
            btn = ev.get("button", "left")
            steps.append(f"    ({_fmt_seconds(t)}, ('click', '{btn}')),")
        # 其他未知类型忽略

    return steps


def generate_run_cycle_body(events):
    """根据事件生成 run_cycle() 函数体里的动作代码。

    生成一个 _steps 列表 + _play_timeline(_steps) 调用，
    回放时按精确时间线还原录制节奏。
    """
    steps = events_to_steps(events)
    if not steps:
        return ["    pass  # 录制内容为空"]

    lines = ["    _steps = ["]
    lines.extend(steps)
    lines.append("    ]")
    lines.append("    _play_timeline(_steps)")
    lines.append("    # 回放动作结束后的收尾缓冲：等最后一个动作完全完成，再进入传送")
    lines.append("    wait(LOCAL_SETTLE_AFTER_REPLAY)")
    return lines


# ------------------------------------------------------------------
# 生成完整脚本：套用内置骨架模板，把录制动作填进 run_cycle()
# ------------------------------------------------------------------
import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 脚本骨架模板：run_cycle 的示例动作会被 {BODY} 替换
_SCRIPT_TEMPLATE = '''# -*- coding: utf-8 -*-
"""
循环自动操作脚本（由录制生成）
================================
【说明】
    本脚本由 recorder.py 录制自动生成，动作与录制时一致。
    已内置「就近传送」代码（local_teleport），默认注释掉调用——
    因为还没有休息点图。配好 tp_imgs/ 里的休息点图、并把 LOCAL_REST_POINT_IMG
    改成对应模板名后，取消 run_cycle() 末尾那几行注释，即可启用
    「录制复现 + 就近传送」的完整循环。
    跨区域传送（启动前从别的地图传送）请参照 teleport.py 的【传送说明】。

【使用方法】
    python {script_name}
    （依赖 cv2/mss/numpy/pynput/pywin32，见 requirements.txt）

【快捷键】
    F8   —— 开始 / 暂停循环
    F9   —— 完全停止（回到空闲）
    Esc  —— 退出脚本
"""

import time
import threading
import os
import sys
import json
from pynput import keyboard as kb
from pynput.keyboard import Controller as KbController, Key
from pynput.mouse import Controller as MouseController, Button

# 强制 stdout/stderr 用 UTF-8 编码输出：本脚本由 UI 以子进程方式启动，
# 输出被重定向到 macro_log.txt。Windows 下子进程 stdout 默认是 GBK，
# 若不强制 UTF-8，中文日志（含 imgfind 的「找到/未找到」提示）会写入成 GBK 字节，
# UI 按 UTF-8 读取时就会乱码。这里统一为 UTF-8，保证日志可读。
# 同时 line_buffering=True：让每次 print 立即 flush 到日志文件，
# 否则重定向到文件时 stdout 是块缓冲，UI 端会长时间读不到新日志（表现为空白）。
try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# 把项目根目录加入模块搜索路径：本脚本在 recordings/ 子目录下，
# 而 imgfind.py 等项目模块在上一级（项目根目录）。不加这段，import imgfind 会失败。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import imgfind

# ------------------------- 全局状态 -------------------------
keyboard = KbController()
mouse = MouseController()

running = False         # 循环是否在运行
stop_all = False        # 是否退出脚本

# 是否循环执行：
#   True  —— 已配置休息点图（启用就近传送），刷怪 + 传送无限循环
#   False —— 暂无休息点图，只执行一次就停（用于验证录制是否正确）
LOOP_ENABLED = {loop_enabled}

# 最大循环次数（0 或负数 = 无限循环）。由 UI 启动时通过环境变量 MAX_LOOPS 传入，
# 用于控制「刷 N 轮后自动停止」。仅当 LOOP_ENABLED=True 时才真正影响（循环模式）。
MAX_LOOPS = int(os.environ.get("MAX_LOOPS", "0") or 0)

# 每轮统计写到项目根目录的 runs.jsonl，UI 读取它显示「已跑轮数 / 平均耗时 / 传送成功率」。
# 统计只是辅助信息，写失败不影响刷怪主流程。
STATS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs.jsonl"
)


# ------------------------- 基础动作 -------------------------
def hold_key(key, seconds):
    """长按某个键指定秒数"""
    keyboard.press(key)
    time.sleep(seconds)
    keyboard.release(key)


def hold_combo(keys, seconds):
    """同时按住多个键指定秒数，然后一起松开。keys 是列表，如 ['w', Key.space]"""
    for k in keys:
        keyboard.press(k)
    time.sleep(seconds)
    for k in reversed(keys):
        keyboard.release(k)


def tap_key(key, count=1):
    """点按某个键指定次数（无额外延时，节奏由调用处控制）"""
    for _ in range(count):
        keyboard.press(key)
        keyboard.release(key)


def click_mouse(count, interval=0.3):
    """鼠标左键连续点击指定次数，interval 是每次点击间隔（秒）。

    注意：鼠标左键在操作中只负责「攻击」，点的是玩家当前朝向/位置，
    因此不移动鼠标，直接在当前鼠标位置连点。
    """
    for _ in range(count):
        mouse.click(Button.left, 1)
        time.sleep(interval)


def wait(seconds):
    """精确等待指定秒数（可被停止信号打断，精度到毫秒级）。

    用小步长 sleep 是为了能及时响应 F8/F9/Esc 停止信号，
    同时避免 int(seconds*20) 这种量化带来的时间误差。
    """
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        if stop_all or not running:
            return
        time.sleep(0.001)


# ------------------------- 精确时间线回放（录制还原核心） -------------------------
# 录制脚本不再把事件压缩成 hold_key/hold_combo/tap_key 这些会引入额外延时、
# 或抹平按键间细微时差的高级动作，而是直接保留「原始事件 + 相对时间戳」，
# 回放时用 perf_counter 精确对齐每个时间点，最大程度还原录制时的节奏。

def _play_timeline(steps):
    """按精确时间线回放一串动作。

    steps 是 list of tuple：(相对秒, 动作)，
    动作是 ('down', key) / ('up', key) / ('click', button) 之一。
    回放起点取第一个动作的时刻，之后每个动作都对齐到「起点 + 相对秒」。
    """
    if not steps:
        return
    start = time.perf_counter()
    for t, action in steps:
        # 对齐到该动作应发生的时刻（用 perf_counter 保证高精度）
        target = start + t
        while time.perf_counter() < target:
            if stop_all or not running:
                return
            time.sleep(0.001)
        if stop_all or not running:
            return
        kind = action[0]
        if kind == "down":
            keyboard.press(action[1])
        elif kind == "up":
            keyboard.release(action[1])
        elif kind == "click":
            btn = Button.left if action[1] == "left" else (
                Button.right if action[1] == "right" else Button.middle)
            mouse.click(btn, 1)


# ------------------------- 就近传送（回休息点） -------------------------
# 刷完怪后打开地图就近传回休息点，省去来回跑动。
# 默认注释掉 run_cycle() 末尾的 local_teleport() 调用（因为还没有休息点图）。
# 配好 LOCAL_REST_POINT_IMG 的模板图后，取消那几行注释即可启用完整循环。

# 就近传送关键参数：
#   LOCAL_REST_POINT_IMG  —— 休息点图标模板名（tp_imgs/ 下，不含 .png）
#   注意：休息点图标是「黄色座椅+橙色底座」造型，和「传送点」（橙色菱形晶体）是两套图。
LOCAL_REST_POINT_IMG = "xiuxidian_point"   # TODO: 改成你这个脚本的休息点图标模板名
LOCAL_WAIT_OPEN_MAP = 1.2   # 按 M 后等地图加载
LOCAL_WAIT_SELECT = 0.8     # 点休息点图标后等选中
LOCAL_WAIT_AFTER = 3.5      # 点「传送」后等传送完成
LOCAL_FIND_TIMEOUT = 6.0    # 找休息点图标的超时时间
LOCAL_SETTLE_AFTER_REPLAY = 1.0  # 回放动作结束后的「收尾缓冲」：等最后一个动作
                                 # （攻击动画 / 按键释放 / 落地）完全完成，再开地图传送，
                                 # 避免动作还没做完就急着按 M 导致传送失败

# ------------------------- 无人值守守护（熔断） -------------------------
# 目的：一旦环境异常（休息点图失效 / 掉线 / 卡在加载界面），脚本不能在错误的
# 状态下无限空刷。宁可自己停下来并留下明确告警，也不要白跑几百轮。
#
#   TELEPORT_RETRIES     单轮内传送失败后的重试次数（0 = 不重试）
#   TELEPORT_FAIL_LIMIT  连续多少轮传送失败就自动暂停（熔断阈值）
#   ROUND_MIN_SECONDS    一轮耗时低于该值视为「异常轮」（掉线 / 卡界面 / 动作没生效）
#   ROUND_ANOMALY_LIMIT  连续多少轮异常就自动暂停
TELEPORT_RETRIES = 2
TELEPORT_FAIL_LIMIT = 3
ROUND_MIN_SECONDS = {round_min}
ROUND_ANOMALY_LIMIT = 3

_teleport_fail_streak = 0   # 连续传送失败的轮数
_anomaly_streak = 0         # 连续异常轮的次数
_last_teleport_ok = None    # 本轮传送结果：None=未执行，True/False=成功/失败


def _record_stat(round_no, duration, teleport_ok):
    """追加一行本轮统计到 runs.jsonl（供 UI 显示看板）。

    字段：时间 / 轮次 / 耗时秒 / 本轮传送是否成功。
    """
    try:
        record = {{
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "round": round_no,
            "duration": round(duration, 2),
            "teleport_ok": teleport_ok,
        }}
        with open(STATS_FILE, "a", encoding="utf-8") as f:
            # 注意：模板里必须写成双反斜杠 + n，否则模板字符串在解析阶段
            # 就会变成真正的换行，生成出来的脚本会缩进错乱。
            f.write(json.dumps(record, ensure_ascii=False) + "\\n")
    except Exception:
        pass


def _should_stop():
    """供 imgfind 在匹配循环中检查是否被 F9/Esc 停止"""
    return stop_all or not running


def _teleport_once():
    """尝试一次「开地图 → 点休息点 → 点传送」，返回是否成功。

    只做一次，不做重试；重试与熔断由 local_teleport() 统一负责。
    失败时会把地图关掉，尽量回到游戏画面，避免把错误状态留给下一轮。
    """
    if _should_stop():
        return False

    # 1. 按 M 打开地图
    tap_key('m')
    wait(LOCAL_WAIT_OPEN_MAP)
    if _should_stop():
        return False

    # 2. 找休息点图标并点击
    ok = imgfind.find_and_click(
        LOCAL_REST_POINT_IMG,
        threshold=0.8,
        timeout=LOCAL_FIND_TIMEOUT,
        interval=0.4,
        stop_check=_should_stop,
    )
    if not ok:
        print("[就近传送] 未找到休息点图标")
        tap_key('m')  # 关闭地图，避免卡在地图界面
        return False
    wait(LOCAL_WAIT_SELECT)

    # 3. 找「传送」按钮并点击
    ok = imgfind.find_and_click(
        "common_teleport_btn",
        threshold=0.8,
        timeout=LOCAL_FIND_TIMEOUT,
        interval=0.4,
        stop_check=_should_stop,
    )
    if not ok:
        print("[就近传送] 未找到传送按钮")
        tap_key('m')
        return False

    # 4. 等传送完成
    wait(LOCAL_WAIT_AFTER)
    return True


def local_teleport():
    """就近传送（带重试）。返回本轮最终是否成功。

    失败会累加连续失败计数，供 _on_teleport_failed() 判断是否熔断。
    """
    global _teleport_fail_streak, _last_teleport_ok

    for attempt in range(TELEPORT_RETRIES + 1):
        if _should_stop():
            return False
        if attempt > 0:
            print(f"[就近传送] 第 {{attempt}} 次重试……")
            wait(1.0)   # 重试前给游戏一点恢复时间（加载 / 地图关闭动画）
        if _teleport_once():
            if _teleport_fail_streak > 0:
                print(f"[就近传送] 已恢复正常（此前连续失败 {{_teleport_fail_streak}} 轮）")
            _teleport_fail_streak = 0
            _last_teleport_ok = True
            return True

    _teleport_fail_streak += 1
    _last_teleport_ok = False
    print(f"[就近传送] 本轮传送失败（连续 {{_teleport_fail_streak}}/{{TELEPORT_FAIL_LIMIT}} 轮）")
    return False


def _on_teleport_failed():
    """传送失败后的熔断判断：连续失败达到上限就自动暂停。

    返回 True 表示已触发熔断（脚本已暂停），False 表示还可以继续下一轮。
    """
    global running

    if _teleport_fail_streak < TELEPORT_FAIL_LIMIT:
        return False

    running = False
    print("=" * 50)
    print(f"[熔断] 连续 {{_teleport_fail_streak}} 轮传送失败，已自动暂停")
    print("[熔断] 请检查：休息点图是否失效、游戏是否卡在地图/加载界面、")
    print("[熔断]           屏幕分辨率是否变化（模板图是按录制时分辨率截的）")
    print("[熔断] 处理后可再次按 F8 继续，或直接重启脚本")
    print("=" * 50)
    return True


# ------------------------- 核心动作序列 -------------------------
def run_cycle():
    """执行一遍完整动作序列（录制生成）"""

    # 执行前先等待 1 秒，避免游戏切窗/加载卡顿导致首个操作失败
    wait(1.0)

{body}

{teleport_tail}


# ------------------------- 循环主线程 -------------------------
def macro_loop():
    """执行动作序列。

    - LOOP_ENABLED=True  （已配休息点图）→ 循环「刷怪 + 就近传送」
    - LOOP_ENABLED=False （暂无休息点图）→ 只执行一次就停（用于验证录制是否正确）

    循环次数受 MAX_LOOPS 控制：MAX_LOOPS>0 时跑满该次数后自动停止；
    MAX_LOOPS<=0 时无限循环。
    """
    global running, _anomaly_streak, _last_teleport_ok, stop_all
    loop_no = 0  # 已完成的循环次数
    while not stop_all:
        if not running:
            # 已停止（用户 F8 暂停 / 自动停止）：空转等待下一次启动。
            # 单次模式跑完后 running=False，下面会直接退出循环，不在这里空转。
            time.sleep(0.1)
            continue

        try:
            loop_no += 1
            _last_teleport_ok = None    # 本轮开始，清掉上一轮的传送结果
            _t0 = time.perf_counter()
            print(f"[{{time.strftime('%H:%M:%S')}}] 开始第 {{loop_no}} 轮")
            run_cycle()
            elapsed = time.perf_counter() - _t0
            print(f"[{{time.strftime('%H:%M:%S')}}] 完成第 {{loop_no}} 轮（耗时 {{elapsed:.1f}}s）")
            _record_stat(loop_no, elapsed, _last_teleport_ok)
        except Exception as e:
            print(f"[错误] {{e}}")
            time.sleep(0.5)
            continue

        # 用户在本轮中途按了暂停/停止：本轮不算异常，也不再往下判断
        if stop_all or not running:
            continue

        # —— 异常轮守护 ——
        # 一轮正常动作是录制出来的，耗时应接近录制时长。若远低于预期，
        # 多半是掉线回到登录界面、卡在加载界面、或按键完全没生效。
        if elapsed < ROUND_MIN_SECONDS:
            _anomaly_streak += 1
            print(f"[守护] 本轮仅耗时 {{elapsed:.1f}}s，低于预期 {{ROUND_MIN_SECONDS:.1f}}s，"
                  f"疑似掉线/卡界面（连续 {{_anomaly_streak}}/{{ROUND_ANOMALY_LIMIT}} 次）")
            if _anomaly_streak >= ROUND_ANOMALY_LIMIT:
                running = False
                print("=" * 50)
                print(f"[熔断] 连续 {{_anomaly_streak}} 轮耗时异常，已自动暂停")
                print("[熔断] 请检查：游戏是否掉线/卡在读条界面、窗口是否被切走")
                print("=" * 50)
                continue
        else:
            if _anomaly_streak > 0:
                print(f"[守护] 本轮耗时恢复正常，异常计数清零（原 {{_anomaly_streak}}）")
            _anomaly_streak = 0

        # —— 循环次数控制 ——
        if MAX_LOOPS > 0 and loop_no >= MAX_LOOPS:
            running = False
            print(f"[{{time.strftime('%H:%M:%S')}}] 已达到设定循环次数({{MAX_LOOPS}})，自动停止")
        elif not LOOP_ENABLED:
            # 单次执行模式：跑一遍就停，不再循环
            running = False
            print(f"[{{time.strftime('%H:%M:%S')}}] 单次执行完成，停止（未配置休息点图，不循环）")

        # 跑完（单次模式完成 / 达到循环上限）→ 主动结束整个脚本进程，
        # 让 UI 的 is_running() 及时检测到进程退出、恢复启动按钮，而不是
        # 一直阻塞在 listener.join() 空转导致界面「卡死」。
        if not LOOP_ENABLED or (MAX_LOOPS > 0 and loop_no >= MAX_LOOPS):
            stop_all = True
            print("脚本任务完成，即将退出……")
            break


# ------------------------- 热键监听 -------------------------
def on_press(key):
    global running, stop_all

    if key == kb.Key.f8:
        running = not running
        print(f"循环已{{'启动' if running else '暂停'}}")
    elif key == kb.Key.f9:
        running = False
        print("循环已完全停止")
    elif key == kb.Key.esc:
        stop_all = True
        print("正在退出脚本……")
        return False  # 返回 False 停止监听


# ------------------------- 入口 -------------------------
def main():
    print("=" * 50)
    print(f"[{{time.strftime('%Y-%m-%d %H:%M:%S')}}] 循环自动操作已启动")
    if LOOP_ENABLED:
        if MAX_LOOPS > 0:
            print(f"循环模式：共 {{MAX_LOOPS}} 轮（跑满自动停止）")
        else:
            print("循环模式：无限循环")
    else:
        print("执行模式：单次执行（未配置休息点图）")
    print("快捷键：F8 开始 | Esc 退出")
    print("=" * 50)

    t = threading.Thread(target=macro_loop, daemon=True)
    t.start()

    # 用非阻塞方式监听热键，同时检测「任务是否已自动完成」：
    #   - macro_loop 结束（单次模式跑完 / 达到循环上限）→ 进程自动退出
    #   - 用户按 Esc → stop_all=True，进程退出
    # 两者任一满足即退出，避免一直阻塞在 listener.join() 上导致进程不结束、
    # UI 的 is_running() 一直误判「运行中」而界面卡死。
    listener = kb.Listener(on_press=on_press)
    listener.start()
    try:
        while not stop_all:
            if not t.is_alive():
                # 任务线程已结束（单次模式完成或循环跑满），主动退出
                break
            time.sleep(0.1)
    finally:
        listener.stop()

    print("脚本已退出。")


if __name__ == "__main__":
    main()
'''


def generate_script(events, output_path, script_name=None,
                    enable_local_teleport=False, rest_point_img=None):
    """根据录制事件生成一份模板风格的 .py 脚本，保存到 output_path。

    参数：
        events                录制事件列表
        output_path           输出脚本路径
        script_name           脚本名（用于头部注释，默认取 basename）
        enable_local_teleport 是否在 run_cycle() 末尾启用就近传送（默认 False，即注释掉）
        rest_point_img        休息点图标模板名（不含 .png）。启用就近传送时用它覆盖
                              LOCAL_REST_POINT_IMG 的默认值。

    返回生成的脚本文件路径。
    """
    body_lines = generate_run_cycle_body(events)
    if not body_lines:
        body = "    pass  # 录制内容为空"
    else:
        body = "\n".join(body_lines)

    # 就近传送的尾部代码：默认注释掉，启用时放开
    if enable_local_teleport:
        teleport_tail = (
            "    # —— 刷完怪后：就近传回休息点，再开始下一轮 ——\n"
            "    print(f\"[{time.strftime('%H:%M:%S')}] 刷完一轮，就近传送回休息点……\")\n"
            "    if not local_teleport():\n"
            "        _on_teleport_failed()   # 连续失败达到上限会自动暂停（熔断）\n"
            "    wait(1.0)   # 传送落地后的短暂缓冲\n"
        )
    else:
        teleport_tail = (
            "    # —— 就近传送（默认注释）：配好 LOCAL_REST_POINT_IMG 的休息点图后，\n"
            "    #    取消下面几行注释即可启用「刷怪 + 传送」完整循环 ——\n"
            "    # print(f\"[{time.strftime('%H:%M:%S')}] 刷完一轮，就近传送回休息点……\")\n"
            "    # local_teleport()\n"
            "    # wait(1.0)   # 传送落地后的短暂缓冲\n"
        )

    if script_name is None:
        script_name = os.path.basename(output_path)

    # LOOP_ENABLED 与「是否启用就近传送」绑定：
    #   - 有休息点图（enable_local_teleport=True）→ 循环执行
    #   - 无休息点图                          → 只执行一次
    loop_enabled = "True" if enable_local_teleport else "False"

    # 录制时长 = 最后一个事件的时间戳，用来生成「异常轮守护」的阈值。
    # 取 40%：正常一轮不可能比录制时长的一半还短，短到这种程度基本是
    # 掉线 / 卡读条 / 按键完全没生效。
    rec_duration = max((float(e.get("t", 0)) for e in events), default=0.0)
    round_min = max(1.0, round(rec_duration * 0.4, 1))

    content = _SCRIPT_TEMPLATE.format(
        script_name=script_name,
        body=body,
        teleport_tail=teleport_tail,
        loop_enabled=loop_enabled,
        round_min=round_min,
    )

    # 若指定了 rest_point_img，覆盖脚本里的 LOCAL_REST_POINT_IMG 默认值
    if rest_point_img:
        content = content.replace(
            'LOCAL_REST_POINT_IMG = "xiuxidian_point"',
            f'LOCAL_REST_POINT_IMG = "{rest_point_img}"',
        )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)
    return output_path

