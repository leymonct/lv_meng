# -*- coding: utf-8 -*-
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
    python 20261103_141337.py
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
LOOP_ENABLED = True

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
LOCAL_REST_POINT_IMG = "20261103_141337_rest_point"   # TODO: 改成你这个脚本的休息点图标模板名
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
ROUND_MIN_SECONDS = 5.1
ROUND_ANOMALY_LIMIT = 3

_teleport_fail_streak = 0   # 连续传送失败的轮数
_anomaly_streak = 0         # 连续异常轮的次数
_last_teleport_ok = None    # 本轮传送结果：None=未执行，True/False=成功/失败


def _record_stat(round_no, duration, teleport_ok):
    """追加一行本轮统计到 runs.jsonl（供 UI 显示看板）。

    字段：时间 / 轮次 / 耗时秒 / 本轮传送是否成功。
    """
    try:
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "round": round_no,
            "duration": round(duration, 2),
            "teleport_ok": teleport_ok,
        }
        with open(STATS_FILE, "a", encoding="utf-8") as f:
            # 注意：模板里必须写成双反斜杠 + n，否则模板字符串在解析阶段
            # 就会变成真正的换行，生成出来的脚本会缩进错乱。
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
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
            print(f"[就近传送] 第 {attempt} 次重试……")
            wait(1.0)   # 重试前给游戏一点恢复时间（加载 / 地图关闭动画）
        if _teleport_once():
            if _teleport_fail_streak > 0:
                print(f"[就近传送] 已恢复正常（此前连续失败 {_teleport_fail_streak} 轮）")
            _teleport_fail_streak = 0
            _last_teleport_ok = True
            return True

    _teleport_fail_streak += 1
    _last_teleport_ok = False
    print(f"[就近传送] 本轮传送失败（连续 {_teleport_fail_streak}/{TELEPORT_FAIL_LIMIT} 轮）")
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
    print(f"[熔断] 连续 {_teleport_fail_streak} 轮传送失败，已自动暂停")
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

    _steps = [
    (0.62, ('down', 'a')),
    (1.12, ('down', 'a')),
    (1.62, ('down', 'a')),
    (2.15, ('down', 'a')),
    (2.68, ('down', 'a')),
    (3.2, ('down', 'a')),
    (3.53, ('up', 'a')),
    (6.0, ('down', 'a')),
    (6.51, ('down', 'a')),
    (7.04, ('down', 'a')),
    (7.57, ('down', 'a')),
    (8.09, ('down', 'a')),
    (8.62, ('down', 'a')),
    (9.1, ('down', Key.space)),
    (9.25, ('up', Key.space)),
    (9.82, ('down', Key.space)),
    (9.98, ('up', Key.space)),
    (10.2, ('down', Key.space)),
    (10.31, ('up', Key.space)),
    (11.62, ('up', 'a')),
    (12.1, ('click', 'left')),
    (12.34, ('click', 'left')),
    (12.56, ('click', 'left')),
    (12.7, ('click', 'left')),
    ]
    _play_timeline(_steps)
    # 回放动作结束后的收尾缓冲：等最后一个动作完全完成，再进入传送
    wait(LOCAL_SETTLE_AFTER_REPLAY)

    # —— 刷完怪后：就近传回休息点，再开始下一轮 ——
    print(f"[{time.strftime('%H:%M:%S')}] 刷完一轮，就近传送回休息点……")
    if not local_teleport():
        _on_teleport_failed()   # 连续失败达到上限会自动暂停（熔断）
    wait(1.0)   # 传送落地后的短暂缓冲



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
            time.sleep(0.1)
            continue

        try:
            loop_no += 1
            _last_teleport_ok = None    # 本轮开始，清掉上一轮的传送结果
            _t0 = time.perf_counter()
            print(f"[{time.strftime('%H:%M:%S')}] 开始第 {loop_no} 轮")
            run_cycle()
            elapsed = time.perf_counter() - _t0
            print(f"[{time.strftime('%H:%M:%S')}] 完成第 {loop_no} 轮（耗时 {elapsed:.1f}s）")
            _record_stat(loop_no, elapsed, _last_teleport_ok)
        except Exception as e:
            print(f"[错误] {e}")
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
            print(f"[守护] 本轮仅耗时 {elapsed:.1f}s，低于预期 {ROUND_MIN_SECONDS:.1f}s，"
                  f"疑似掉线/卡界面（连续 {_anomaly_streak}/{ROUND_ANOMALY_LIMIT} 次）")
            if _anomaly_streak >= ROUND_ANOMALY_LIMIT:
                running = False
                print("=" * 50)
                print(f"[熔断] 连续 {_anomaly_streak} 轮耗时异常，已自动暂停")
                print("[熔断] 请检查：游戏是否掉线/卡在读条界面、窗口是否被切走")
                print("=" * 50)
                continue
        else:
            if _anomaly_streak > 0:
                print(f"[守护] 本轮耗时恢复正常，异常计数清零（原 {_anomaly_streak}）")
            _anomaly_streak = 0

        # —— 循环次数控制 ——
        if MAX_LOOPS > 0 and loop_no >= MAX_LOOPS:
            running = False
            print(f"[{time.strftime('%H:%M:%S')}] 已达到设定循环次数({MAX_LOOPS})，自动停止")
        elif not LOOP_ENABLED:
            # 单次执行模式：跑一遍就停，不再循环
            running = False
            print(f"[{time.strftime('%H:%M:%S')}] 单次执行完成，停止（未配置休息点图，不循环）")

        # 跑完（单次模式完成 / 达到循环上限）→ 主动结束脚本进程
        if not LOOP_ENABLED or (MAX_LOOPS > 0 and loop_no >= MAX_LOOPS):
            stop_all = True
            print("脚本任务完成，即将退出……")
            break


# ------------------------- 热键监听 -------------------------
def on_press(key):
    global running, stop_all

    if key == kb.Key.f8:
        running = not running
        print(f"循环已{'启动' if running else '暂停'}")
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
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 循环自动操作已启动")
    if LOOP_ENABLED:
        if MAX_LOOPS > 0:
            print(f"循环模式：共 {MAX_LOOPS} 轮（跑满自动停止）")
        else:
            print("循环模式：无限循环")
    else:
        print("执行模式：单次执行（未配置休息点图）")
    print("快捷键：F8 开始 | Esc 退出")
    print("=" * 50)

    t = threading.Thread(target=macro_loop, daemon=True)
    t.start()

    # 非阻塞监听热键 + 检测任务是否自动完成：任一满足即退出进程，
    # 避免一直阻塞在 listener.join() 导致进程不结束、UI 误判运行中而卡死。
    listener = kb.Listener(on_press=on_press)
    listener.start()
    try:
        while not stop_all:
            if not t.is_alive():
                break
            time.sleep(0.1)
    finally:
        listener.stop()

    print("脚本已退出。")


if __name__ == "__main__":
    main()
