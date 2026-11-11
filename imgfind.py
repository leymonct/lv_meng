# -*- coding: utf-8 -*-
"""
图片定位核心模块
================================
提供「截屏 → 在画面中找目标小图 → 点击其中心」的能力，
供传送脚本等使用。相比固定坐标，图片定位能适应窗口位置/大小变化。

依赖：opencv-python-headless、mss、numpy、pynput
"""

import os
import time
import ctypes

import cv2
import numpy as np
import mss
from pynput.mouse import Controller as MouseController, Button

# DPI 感知，确保截屏坐标和鼠标坐标一致
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

mouse = MouseController()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(BASE_DIR, "tp_imgs")  # 存放目标小图的目录

# 匹配阈值：0~1，越大越严格（要求越像）。默认 0.8。
DEFAULT_THRESHOLD = 0.8


def _ensure_img_dir():
    if not os.path.exists(IMG_DIR):
        os.makedirs(IMG_DIR)


def screenshot():
    """截取整个屏幕，返回 BGR 的 numpy 数组"""
    with mss.mss() as sct:
        monitor = sct.monitors[0]  # 全屏
        img = np.array(sct.grab(monitor))
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)


def _imread_unicode(path):
    """用 numpy + imdecode 读取图片，避免 cv2.imread 对中文路径不支持的问题"""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        return img
    except Exception:
        return None


def find_template(screen_img, template_path, threshold=DEFAULT_THRESHOLD, region=None):
    """在 screen_img 中找 template 小图，返回 (中心x, 中心y) 或 None

    region：可选，限定搜索区域的屏幕坐标 (x1, y1, x2, y2)。
            用于区分外观相似、位置不同的按钮（如缩小/放大按钮）。
    """
    if not os.path.exists(template_path):
        return None
    tpl = _imread_unicode(template_path)
    if tpl is None:
        return None

    h, w = tpl.shape[:2]

    # 若指定了搜索区域，只截取该区域做匹配，并把结果坐标偏移回全屏
    search = screen_img
    offset_x, offset_y = 0, 0
    if region is not None:
        x1, y1, x2, y2 = region
        x1 = max(0, int(x1))
        y1 = max(0, int(y1))
        x2 = min(screen_img.shape[1], int(x2))
        y2 = min(screen_img.shape[0], int(y2))
        if x2 - x1 < w or y2 - y1 < h:
            return None
        search = screen_img[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1

    result = cv2.matchTemplate(search, tpl, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

    if max_val >= threshold:
        cx = max_loc[0] + offset_x + w // 2
        cy = max_loc[1] + offset_y + h // 2
        return (cx, cy, max_val)
    return None


def find_best_template(screen_img, template_paths, threshold=DEFAULT_THRESHOLD, region=None):
    """在多个模板中找最佳匹配，返回 (中心x, 中心y, 相似度, 命中模板路径) 或 None。

    用于「同一目标有多个外观状态」的场景（如要塞入口：角色在/不在时的不同样子）。
    逐个模板匹配，取相似度最高且超过阈值的那一个。
    """
    best = None
    best_score = -1.0
    for tp in template_paths:
        if not os.path.exists(tp):
            continue
        hit = find_template(screen_img, tp, threshold, region)
        if hit and hit[2] > best_score:
            best_score = hit[2]
            best = (hit[0], hit[1], hit[2], tp)
    return best


def find_and_click(template_name, threshold=DEFAULT_THRESHOLD, timeout=5.0,
                   interval=0.5, retries=None, stop_check=None, region=None):
    """在屏幕上找 template_name 对应的小图并点击中心。

    参数：
        template_name  小图文件名（不含扩展名），位于 tp_imgs/ 目录；
                       也可以是「文件名列表」，此时任一模板命中即点击（多外观状态）。
        threshold      匹配阈值（0~1）
        timeout        总超时秒数，超时仍未找到返回 False
        interval       每次尝试间隔秒数
        retries        尝试次数（None 则按 timeout/interval 计算）
        stop_check     可选回调，返回 True 时立即中断（如 Esc 停止）
        region         可选，限定搜索区域 (x1,y1,x2,y2)，区分相似按钮
    返回：
        True  —— 找到并点击成功
        False —— 超时未找到 / 被中断
    """
    _ensure_img_dir()

    # 支持单个名字或名字列表
    if isinstance(template_name, (list, tuple)):
        names = list(template_name)
    else:
        names = [template_name]

    # 构造模板路径列表，并校验
    tpl_paths = []
    for n in names:
        p = os.path.join(IMG_DIR, n + ".png")
        if os.path.exists(p):
            tpl_paths.append(p)
        else:
            print(f"[imgfind] 缺少模板图：{p}")
    if not tpl_paths:
        return False

    start = time.time()
    attempt = 0
    while True:
        # 检查是否被要求中断
        if stop_check is not None and stop_check():
            print(f"[imgfind] 查找「{names}」被中断")
            return False
        attempt += 1
        screen = screenshot()
        hit = find_best_template(screen, tpl_paths, threshold, region)
        if hit:
            cx, cy, score, tpl_path = hit
            mouse.position = (cx, cy)
            time.sleep(0.1)
            mouse.click(Button.left, 1)
            hit_name = os.path.splitext(os.path.basename(tpl_path))[0]
            print(f"[imgfind] 找到「{hit_name}」({cx},{cy}) 相似度 {score:.2f}，已点击")
            return True

        # 判断是否还要继续
        if retries is not None and attempt >= retries:
            break
        if retries is None and (time.time() - start) >= timeout:
            break
        time.sleep(interval)

    print(f"[imgfind] 未找到「{names}」（尝试 {attempt} 次）")
    return False


if __name__ == "__main__":
    # 自测：找某张图并点击
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "test"
    ok = find_and_click(name, timeout=3.0)
    print("结果:", "找到并点击" if ok else "未找到")
