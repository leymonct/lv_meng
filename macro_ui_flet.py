# -*- coding: utf-8 -*-
"""
绿梦小助手 —— Flet 版界面
================================
用 Flet 重写的图形界面，复用 macro_backend.py 里的后端逻辑（MacroManager、
focus_game_window 等）。

  - 深色现代化主题 + 蓝色主色
  - 左侧导航栏（运行 / 脚本管理 / 运行日志）
  - 卡片式布局、图标按钮
  - 传送/启动流程已在后台线程执行，界面不会「未响应」
  - 运行日志独立成页，可滚动回看 + 复制/清空

【运行方式】
    python macro_ui_flet.py
    （需管理员权限，否则无法操作游戏窗口）

【与 macro_backend.py 的关系】
    - 后端：from macro_backend import MacroManager, focus_game_window, ...
    - 界面：本文件全新实现
"""

import os
import sys
import json
import threading
import time
import shutil

import flet as ft

# Windows 原生文件对话框（替代 Flet FilePicker，见 pick_image_file 说明）
try:
    import win32ui
    import win32con
    import win32gui
    _HAS_WIN32UI = True
except Exception:
    _HAS_WIN32UI = False

# Flet 1.x 图标通过 Icons 枚举访问（Icons.Icons.XXX），这里取个短别名
Icons = ft.icons.Icons

# ------------------------------------------------------------------
# 复用后端：直接 import macro_backend 的 MacroManager 和窗口工具
# ------------------------------------------------------------------
from macro_backend import (
    MacroManager,
    focus_game_window,
    focus_ui_window,
    is_admin,
    MACROS,
    RECORD_DIR,
    RECORD_SCREEN,
    GAME_TITLE_KEYWORDS,
    BASE_DIR,
)

# ------------------------- 主题配色 -------------------------
# 深色游戏风：背景深灰蓝，主色青蓝（绿梦主题），文字浅色
COLOR_BG = "#0f172a"          # 主背景
COLOR_SURFACE = "#1e293b"     # 卡片/面板背景
COLOR_SURFACE_2 = "#263449"   # 次级面板
COLOR_PRIMARY = "#38bdf8"     # 主色（青蓝）
COLOR_PRIMARY_DARK = "#0ea5e9"
COLOR_ACCENT = "#22d3ee"
COLOR_TEXT = "#e2e8f0"        # 主文字
COLOR_TEXT_DIM = "#94a3b8"    # 次要文字
COLOR_SUCCESS = "#34d399"     # 成功绿
COLOR_WARN = "#fbbf24"        # 警告黄
COLOR_ERROR = "#f87171"       # 错误红
COLOR_LOG_BG = "#0b1220"      # 日志控制台背景
COLOR_LOG_TEXT = "#cbd5e1"

# 状态栏文字色随状态切换
_STATUS_COLORS = {
    "ok": COLOR_SUCCESS,
    "warn": COLOR_WARN,
    "err": COLOR_ERROR,
    "info": COLOR_TEXT_DIM,
}


def _rel(base_name):
    """把相对 BASE_DIR 的文件名转成绝对路径"""
    return os.path.join(BASE_DIR, base_name)


def _find_ui_hwnd():
    """枚举找到本程序自己的 UI 窗口句柄（标题含「绿梦小助手」），找不到返回 None。

    用作原生文件对话框的父窗口，让对话框作为 UI 的模态子窗口显示在最上层。
    """
    if not _HAS_WIN32UI:
        return None
    try:
        found = []

        def _cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                if "绿梦小助手" in win32gui.GetWindowText(hwnd):
                    found.append(hwnd)

        win32gui.EnumWindows(_cb, None)
        return found[0] if found else None
    except Exception:
        return None


def pick_image_file(dialog_title="选择图片"):
    """用 Windows 原生文件对话框选择一张图片，返回绝对路径或 None。

    为什么不用 Flet FilePicker：
    Flet 1.1.2 桌面端的 FilePicker.pick_files() 是 async 方法，依赖
    session.invoke_method 往返 + service 注册机制。实测在该应用里会因
    session 重置 / service 被 unregister_services 按 refcount 误注销，导致
    pick_files 永久挂起（「选图卡死」）。改用 win32ui.CreateFileDialog 直接调
    Windows 原生 GetOpenFileName，同步返回路径，不依赖任何 Flet 机制，最可靠。

    注意：本函数是同步阻塞的，必须在后台线程调用（不能在 Flet 事件循环里跑）。
    """
    if not _HAS_WIN32UI:
        # 兜底：无 pywin32 时退化为简单输入框（几乎不会发生，pywin32 是项目依赖）
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            title=dialog_title,
            filetypes=[("图片文件", "*.png *.jpg *.jpeg *.bmp"), ("所有文件", "*.*")],
        )
        root.destroy()
        return path or None

    try:
        # 把 UI 窗口作为父窗口，让原生文件对话框作为其模态子窗口、显示在最上层，
        # 避免对话框弹到 Flet 窗口后面被盖住、用户以为没反应。
        # CreateFileDialog 的第 6 个参数 pParentWnd 需要 PyCWnd 对象（不是 int hwnd），
        # 用 CreateWindowFromHandle 把 hwnd 转成 PyCWnd；找不到 UI 窗口时传 None（独立顶层窗口）。
        parent_cwnd = None
        parent_hwnd = _find_ui_hwnd()
        if parent_hwnd:
            try:
                parent_cwnd = win32ui.CreateWindowFromHandle(parent_hwnd)
            except Exception:
                parent_cwnd = None
        dlg = win32ui.CreateFileDialog(
            1,  # 1 = 打开文件（非保存）
            None,       # lpszDefExt（默认扩展名）
            None,       # lpszFileName（默认文件名）
            win32con.OFN_FILEMUSTEXIST | win32con.OFN_HIDEREADONLY,
            "图片文件 (*.png;*.jpg;*.jpeg;*.bmp)|*.png;*.jpg;*.jpeg;*.bmp|所有文件 (*.*)|*.*||",
            parent_cwnd,  # pParentWnd：父窗口（PyCWnd），让对话框置顶
        )
        dlg.SetOFNTitle(dialog_title)
        if dlg.DoModal() == 1:  # IDOK
            return dlg.GetPathName()
        return None
    except Exception as ex:
        # 打开失败（如被安全软件拦截）时落盘诊断，返回 None 走「取消」逻辑
        try:
            with open(_rel("poll_error.txt"), "a", encoding="utf-8") as ef:
                ef.write(f"[native picker error] {ex!r}\n")
        except Exception:
            pass
        return None


# ==================================================================
# 主界面
# ==================================================================
class LuminApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.manager = MacroManager()

        # 动态脚本列表：{显示名: 相对路径}
        self._dynamic_scripts = {}
        self._combo_values = [m["name"] for m in MACROS]

        # 界面状态
        self._selected_script = None     # 当前选中脚本相对路径
        self._teleport_points = []       # 所有传送点名称
        self._regions = []               # 所有地区名
        self._flow_running = False       # 「传送+启动」流程是否进行中

        # 页面基础设置
        page.title = "绿梦小助手"
        page.theme_mode = ft.ThemeMode.DARK
        page.padding = 0
        page.bgcolor = COLOR_BG
        # 统一字体：微软雅黑（Windows 自带，中文渲染清晰统一）
        page.theme = ft.Theme(
            color_scheme_seed=COLOR_PRIMARY,
            font_family="Microsoft YaHei",
        )
        page.window.width = 960
        page.window.height = 680
        page.window.min_width = 820
        page.window.min_height = 600
        # 窗口标题栏图标（Windows 用 .ico 效果最好）
        _ico = _rel("Lumin.ico")
        if os.path.exists(_ico):
            page.window.icon = _ico

        # 构建 UI
        self._build()

        # 初始加载
        self.refresh_dynamic_scripts()
        # 传送点/地区列表的首次加载会 import teleport（含 cv2 + pynput 实例化，
        # 约 1.9 秒）。放到后台线程预热，避免阻塞 UI 启动；后续 on_config_teleport
        # 里再读就是缓存命中、毫秒级返回。
        threading.Thread(target=self._load_teleport_points, daemon=True).start()

        # 日志面板指纹（用于判断是否需要重建日志行列表）
        self._log_fingerprint = None

        # 启动轮询（500ms 刷新运行状态/日志/统计/录制器状态）
        self._last_running = None
        self._last_rec = None
        self._start_polling()

    # ---------------- UI 构建 ----------------
    def _build(self):
        # 选图已改用 Windows 原生对话框（见 pick_image_file），不再使用 Flet FilePicker。
        # Flet 1.1.2 的 FilePicker 是 Service，在桌面端会因 session 重置 / service 被
        # unregister_services 按 refcount 误注销，导致 pick_files() 永久挂起（选图卡死）。
        # 原生对话框同步返回路径、不依赖任何 Flet 机制，从根源上规避该问题。

        admin = is_admin()
        admin_badge = ft.Container(
            content=ft.Text(
                "管理员" if admin else "未提权",
                size=12,
                weight=ft.FontWeight.BOLD,
                color="#052e16" if admin else "#450a0a",
            ),
            bgcolor=COLOR_SUCCESS if admin else COLOR_ERROR,
            border_radius=999,
            padding=ft.Padding(left=10, right=10, top=3, bottom=3),
        )

        # 标题栏
        header = ft.Container(
            content=ft.Row(
                [
                    # 标题栏 Logo：鲸鱼娘图标（app_icon.png，随程序打包）
                    ft.Container(
                        content=ft.Image(
                            src=_rel("app_icon.png"),
                            width=32,
                            height=32,
                            fit="contain",
                        ),
                        width=36,
                        height=36,
                        border_radius=8,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                    ),
                    ft.Column(
                        [
                            ft.Text("绿梦小助手", size=20, weight=ft.FontWeight.BOLD, color=COLOR_TEXT),
                            ft.Text("Lumin Assistant", size=12, color=COLOR_TEXT_DIM),
                        ],
                        spacing=0,
                    ),
                    ft.Container(expand=True),
                    admin_badge,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=20, right=20, top=14, bottom=14),
            bgcolor=COLOR_SURFACE,
        )

        # 左侧导航
        self.nav = ft.NavigationRail(
            selected_index=0,
            label_type=ft.NavigationRailLabelType.ALL,
            min_width=80,
            min_extended_width=180,
            bgcolor=COLOR_SURFACE,
            indicator_color=COLOR_PRIMARY_DARK,
            destinations=[
                ft.NavigationRailDestination(
                    icon=Icons.PLAY_CIRCLE_FILL_ROUNDED,
                    selected_icon=Icons.PLAY_CIRCLE_FILL,
                    label="运行",
                ),
                ft.NavigationRailDestination(
                    icon=Icons.SETTINGS_OUTLINED,
                    selected_icon=Icons.SETTINGS,
                    label="脚本管理",
                ),
                ft.NavigationRailDestination(
                    icon=Icons.ARTICLE_OUTLINED,
                    selected_icon=Icons.ARTICLE,
                    label="运行日志",
                ),
            ],
            on_change=self._on_nav_change,
        )

        # 三个页面
        self.run_view = self._build_run_view()
        self.mgr_view = self._build_mgr_view()
        self.log_view_page = self._build_log_view()

        self.body = ft.Container(
            content=ft.Row(
                [
                    self.nav,
                    ft.VerticalDivider(width=1, color="#334155"),
                    ft.Container(
                        content=ft.Stack(
                            [
                                self.run_view,
                                self.mgr_view,
                                self.log_view_page,
                            ]
                        ),
                        expand=True,
                        padding=20,
                    ),
                ],
                spacing=0,
            ),
            expand=True,
        )

        # 状态栏
        self.status_text = ft.Text("就绪", size=12, color=COLOR_TEXT_DIM)
        self.status_dot = ft.Icon(Icons.CIRCLE, size=8, color=COLOR_TEXT_DIM)
        status_bar = ft.Container(
            content=ft.Row(
                [
                    self.status_dot,
                    self.status_text,
                    ft.Container(expand=True),
                    ft.Text(
                        f"分辨率基准 {RECORD_SCREEN[0]}×{RECORD_SCREEN[1]} · 关键词 {'/'.join(GAME_TITLE_KEYWORDS)}",
                        size=11,
                        color=COLOR_TEXT_DIM,
                    ),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.Padding(left=20, right=20, top=10, bottom=10),
            bgcolor=COLOR_SURFACE,
        )

        # 根布局
        self.page.add(
            ft.Column(
                [
                    header,
                    self.body,
                    status_bar,
                ],
                spacing=0,
                expand=True,
            )
        )

        # 初始显示运行页
        self.mgr_view.visible = False
        self.log_view_page.visible = False

    # ---------------- 运行页 ----------------
    def _build_run_view(self):
        # 脚本选择
        self.script_dropdown = ft.Dropdown(
            label="选择脚本",
            options=[ft.dropdown.Option(m["name"]) for m in MACROS],
            value=self._combo_values[0] if self._combo_values else None,
            expand=True,
            bgcolor=COLOR_SURFACE_2,
            border=ft.OutlineInputBorder(side=ft.BorderSide(width=1, color="#334155")),
            text_size=14,
            on_select=self._on_combo_change,
        )

        # 自动传送开关
        self.teleport_switch = ft.Switch(
            label="启动时自动传送到目标地图",
            value=False,
            active_color=COLOR_PRIMARY,
            on_change=self._on_teleport_toggle,
        )
        self.teleport_hint = ft.Text("", size=12, color=COLOR_TEXT_DIM)

        # 循环次数
        self.loop_field = ft.TextField(
            value="0",
            label="循环次数（0 = 无限）",
            width=200,
            keyboard_type=ft.KeyboardType.NUMBER,
            bgcolor=COLOR_SURFACE_2,
            border=ft.OutlineInputBorder(side=ft.BorderSide(width=1, color="#334155")),
            text_size=14,
        )

        # 启动/停止按钮
        self.btn_start = ft.FilledButton(
            content="启动（自动切换游戏）",
            icon=Icons.PLAY_ARROW,
            expand=True,
            style=ft.ButtonStyle(bgcolor=COLOR_PRIMARY, color="#0b1220", padding=16),
            on_click=self.on_start,
        )
        self.btn_stop = ft.OutlinedButton(
            content="停止",
            icon=Icons.STOP,
            expand=True,
            style=ft.ButtonStyle(
                color=COLOR_ERROR,
                side=ft.BorderSide(1, COLOR_ERROR),
                padding=16,
            ),
            on_click=self.on_stop,
            disabled=True,
        )

        # 快捷键提示条：启动后动态提示「F8 开始/暂停 · Esc 退出」，未运行时给出操作引导
        self.run_hint = ft.Text(
            "启动后：F8 开始/暂停 · Esc 退出",
            size=13,
            color=COLOR_TEXT_DIM,
            weight=ft.FontWeight.W_500,
        )

        # 运行卡片
        run_card = ft.Container(
            content=ft.Column(
                [
                    ft.Text("运行", size=16, weight=ft.FontWeight.BOLD, color=COLOR_TEXT),
                    ft.Container(height=8),
                    self.script_dropdown,
                    ft.Container(height=12),
                    # 传送开关 + 循环次数 放同一行，布局更紧凑
                    ft.Row(
                        [
                            ft.Container(
                                content=self.teleport_switch,
                                expand=True,
                            ),
                            self.loop_field,
                        ],
                        spacing=16,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.teleport_hint,
                    ft.Container(height=16),
                    ft.Row([self.btn_start, self.btn_stop], spacing=12),
                    ft.Container(height=12),
                    # 快捷键提示条：醒目标注运行时的控制方式
                    ft.Container(
                        content=ft.Row(
                            [
                                ft.Icon(Icons.KEYBOARD, size=15, color=COLOR_PRIMARY),
                                self.run_hint,
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        bgcolor=COLOR_SURFACE_2,
                        border_radius=8,
                        padding=ft.Padding(left=12, right=12, top=8, bottom=8),
                    ),
                ],
            ),
            bgcolor=COLOR_SURFACE,
            border_radius=16,
            padding=20,
        )

        # 日志看板（stats_text 保留在运行页做运行统计提示；日志本体移到独立「运行日志」页）
        self.stats_text = ft.Text("尚未开始运行", size=12, color=COLOR_TEXT_DIM)
        # 日志用 ListView 承载：每条日志一行，可上下滚动回看完整历史。
        # auto_scroll=False —— 不自动追着最新一条滚，让用户能安心阅读，手动滚动查看全部。
        self.log_list = ft.ListView(
            controls=[],
            auto_scroll=False,
            spacing=2,
            padding=8,
            expand=True,
        )

        # 运行页底部只放一个统计小条，日志入口提示用户到「运行日志」页查看
        stats_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(Icons.INSIGHTS, size=16, color=COLOR_PRIMARY),
                    self.stats_text,
                    ft.Container(expand=True),
                    ft.Text("完整日志请查看左侧「运行日志」页", size=11, color=COLOR_TEXT_DIM),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=COLOR_SURFACE,
            border_radius=12,
            padding=ft.Padding(left=16, right=16, top=12, bottom=12),
        )

        return ft.Column(
            [
                run_card,
                ft.Container(height=16),
                stats_bar,
            ],
            expand=True,
        )

    # ---------------- 脚本管理页 ----------------
    def _build_mgr_view(self):
        def _card(title, subtitle, btn):
            return ft.Container(
                content=ft.Column(
                    [
                        ft.Row(
                            [
                                ft.Container(expand=True, content=ft.Column(
                                    [
                                        ft.Text(title, size=15, weight=ft.FontWeight.W_600, color=COLOR_TEXT),
                                        ft.Text(subtitle, size=12, color=COLOR_TEXT_DIM),
                                    ],
                                    spacing=2,
                                )),
                                btn,
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        )
                    ]
                ),
                bgcolor=COLOR_SURFACE,
                border_radius=14,
                padding=ft.Padding(left=16, right=16, top=14, bottom=14),
            )

        self.btn_record = ft.FilledButton(
            content="启动录制",
            icon=Icons.FIBER_MANUAL_RECORD,
            style=ft.ButtonStyle(bgcolor=COLOR_PRIMARY_DARK, color="#0b1220"),
            on_click=self.on_record,
        )
        self.btn_record_stop = ft.OutlinedButton(
            content="停止",
            icon=Icons.STOP,
            on_click=self.on_record_stop,
            disabled=True,
        )
        record_btn = ft.Row([self.btn_record, self.btn_record_stop], spacing=8)

        self.btn_rest_img = ft.FilledButton(
            content="选择休息点图",
            icon=Icons.IMAGE_OUTLINED,
            on_click=self.on_select_rest_image,
        )
        self.btn_rename = ft.FilledButton(
            content="重命名",
            icon=Icons.EDIT_OUTLINED,
            on_click=self.on_rename_script,
        )
        self.btn_teleport = ft.FilledButton(
            content="配置传送点",
            icon=Icons.PLACE_OUTLINED,
            on_click=self.on_config_teleport,
        )
        self.btn_delete = ft.FilledButton(
            content="删除",
            icon=Icons.DELETE_OUTLINE,
            on_click=self.on_delete_script,
            style=ft.ButtonStyle(bgcolor=COLOR_ERROR, color="#450a0a"),
        )

        hint_bar = ft.Container(
            content=ft.Text(
                "提示：录制 → 切到游戏按 F7 开始/停止，Esc 退出；录完自动生成脚本。",
                size=12,
                color=COLOR_TEXT_DIM,
            ),
            bgcolor=COLOR_SURFACE_2,
            border_radius=10,
            padding=12,
        )

        return ft.Column(
            [
                ft.Text("脚本管理", size=16, weight=ft.FontWeight.BOLD, color=COLOR_TEXT),
                ft.Container(height=4),
                ft.Text("管理录制生成的脚本", size=12, color=COLOR_TEXT_DIM),
                ft.Container(height=16),
                _card("操作录制", "录制键鼠操作，自动生成脚本并加入列表", record_btn),
                ft.Container(height=12),
                _card("休息点图片", "选中录制脚本后选一张休息点图标，启用就近传送", self.btn_rest_img),
                ft.Container(height=12),
                _card("重命名脚本", "给录制脚本起个好记的显示名（不重命名文件）", self.btn_rename),
                ft.Container(height=12),
                _card("配置传送点", "选地区 + 上传图标，启动时自动传送到目标地图", self.btn_teleport),
                ft.Container(height=12),
                _card("删除脚本", "删除录制脚本及关联图片、配置（二次确认）", self.btn_delete),
                ft.Container(height=16),
                hint_bar,
            ],
            scroll=ft.ScrollMode.AUTO,
        )

    # ---------------- 运行日志页 ----------------
    def _build_log_view(self):
        """独立「运行日志」页：完整的日志面板 + 清空/复制按钮，可滚动回看全部历史。"""
        btn_clear_log = ft.IconButton(
            icon=Icons.DELETE_SWEEP_OUTLINED,
            icon_color=COLOR_TEXT_DIM,
            tooltip="清空日志",
            on_click=self.on_clear_log,
        )
        btn_copy_log = ft.IconButton(
            icon=Icons.CONTENT_COPY,
            icon_color=COLOR_TEXT_DIM,
            tooltip="复制全部日志",
            on_click=self.on_copy_log,
        )
        btn_refresh_log = ft.IconButton(
            icon=Icons.REFRESH,
            icon_color=COLOR_TEXT_DIM,
            tooltip="立即刷新",
            on_click=lambda e: self._refresh_log(),
        )

        # 日志容器：背景用 Container 的 bgcolor 实现，内部 ListView 负责滚动
        log_card = ft.Container(
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text("运行日志", size=16, weight=ft.FontWeight.BOLD, color=COLOR_TEXT),
                            ft.Container(expand=True),
                            self.stats_text,
                            btn_refresh_log,
                            btn_copy_log,
                            btn_clear_log,
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(height=4),
                    ft.Text(
                        "运行产生的全部输出，可上下滚动回看；日志文件：macro_log.txt",
                        size=11,
                        color=COLOR_TEXT_DIM,
                    ),
                    ft.Container(height=10),
                    ft.Container(
                        content=self.log_list,
                        bgcolor=COLOR_LOG_BG,
                        border_radius=10,
                        expand=True,
                    ),
                ],
                expand=True,
            ),
            bgcolor=COLOR_SURFACE,
            border_radius=16,
            padding=20,
            expand=True,
        )

        return log_card

    # ---------------- 导航切换 ----------------
    def _on_nav_change(self, e):
        idx = e.control.selected_index
        self.run_view.visible = idx == 0
        self.mgr_view.visible = idx == 1
        self.log_view_page.visible = idx == 2
        self.page.update()

    # ---------------- 状态栏 ----------------
    def set_status(self, text, kind="info", refresh=True):
        self.status_text.value = text
        self.status_text.color = _STATUS_COLORS.get(kind, COLOR_TEXT_DIM)
        # 状态圆点随状态变色：运行中(ok)绿色、警告黄、错误红、其它灰色
        self.status_dot.color = _STATUS_COLORS.get(kind, COLOR_TEXT_DIM)
        if refresh:
            self.page.update()

    def _set_run_hint_running(self):
        """把运行页快捷键提示条切换为「运行中」醒目状态"""
        self.run_hint.value = "运行中：按 F8 开始/暂停循环 · Esc 退出"
        self.run_hint.color = COLOR_PRIMARY

    def _set_run_hint_idle(self):
        """把运行页快捷键提示条恢复为「未运行」引导文案"""
        self.run_hint.value = "启动后：F8 开始/暂停 · Esc 退出"
        self.run_hint.color = COLOR_TEXT_DIM

    def _snack(self, msg, color=COLOR_TEXT_DIM):
        self.page.show_dialog(ft.SnackBar(ft.Text(msg, color="#0b1220"), bgcolor=color))

    # ---------------- 脚本列表 ----------------
    def get_selected_script(self):
        """返回当前选中脚本的相对路径（相对 BASE_DIR）"""
        return self._selected_script

    def get_selected_display(self):
        return self.script_dropdown.value or ""

    def refresh_dynamic_scripts(self):
        """重建下拉列表：录制脚本"""
        current = self.script_dropdown.value
        static_names = [m["name"] for m in MACROS]
        new_dynamic = {}
        for display, fname in self.manager.scan_recorded_scripts():
            new_dynamic[display] = os.path.join(RECORD_DIR, fname)

        old_dynamic = self._dynamic_scripts
        added = set(new_dynamic) - set(old_dynamic)
        self._dynamic_scripts = new_dynamic
        self._combo_values = static_names + list(new_dynamic.keys())

        # 重建下拉选项
        self.script_dropdown.options = [ft.dropdown.Option(v) for v in self._combo_values]

        # 选中项恢复：优先最新加入，否则恢复原选中，否则第一个
        if added:
            newest = max(added, key=lambda d: os.path.basename(new_dynamic[d]))
            self.script_dropdown.value = newest
        elif current in self._combo_values:
            self.script_dropdown.value = current
        elif self._combo_values:
            self.script_dropdown.value = self._combo_values[0]

        self._on_combo_change()
        self.page.update()

    def _load_teleport_points(self):
        self._teleport_points = self.manager.get_available_teleport_points()
        self._regions = self.manager.get_available_regions()

    # ---------------- 选择脚本变化 ----------------
    def _on_combo_change(self, e=None):
        display = self.script_dropdown.value
        if display in self._dynamic_scripts:
            self._selected_script = self._dynamic_scripts[display]
        else:
            for m in MACROS:
                if m["name"] == display:
                    self._selected_script = m["script"]
                    break
            else:
                self._selected_script = None

        # 更新自动传送提示
        self._update_teleport_hint()

    def _update_teleport_hint(self):
        script = self._selected_script
        if not script:
            self.teleport_hint.value = ""
            return
        fname = os.path.basename(script)
        tp = None
        for m in MACROS:
            if m["script"] == script and m.get("teleport"):
                tp = m["teleport"]
                break
        if tp is None:
            tp = self.manager.get_script_teleport(fname)
        if tp:
            self.teleport_hint.value = f"传送点：{tp}"
            self.teleport_hint.color = COLOR_SUCCESS
        else:
            self.teleport_hint.value = "未配置传送点"
            self.teleport_hint.color = COLOR_TEXT_DIM

    def _on_teleport_toggle(self, e):
        self._update_teleport_hint()

    # ---------------- 启动 / 停止 ----------------
    def on_start(self, e):
        script = self.get_selected_script()
        if not script:
            self._snack("请先选择一个", COLOR_WARN)
            return

        # 启动前体检
        blockers, warnings = self.manager.preflight(script)
        if blockers:
            self._show_dialog(
                "启动前检查未通过",
                "\n".join("· " + b for b in blockers),
                error=True,
            )
            self.set_status("启动前检查未通过", "err")
            return
        if warnings:
            # Flet 没有同步 confirm，用对话框 + 回调
            self._show_confirm(
                "启动前提示",
                "\n".join("· " + w for w in warnings),
                on_yes=lambda: self._do_start(script),
                yes_text="仍要启动",
                no_text="取消",
            )
            return

        self._do_start(script)

    def _do_start(self, script):
        tp = None
        if self.teleport_switch.value:
            tp = self.manager.get_script_teleport(os.path.basename(script))
            # 静态脚本从 MACROS 取
            for m in MACROS:
                if m["script"] == script and m.get("teleport"):
                    tp = m["teleport"]
                    break

        # 循环次数
        try:
            max_loops = int(self.loop_field.value.strip())
        except ValueError:
            max_loops = 0
        if max_loops < 0:
            max_loops = 0

        # 后台线程执行「切窗口 → 传送 → 启动」
        # 注意：focus_game_window 里的 SetForegroundWindow 等 win32 前台调用，
        # 必须在独立线程执行（Flet 事件回调线程里调用会抛错误码 0）。
        self._flow_running = True
        self.sync_buttons()
        threading.Thread(target=self._start_flow, args=(script, tp, max_loops), daemon=True).start()

    def _start_flow(self, script, tp, max_loops):
        try:
            # 第一步：切换游戏窗口到前台（在后台线程执行，避免前台锁定失败）
            self._ui(lambda: self.set_status("正在切换到游戏窗口……", "info"))
            ok_focus, msg_focus = focus_game_window()
            if not ok_focus:
                self._ui(lambda: self._show_dialog("切换失败", msg_focus, error=True))
                self._ui(lambda: self.set_status(msg_focus, "err"))
                return

            if tp:
                self._ui(lambda: self.set_status(f"正在传送到：{tp}……", "info"))
                ok_tp, msg_tp = self.manager.run_teleport(
                    tp, on_cancel_check=lambda: self.manager._teleport_cancelled
                )
                if not ok_tp:
                    self._ui(lambda: self.set_status(msg_tp, "warn"))
                    if "取消" not in msg_tp:
                        self._ui(lambda: self._show_dialog("传送失败", msg_tp, error=True))
                    return
                self._ui(lambda: self.set_status(f"{msg_tp}，正在启动刷怪……", "info"))

            # 启动脚本（会覆盖写 macro_log.txt），启动成功后清空日志面板，等待轮询刷新新日志
            ok2, msg2 = self.manager.start(script, max_loops=max_loops)
            self._ui(self._clear_log)
            if ok2:
                self._ui(self.sync_buttons)
                # 启动成功：更新状态栏 + 醒目提示条（F8 开始/暂停、Esc 退出）
                if max_loops > 0:
                    self._ui(lambda: self.set_status(f"运行中 · 循环 {max_loops} 轮（F8 开始 / Esc 退出）", "ok"))
                else:
                    self._ui(lambda: self.set_status("运行中 · 无限循环（F8 开始 / Esc 退出）", "ok"))
                self._ui(lambda: self._set_run_hint_running())
                # 提示条（run_hint）已在运行页显示「F8 开始/暂停 · Esc 退出」，
                # 用户启动前就能看到，无需再弹系统弹窗。
            else:
                self._ui(lambda: self._show_dialog("启动失败", msg2, error=True))
                self._ui(lambda: self.set_status(msg2, "err"))
                self._ui(self.sync_buttons)
        finally:
            self.manager._teleport_cancelled = False
            self._flow_running = False
            self._ui(self.sync_buttons)
            # 若脚本最终没有在运行（切换失败 / 传送失败 / 启动失败），焦点可能
            # 停在游戏窗口，这里把界面切回前台，避免用户「切不回 UI」。
            # 脚本正常启动并运行时不切（要让脚本操作游戏），等轮询检测到停止再切。
            try:
                if not self.manager.is_running():
                    self._refocus_ui()
            except Exception:
                pass

    def _ui(self, fn):
        """从后台线程更新 UI。

        Flet 1.1.2 的 page.update() 内部通过线程安全的 send_queue 发送消息，
        因此从任意线程直接调用回调（内部改控件属性 + page.update()）是安全的。
        不再依赖 page.run_thread（它依赖 session，未就绪时会抛 RuntimeError）。
        """
        try:
            fn()
        except Exception as ex:
            # 后台线程的 UI 更新失败不能中断主流程，落盘诊断即可
            try:
                with open(_rel("poll_error.txt"), "a", encoding="utf-8") as ef:
                    ef.write(f"[ui error] {ex!r}\n")
            except Exception:
                pass

    def _refocus_ui(self):
        """脚本结束后，把界面窗口重新切回前台（在后台线程调用）。

        脚本运行时 focus_game_window() 把游戏切到前台；跑完后焦点仍停在游戏
        （尤其全屏游戏），用户看不到也点不到 UI。这里调用后端的 focus_ui_window
        把标题含「绿梦小助手」的窗口切回前台。
        """
        try:
            focus_ui_window()
        except Exception:
            pass

    def on_stop(self, e):
        if self.manager.stop_teleport():
            self.set_status("已取消传送", "warn")
            self.sync_buttons()
            return
        ok, msg = self.manager.stop()
        self.sync_buttons()
        if ok:
            self.set_status("已停止（可点击启动）", "info")
        else:
            self.set_status("当前没有运行中", "info")

    def sync_buttons(self, refresh=True):
        running = self.manager.is_running() or self._flow_running
        self.btn_start.disabled = running
        self.btn_stop.disabled = not running
        if running:
            # 运行中：停止按钮变成红色实心高亮（强烈提示「正在运行，点我停止」），
            # 启动按钮保持默认样式但被 disabled 置灰。
            self.btn_stop.style = ft.ButtonStyle(
                bgcolor=COLOR_ERROR, color="#450a0a", padding=16
            )
        else:
            # 已停止：停止按钮恢复红色描边（弱化），启动按钮恢复蓝色实心。
            self.btn_stop.style = ft.ButtonStyle(
                color=COLOR_ERROR, side=ft.BorderSide(1, COLOR_ERROR), padding=16
            )
        if refresh:
            self.page.update()

    # ---------------- 日志 ----------------
    def _clear_log(self):
        self.log_list.controls = [self._log_placeholder_row()]
        self._log_fingerprint = (1, "等待日志……", "等待日志……")
        self.stats_text.value = "尚未开始运行"
        self.page.update()

    def _log_placeholder_row(self):
        return ft.Text("等待日志……", size=12, color=COLOR_LOG_TEXT)

    def on_clear_log(self, e):
        self._clear_log()
        try:
            with open(_rel("macro_log.txt"), "w", encoding="utf-8", buffering=1):
                pass
        except Exception:
            pass

    def on_copy_log(self, e):
        """把当前日志全文复制到剪贴板"""
        log_path = _rel("macro_log.txt")
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            content = ""
        if not content:
            self._snack("日志为空，无可复制", COLOR_WARN)
            return
        try:
            self.page.set_clipboard(content)
            self._snack(f"已复制 {len(content)} 字符日志到剪贴板", COLOR_SUCCESS)
        except Exception:
            self._snack("复制失败", COLOR_ERROR)

    def _refresh_log(self):
        """把 macro_log.txt 的完整内容刷新到日志面板。

        逐行读入，每条日志一个 Text 行放进 ListView，日志多了可上下滚动完整回看。
        每次直接读全量、与当前已渲染内容对比，不同才重建——不做基于大小/offset 的
        增量判断，彻底规避「覆盖写导致文件变短 → offset 越界」与「大小判断竞态漏读」。
        """
        log_path = _rel("macro_log.txt")
        if not os.path.exists(log_path):
            return
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception:
            return
        if not content:
            content = "等待日志……"

        # 生成行列表（保留空行为空串，方便对齐）
        lines = content.split("\n")
        if lines and lines[-1] == "":
            lines = lines[:-1]  # 去掉末尾换行产生的空行

        # 用 (行数, 首行, 末行) 做轻量指纹，判断是否需要重建
        fingerprint = (len(lines), lines[0] if lines else "", lines[-1] if lines else "")
        if fingerprint == self._log_fingerprint:
            return
        self._log_fingerprint = fingerprint

        self.log_list.controls = [
            ft.Text(line if line else " ", size=12, color=COLOR_LOG_TEXT, selectable=True)
            for line in lines
        ]
        # 精确 patch 单个控件，绕过整页 diff 对深层控件的潜在漏检
        try:
            self.log_list.update()
        except Exception:
            pass

    def _refresh_stats(self):
        path = _rel("runs.jsonl")
        if not os.path.exists(path):
            return
        rounds = 0
        total = 0.0
        tp_ok = 0
        tp_fail = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    rounds += 1
                    total += float(rec.get("duration", 0))
                    if rec.get("teleport_ok") is True:
                        tp_ok += 1
                    elif rec.get("teleport_ok") is False:
                        tp_fail += 1
        except Exception:
            return
        if rounds == 0:
            return
        avg = total / rounds
        tp_total = tp_ok + tp_fail
        tp_rate = f"{tp_ok}/{tp_total}" if tp_total else "—"
        self.stats_text.value = f"轮数 {rounds} · 平均 {avg:.1f}s · 传送 {tp_rate}"
        # 不在这里 page.update()，由轮询 poll() 末尾统一 update

    # ---------------- 录制 ----------------
    def on_record(self, e):
        ok, msg = focus_game_window()
        if not ok:
            self._show_dialog("切换失败", msg, error=True)
            self.set_status(msg, "err")
            return
        ok, msg = self.manager.start_recorder()
        if ok:
            self.btn_record.disabled = True
            self.btn_record_stop.disabled = False
            self.set_status("录制器已启动，切到游戏按 F7 开始录制", "ok")
        else:
            self._snack(msg, COLOR_WARN)
            self.set_status(msg, "warn")
        self.page.update()

    def on_record_stop(self, e):
        ok, msg = self.manager.stop_recorder()
        self.btn_record.disabled = False
        self.btn_record_stop.disabled = True
        self.set_status(msg, "info")
        self.refresh_dynamic_scripts()
        self.page.update()

    # ---------------- 休息点图 ----------------
    def on_select_rest_image(self, e):
        script = self.get_selected_script()
        if not script:
            self._snack("请先选择一个录制脚本", COLOR_WARN)
            return
        if not script.startswith(RECORD_DIR + os.sep):
            self._snack("休息点图只针对录制脚本，静态请手动配置", COLOR_WARN)
            return

        display = self.get_selected_display()
        fname = os.path.basename(script)
        self._show_confirm(
            "确认脚本",
            f"将为以下录制脚本配置就近传送：\n\n显示名：{display}\n文件：{fname}\n\n继续？",
            on_yes=lambda: self._pick_rest_image(script),
        )

    def _pick_rest_image(self, script):
        # 用 Windows 原生文件对话框选图，同步阻塞，放到后台线程执行，
        # 完全不依赖 Flet FilePicker（规避其 session/service 挂起问题）。
        def _run():
            path = pick_image_file("选择休息点图标图片")
            if not path:
                return  # 用户取消
            self._apply_rest_image(script, path)
        threading.Thread(target=_run, daemon=True).start()

    # ---------------- 重命名 ----------------
    def on_rename_script(self, e):
        script = self.get_selected_script()
        if not script:
            self._snack("请先选择一个脚本", COLOR_WARN)
            return
        if not script.startswith(RECORD_DIR + os.sep):
            self._snack("重命名只针对录制脚本", COLOR_WARN)
            return
        fname = os.path.basename(script)
        custom = self.manager.load_script_names()
        old = custom.get(fname, fname)
        self._show_text_input(
            "重命名脚本",
            f"脚本文件：{fname}\n留空则恢复默认文件名",
            old,
            on_ok=lambda new_name: self._do_rename(fname, new_name),
        )

    def _do_rename(self, fname, new_name):
        new_name = (new_name or "").strip()
        if not new_name:
            ok, msg = self.manager.clear_script_name(fname)
        else:
            ok, msg = self.manager.save_script_name(fname, new_name)
        self.refresh_dynamic_scripts()
        self.set_status(msg, "ok" if ok else "err")

    # ---------------- 删除 ----------------
    def on_delete_script(self, e):
        script = self.get_selected_script()
        if not script:
            self._snack("请先选择一个脚本", COLOR_WARN)
            return
        if not script.startswith(RECORD_DIR + os.sep):
            self._snack("删除只针对录制脚本，静态不可删", COLOR_WARN)
            return
        fname = os.path.basename(script)
        base = os.path.splitext(fname)[0]
        tp_dir = _rel("tp_imgs")
        will_delete = [f"recordings/{fname}"]
        jp = _rel(os.path.join(RECORD_DIR, base + ".json"))
        if os.path.exists(jp):
            will_delete.append(f"recordings/{base}.json")
        for suf in ("_rest_point.png", "_point.png"):
            if os.path.exists(os.path.join(tp_dir, base + suf)):
                will_delete.append(f"tp_imgs/{base}{suf}")

        self._show_confirm(
            "确认删除",
            f"将删除脚本 {fname} 及以下文件：\n\n"
            + "\n".join("  · " + p for p in will_delete)
            + "\n\n同时清理显示名和传送点配置，不可撤销！",
            on_yes=lambda: self._do_delete(fname),
            yes_text="删除",
            danger=True,
        )

    def _do_delete(self, fname):
        ok, msg = self.manager.delete_recorded_script(fname)
        self.refresh_dynamic_scripts()
        self.set_status(msg, "ok" if ok else "err")
        self._snack(msg, COLOR_SUCCESS if ok else COLOR_ERROR)

    # ---------------- 配置传送点 ----------------
    def on_config_teleport(self, e):
        script = self.get_selected_script()
        if not script:
            self._snack("请先选择一个录制脚本", COLOR_WARN)
            return
        if not script.startswith(RECORD_DIR + os.sep):
            self._snack("配置传送点只针对录制脚本", COLOR_WARN)
            return
        self._load_teleport_points()
        if not self._regions:
            self._show_dialog("提示", "未找到地区列表，请检查 teleport.py 的 REGION_ENTRANCES", error=True)
            return

        fname = os.path.basename(script)
        current = self.manager.get_script_teleport(fname)
        current_info = self.manager.get_teleport_point_info(current) if current else None

        # 用对话框承载：名称 + 地区
        name_field = ft.TextField(
            label="传送点名称",
            value=current or "",
            hint_text="例如：淬火池、西侧通道",
            bgcolor=COLOR_SURFACE_2,
            border=ft.OutlineInputBorder(side=ft.BorderSide(width=1, color="#334155")),
        )
        default_region = (current_info or {}).get("region", "") or self._regions[0]
        region_dropdown = ft.Dropdown(
            label="所在地区",
            options=[ft.dropdown.Option(r) for r in self._regions],
            value=default_region if default_region in self._regions else self._regions[0],
            bgcolor=COLOR_SURFACE_2,
            border=ft.OutlineInputBorder(side=ft.BorderSide(width=1, color="#334155")),
        )

        def on_ok(_):
            name = (name_field.value or "").strip()
            if not name:
                self._snack("传送点名称不能为空", COLOR_WARN)
                return
            region = region_dropdown.value
            # 先关闭「配置传送点」模态对话框，再打开 Windows 原生文件选择器。
            # 原生对话框是独立顶层窗口，不占用 Flet 的 overlay，无争抢问题。
            self._close_dialog(dlg)
            self._pick_teleport_image(script, fname, name, region)

        dlg = self._build_dialog(
            "配置传送点",
            ft.Column(
                [
                    ft.Text(f"脚本：{self.get_selected_display()}（{fname}）", size=13, color=COLOR_TEXT_DIM),
                    name_field,
                    region_dropdown,
                ],
                tight=True,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("下一步", on_click=on_ok),
            ],
        )
        self.page.show_dialog(dlg)

    def _pick_teleport_image(self, script, fname, name, region):
        # 用 Windows 原生文件对话框选图。原生对话框是独立顶层窗口，不受 Flet
        # 模态对话框（配置传送点）关闭时序影响。同步阻塞 → 放后台线程，拿到路径后调 _apply_teleport_image。
        def _run():
            path = pick_image_file(f"选择「{name}」的传送点图标图片")
            if not path:
                return  # 用户取消
            self._apply_teleport_image(script, fname, name, region, path)
        threading.Thread(target=_run, daemon=True).start()

    def _apply_rest_image(self, script, path):
        # 注意：本方法在后台线程执行（由 _pick_rest_image 的独立线程调用），
        # 内部的 UI 更新都要用 _ui 包裹。
        base = os.path.splitext(os.path.basename(script))[0]
        img_name = base + "_rest_point.png"
        tp_dir = _rel("tp_imgs")
        os.makedirs(tp_dir, exist_ok=True)
        dest = os.path.join(tp_dir, img_name)
        try:
            shutil.copyfile(path, dest)
        except Exception as ex:
            self._ui(lambda: self._show_dialog("出错", f"复制图片失败：{ex}", error=True))
            return
        script_path = _rel(script)
        template_name = os.path.splitext(img_name)[0]
        ok, msg = self._enable_local_teleport(script_path, template_name)
        self._ui(lambda: self.set_status(msg, "ok" if ok else "err"))
        self._ui(lambda: self._show_dialog("完成" if ok else "出错", msg, error=not ok))

    def _enable_local_teleport(self, script_path, template_name):
        """启用就近传送：把录制脚本里被注释掉的就近传送逻辑解开，
        并把休息点模板图名替换为用户选择的新图。这里直接在 Flet 界面层实现，
        只做文本改写，不依赖任何 GUI 状态。
        """
        try:
            with open(script_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            return False, f"读取脚本失败：{e}"

        # 1. 替换休息点图模板名
        if 'LOCAL_REST_POINT_IMG = "xiuxidian_point"' in content:
            content = content.replace(
                'LOCAL_REST_POINT_IMG = "xiuxidian_point"',
                f'LOCAL_REST_POINT_IMG = "{template_name}"',
            )
        # 2. 取消就近传送三行注释
        content = content.replace(
            "    # local_teleport()\n", "    local_teleport()\n"
        )
        content = content.replace(
            "    # wait(1.0)   # 传送落地后的短暂缓冲\n",
            "    wait(1.0)   # 传送落地后的短暂缓冲\n",
        )
        content = content.replace(
            "    # print(f\"[{time.strftime('%H:%M:%S')}] 刷完一轮，就近传送回休息点……\")\n",
            "    print(f\"[{time.strftime('%H:%M:%S')}] 刷完一轮，就近传送回休息点……\")\n",
        )
        # 3. LOOP_ENABLED 置 True
        if "LOOP_ENABLED = False" in content:
            content = content.replace("LOOP_ENABLED = False", "LOOP_ENABLED = True")

        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            return False, f"写入脚本失败：{e}"
        MacroManager._clear_pyc(script_path)
        return True, f"已入库休息点图 {template_name}，并启用就近传送"

    def _apply_teleport_image(self, script, fname, name, region, path):
        # 注意：本方法在后台线程执行（由 _pick_teleport_image 的独立线程调用），
        # 内部的 UI 更新（_show_dialog/set_status/refresh_dynamic_scripts）都要
        # 用 _ui 包裹，避免跨线程直接操作 Flet 控件。

        # 图片入库，命名规则：时间戳 + _point.png
        base = os.path.splitext(fname)[0]
        img_name = base + "_point.png"
        tp_dir = _rel("tp_imgs")
        os.makedirs(tp_dir, exist_ok=True)
        dest = os.path.join(tp_dir, img_name)
        try:
            shutil.copyfile(path, dest)
        except Exception as ex:
            self._ui(lambda: self._show_dialog("出错", f"复制图片失败：{ex}", error=True))
            return
        point_img_name = os.path.splitext(img_name)[0]

        # 1. 往 teleport.py 的 TELEPORT_POINTS 新增/更新条目
        ok1, msg1 = self.manager.add_teleport_point(name, region, point_img_name)
        # 2. 绑定到当前录制脚本
        ok2, msg2 = self.manager.save_script_teleport(fname, name)

        if ok1 and ok2:
            self._ui(self._load_teleport_points)
            self._ui(self.refresh_dynamic_scripts)
            self._ui(lambda: self.set_status(f"已配置传送点「{name}」（地区：{region}）", "ok"))
            self._ui(lambda: self._show_dialog("完成", f"传送点「{name}」已配置并绑定到脚本。"))
        else:
            msg = "; ".join(m for m in (msg1, msg2) if m and not m.startswith("已"))
            self._ui(lambda: self.set_status(msg or "配置失败", "err"))
            self._ui(lambda: self._show_dialog("出错", (msg1 if not ok1 else msg2), error=True))

    # ---------------- 通用对话框 ----------------
    def _build_dialog(self, title, content, actions=None):
        return ft.AlertDialog(
            modal=True,
            title=ft.Text(title, size=16, weight=ft.FontWeight.BOLD, color=COLOR_TEXT),
            content=content,
            actions=actions or [ft.TextButton("确定", on_click=lambda e: None)],
            bgcolor=COLOR_SURFACE,
        )

    def _close_dialog(self, dlg):
        dlg.open = False
        self.page.update()

    def _show_dialog(self, title, msg, error=False):
        color = COLOR_ERROR if error else COLOR_TEXT
        dlg = self._build_dialog(
            title,
            ft.Text(msg, size=13, color=color, selectable=True),
            actions=[ft.TextButton("确定", on_click=lambda _: self._close_dialog(dlg))],
        )
        self.page.show_dialog(dlg)

    def _show_confirm(self, title, msg, on_yes, yes_text="确定", no_text="取消", danger=False):
        def yes(_):
            self._close_dialog(dlg)
            on_yes()

        def no(_):
            self._close_dialog(dlg)

        yes_btn = ft.FilledButton(yes_text, on_click=yes)
        if danger:
            yes_btn.style = ft.ButtonStyle(bgcolor=COLOR_ERROR, color="#450a0a")
        dlg = self._build_dialog(
            title,
            ft.Text(msg, size=13, color=COLOR_TEXT, selectable=True),
            actions=[ft.TextButton(no_text, on_click=no), yes_btn],
        )
        self.page.show_dialog(dlg)

    def _show_text_input(self, title, msg, initial, on_ok):
        field = ft.TextField(
            value=initial,
            autofocus=True,
            bgcolor=COLOR_SURFACE_2,
            border=ft.OutlineInputBorder(side=ft.BorderSide(width=1, color="#334155")),
        )

        def ok(_):
            self._close_dialog(dlg)
            on_ok(field.value)

        dlg = self._build_dialog(
            title,
            ft.Column(
                [
                    ft.Text(msg, size=13, color=COLOR_TEXT_DIM),
                    field,
                ],
                tight=True,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda _: self._close_dialog(dlg)),
                ft.FilledButton("确定", on_click=ok),
            ],
        )
        self.page.show_dialog(dlg)

    # ---------------- 轮询 ----------------
    def _start_polling(self):
        def poll():
            # 脚本运行状态
            try:
                now_running = self.manager.is_running()
            except Exception:
                now_running = False
            # 状态切换时给出明确提示 + 更新状态圆点颜色
            if self._last_running is True and now_running is False:
                self.set_status("已停止（可点击启动）", "info", refresh=False)
                # 恢复提示条为未运行引导文案
                self.run_hint.value = "启动后：F8 开始/暂停 · Esc 退出"
                self.run_hint.color = COLOR_TEXT_DIM
                # 额外弹一个明显的提示，确保无论用户在哪个页面都能看到「脚本已停止」
                self._snack("已停止", COLOR_SUCCESS)
                # 关键：脚本跑完后焦点仍停在游戏窗口（尤其全屏游戏），用户看不到
                # 也点不到 UI。这里把界面窗口重新切回前台。win32 前台切换调用
                # 必须放在后台线程（事件回调线程里 SetForegroundWindow 会失败）。
                threading.Thread(target=self._refocus_ui, daemon=True).start()
            elif self._last_running is False and now_running is True:
                # 运行中：提示按 F8 开始/暂停、Esc 退出（与启动时的文案保持一致）
                self.set_status("运行中（F8 开始/暂停 · Esc 退出）", "ok", refresh=False)
                self.run_hint.value = "运行中：按 F8 开始/暂停循环 · Esc 退出"
                self.run_hint.color = COLOR_PRIMARY
            self._last_running = now_running
            self.sync_buttons(refresh=False)

            # 日志、统计、录制器状态——每步独立隔离，任一步异常都不影响其它步骤
            try:
                self._refresh_log()
            except Exception:
                pass
            try:
                self._refresh_stats()
            except Exception:
                pass

            # 录制器状态
            try:
                now_rec = self.manager.is_recorder_running()
            except Exception:
                now_rec = False
            if self._last_rec is True and now_rec is False:
                self.btn_record.disabled = False
                self.btn_record_stop.disabled = True
                self.refresh_dynamic_scripts()
                self.set_status("录制器已退出", "info", refresh=False)
            elif now_rec:
                self.refresh_dynamic_scripts()
            self._last_rec = now_rec
            # 统一在这里 update 一次，避免多次 page.update() 竞争导致 diff 基准混乱
            self.page.update()

        # 轮询循环：直接在独立线程里调用 poll()。
        # 关键点：Flet 1.1.2 的 page.update() 内部用线程安全的 send_queue 发送消息，
        # 因此从任意线程修改控件属性 + page.update() 都是安全的，不需要再经过
        # page.run_thread（它依赖 session.connection.loop，session 未就绪/销毁时会抛
        # RuntimeError('An attempt to fetch destroyed session.')，反而导致轮询失效）。
        def loop():
            while True:
                time.sleep(0.5)
                try:
                    poll()
                except Exception as ex:
                    # 轮询异常绝不能静默退出——否则日志不再刷新、按钮状态不再同步。
                    # 落盘诊断并继续循环。
                    try:
                        with open(_rel("poll_error.txt"), "a", encoding="utf-8") as ef:
                            ef.write(f"[poll error] {ex!r}\n")
                    except Exception:
                        pass

        threading.Thread(target=loop, daemon=True).start()


# ==================================================================
# 入口
# ==================================================================
def main(page: ft.Page):
    LuminApp(page)


if __name__ == "__main__":
    # 非管理员自动提权（复用 macro_backend 的 relaunch_as_admin）
    if not is_admin() and os.environ.get("LUMIN_NO_ELEVATE") != "1":
        from macro_backend import relaunch_as_admin
        relaunch_as_admin()
        sys.exit(0)
    ft.run(main)
