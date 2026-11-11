# -*- coding: utf-8 -*-
"""
操作录制 / 回放脚本
================================
【作用】
    精确录制你的键盘 / 鼠标操作（带时间轴），之后按原节奏回放。
    传送点定位不靠录制时的固定坐标，而是你事后手动截图存到 tp_imgs/，
    回放脚本（或单独调用 imgfind）做图片匹配定位，能适应窗口位置变化。

【使用方法】
    python recorder.py
    （依赖 pynput/pywin32/mss，见 requirements.txt）

【快捷键】
    F7   —— 开始录制 / 再按一次停止录制（保存脚本后录制器自动退出）
    F8   —— 回放最近一次录制（按 F8 后 3 秒倒计时开始）
    F9   —— 停止回放
    Esc  —— 退出脚本

【录制产物】
    每次录制保存为一个 JSON 文件：recordings/YYYYMMDD_HHMMSS.json
    结构：{"actions": [{"type": "...", "t": 偏移秒, ...}, ...]}
    可手动编辑 JSON 微调动作（改间隔、删动作等）。

【关于传送点】
    本脚本只负责「录制/回放操作」。需要传送定位时：
      1. 手动截一张传送点/休息点图标，存到 tp_imgs/（命名见 teleport.py 注释）
      2. 用 imgfind.find_and_click("模板名") 做匹配点击（可另写脚本，或改回放逻辑）
"""

import time
import json
import os
import threading
from pynput import keyboard as kb
from pynput import mouse as ms
from pynput.keyboard import Controller as KbController, Key
from pynput.mouse import Controller as MouseController, Button

import record_to_script

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RECORD_DIR = os.path.join(BASE_DIR, "recordings")


# ------------------------- 单实例锁 -------------------------
# 防止同时运行多个录制器进程（多个进程都监听 F7 会导致「录一次出多个脚本」）。
# 用文件锁实现：第二个实例启动时若拿不到锁，直接退出。
def _acquire_singleton_lock():
    """尝试获取单实例锁。成功返回锁文件句柄，失败返回 None。"""
    try:
        import msvcrt
        lock_path = os.path.join(BASE_DIR, ".recorder.lock")
        fh = open(lock_path, "w")
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except Exception:
        return None


_singleton_fh = _acquire_singleton_lock()
if _singleton_fh is None:
    print("[录制器] 检测到已有录制器在运行，本实例退出。")
    os._exit(0)


keyboard = KbController()
mouse = MouseController()

# ------------------------- 全局状态 -------------------------
recording = False          # 是否正在录制
recording_started = False  # 是否已开始过录制（区分 F7 第一次/第二次）
record_start_time = 0.0    # 录制开始时刻（perf_counter 基准）
record_actions = []        # 录制中的动作列表
pressed_keys = set()       # 当前已按下的键（用于过滤系统自动重复的 key_down）
key_down_time = {}         # 每个键最近一次 key_down 的时刻（用于识别「残留键」兜底清理）

playing = False            # 是否正在回放
stop_all = False           # 是否退出脚本
exiting = False            # 是否已触发退出（防止 F7 停止后又立刻被误触发「开始录制」）

# 最近一次录制的文件路径（F8 回放它）
last_record_file = None


# ------------------------- 工具 -------------------------
def _ensure_record_dir():
    if not os.path.exists(RECORD_DIR):
        os.makedirs(RECORD_DIR)


def _now_offset():
    """返回距录制开始的时间偏移（秒）。用 perf_counter 保证高精度计时。"""
    return time.perf_counter() - record_start_time


def _push_action(action):
    """向录制动作列表追加一条"""
    record_actions.append(action)


def _save_record():
    """保存当前录制到 JSON 文件，并自动生成模板风格的 .py 脚本"""
    global last_record_file
    _ensure_record_dir()
    if not record_actions:
        print("[录制] 没有录到任何动作，不保存")
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    fname = stamp + ".json"
    fpath = os.path.join(RECORD_DIR, fname)
    data = {
        "recorded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": round(_now_offset(), 3),
        "actions": record_actions,
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    last_record_file = fpath
    print(f"[录制] 已保存 {len(record_actions)} 个动作到 {fpath}")

    # 自动生成模板风格的 .py 脚本（文件名同 stamp）
    py_name = stamp + ".py"
    py_path = os.path.join(RECORD_DIR, py_name)
    try:
        record_to_script.generate_script(record_actions, py_path)
        print(f"[录制] 已生成脚本 {py_path}")
    except Exception as e:
        print(f"[录制] 生成脚本失败：{e}")

    return fpath


# ------------------------- 录制：键盘 / 鼠标监听 -------------------------
# 系统 auto-repeat 阈值：两次同一键 key_down 间隔小于此值视为「自动重复」，
# 忽略之；间隔大于此值则认为是「真实的新按键」（可覆盖因焦点丢失导致的残留键误过滤）。
AUTO_REPEAT_WINDOW = 0.5   # 秒


def on_press(key):
    global recording, record_actions, pressed_keys
    if key == kb.Key.f7:
        return  # F7 用于控制录制开关，不记录
    if recording:
        k = _key_to_name(key)
        now = _now_offset()
        # 过滤系统自动重复（auto-repeat）：同一个键已处于按下状态时，
        # 忽略后续重复的 key_down，避免录到一堆重复事件。
        if k in pressed_keys:
            # 兜底：若该键上次 key_down 已过去较久（> AUTO_REPEAT_WINDOW），
            # 说明之前那次是「按下后 key_up 丢失」的残留键，而非真正的自动重复。
            # 此时把它当作新按键重新记录，避免漏掉真实动作。
            last = key_down_time.get(k)
            if last is not None and (now - last) > AUTO_REPEAT_WINDOW:
                pressed_keys.discard(k)  # 清理残留，走下方正常记录逻辑
            else:
                return  # 真正的 auto-repeat，忽略
        pressed_keys.add(k)
        key_down_time[k] = now
        _push_action({"type": "key_down", "t": round(now, 3), "key": k})


def on_release(key):
    global recording, pressed_keys
    if key == kb.Key.f7:
        return
    if recording:
        k = _key_to_name(key)
        pressed_keys.discard(k)
        key_down_time.pop(k, None)
        _push_action({"type": "key_up", "t": round(_now_offset(), 3), "key": k})


def on_click(x, y, button, pressed):
    global recording
    if recording and pressed:
        # 只记录「按下」的点击，松开忽略
        b = "left" if button == ms.Button.left else ("right" if button == ms.Button.right else "middle")
        _push_action({"type": "mouse_click", "t": round(_now_offset(), 3), "x": x, "y": y, "button": b})


def _key_to_name(key):
    """把 pynput 的 key 转成可 JSON 序列化的字符串"""
    if isinstance(key, kb.KeyCode):
        # 普通字符键 / 带字符的特殊键
        return key.char if key.char is not None else str(key)
    # Key.xxx 特殊键
    name = key.name if hasattr(key, "name") else str(key)
    return name


# ------------------------- 回放 -------------------------
def _name_to_key(name):
    """把录制的键名还原成 pynput 能用的 key 对象"""
    # 特殊键名映射
    special = {
        "space": Key.space, "shift": Key.shift, "shift_r": Key.shift_r,
        "ctrl": Key.ctrl, "ctrl_l": Key.ctrl_l, "ctrl_r": Key.ctrl_r,
        "alt": Key.alt, "alt_l": Key.alt_l, "alt_r": Key.alt_r,
        "enter": Key.enter, "esc": Key.esc, "tab": Key.tab,
        "backspace": Key.backspace, "delete": Key.delete,
        "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
        "caps_lock": Key.caps_lock, "cmd": Key.cmd, "page_up": Key.page_up,
        "page_down": Key.page_down, "home": Key.home, "end": Key.end,
    }
    if name in special:
        return special[name]
    # f1~f12
    if name.startswith("f") and name[1:].isdigit():
        return getattr(Key, name, None)
    # 单个字符
    if len(name) == 1:
        return name
    return None


def _play_file(fpath):
    """回放一个 JSON 录制文件"""
    global playing
    if not os.path.exists(fpath):
        print(f"[回放] 找不到录制文件：{fpath}")
        return
    with open(fpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    actions = data.get("actions", [])
    if not actions:
        print("[回放] 录制文件里没有动作")
        return

    print(f"[回放] 开始回放 {len(actions)} 个动作（时长 {data.get('duration', '?')}s）")
    playing = True
    base_time = time.time()  # 回放起点
    for act in actions:
        if not playing or stop_all:
            break
        # 等到该动作对应的相对时间点
        target = base_time + act["t"]
        while time.time() < target:
            if not playing or stop_all:
                break
            time.sleep(0.001)
        if not playing or stop_all:
            break

        try:
            _do_action(act)
        except Exception as e:
            print(f"[回放] 执行动作出错：{act} -> {e}")

    playing = False
    print("[回放] 回放结束")


def _do_action(act):
    t = act["type"]
    if t == "key_down":
        k = _name_to_key(act["key"])
        if k is not None:
            keyboard.press(k)
    elif t == "key_up":
        k = _name_to_key(act["key"])
        if k is not None:
            keyboard.release(k)
    elif t == "mouse_click":
        mouse.position = (act["x"], act["y"])
        btn = Button.left if act.get("button") == "left" else (Button.right if act.get("button") == "right" else Button.middle)
        mouse.click(btn, 1)
    else:
        print(f"[回放] 未知动作类型：{t}")


def _play_worker():
    global playing
    # F8 按下后给 3 秒倒计时，让用户切到游戏
    for i in range(3, 0, -1):
        if stop_all:
            return
        print(f"[回放] {i} 秒后开始……")
        time.sleep(1)
    if last_record_file:
        _play_file(last_record_file)
    else:
        print("[回放] 还没有录制文件，请先按 F7 录制")


# ------------------------- 控制热键 -------------------------
# 控制键（F7/F8/F9/Esc）同样要过滤系统自动重复，否则按住 F7 的瞬间
# 会连续触发多次 on_ctrl_press，导致「开始录制」和「停止录制」几乎同时发生。
ctrl_keys_down = set()


def _ctrl_key_name(key):
    """把控制键转成稳定字符串名（用于去重集合）"""
    return getattr(key, "name", str(key))


def on_ctrl_press(key):
    """控制热键：F7 录制开关、F8 回放、F9 停止、Esc 退出"""
    global recording, recording_started, record_start_time, record_actions
    global playing, stop_all, exiting

    # 已触发退出后，不再响应任何控制键（防止 F7 停止后又立刻被误触发）
    if exiting:
        return

    # 过滤自动重复：控制键已处于按下状态时，忽略重复的 press 事件
    kname = _ctrl_key_name(key)
    if kname in ctrl_keys_down:
        return
    ctrl_keys_down.add(kname)

    if key == kb.Key.f7:
        if not recording:
            # 开始录制
            recording = True
            recording_started = True
            record_start_time = time.perf_counter()
            record_actions = []
            pressed_keys.clear()
            key_down_time.clear()
            print("[录制] 开始录制……（再按 F7 停止并保存）")
        else:
            # 停止录制并保存，然后立即退出（录制器一次负责录一段）
            recording = False
            _save_record()
            record_actions = []
            print("[录制] 录制完成，录制器退出")
            exiting = True          # 立即标记退出，阻止后续 F7 误触发
            stop_all = True
            _graceful_exit()

    elif key == kb.Key.f8:
        if not playing and not recording:
            # 回放最近一次录制（后台线程，避免阻塞监听）
            threading.Thread(target=_play_worker, daemon=True).start()
        else:
            print("[回放] 当前正在录制或回放中，忽略 F8")

    elif key == kb.Key.f9:
        # F9 只负责停止回放；录制由 F7 专门控制（开始/停止并保存）
        playing = False
        print("[控制] 已停止回放")

    elif key == kb.Key.esc:
        exiting = True
        stop_all = True
        playing = False
        recording = False
        print("正在退出脚本……")
        _graceful_exit()
        return False  # 停止监听


def _graceful_exit():
    """保存输出落盘后退出整个进程。

    用单独线程延迟退出，给 print 缓冲和文件写入一点时间，但会先置好
    exiting 标志（在调用方），确保退出前不会再误触发其他控制键。
    """
    try:
        time.sleep(0.2)
    finally:
        os._exit(0)


# ------------------------- 入口 -------------------------
def main():
    print("=" * 50)
    print("操作录制 / 回放脚本 已启动")
    print("F7  开始录制 / 再按 F7 停止并保存（录完自动退出）")
    print("F8  回放最近一次录制（3 秒倒计时）")
    print("F9  停止回放")
    print("Esc 退出")
    print("=" * 50)

    # 统一键盘监听：控制键（F7/F8/F9/Esc）优先，其余键按录制处理
    def unified_on_press(key):
        if key in (kb.Key.f7, kb.Key.f8, kb.Key.f9, kb.Key.esc):
            on_ctrl_press(key)
            return
        on_press(key)

    def unified_on_release(key):
        # 控制键松开时清除按下状态，以便下次按下能正常触发
        if key in (kb.Key.f7, kb.Key.f8, kb.Key.f9, kb.Key.esc):
            ctrl_keys_down.discard(_ctrl_key_name(key))
            return
        on_release(key)

    kb_listener = kb.Listener(on_press=unified_on_press, on_release=unified_on_release)
    kb_listener.start()

    # 鼠标监听（录制点击）
    mouse_listener = ms.Listener(on_click=on_click)
    mouse_listener.start()

    # 主线程等待，直到 Esc 退出
    try:
        while not stop_all:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    kb_listener.stop()
    mouse_listener.stop()
    print("脚本已退出。")


if __name__ == "__main__":
    main()
