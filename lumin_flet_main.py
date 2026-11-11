# -*- coding: utf-8 -*-
"""
Lumin 程序入口（Flet 版）—— 源码运行与 exe 打包共用
================================================================
三种启动身份：

    Lumin.exe                     打开 Flet 版宏管理器界面（默认）
    Lumin.exe --run-py x.py [参数] 作为「脚本运行器」执行 x.py
    Lumin.exe --selftest          自检：不打开界面，检查运行环境后退出

说明：
    本文件是 Flet 版界面（macro_ui_flet.py）的打包入口。
    默认身份启动 ft.run(main)（Flet UI）。

    --run-py / --selftest 两个身份复用 runtime_env：
    打包后 sys.executable 指向 exe 本身，MacroManager 起子进程时会用
    runtime_env.child_cmd() 拼出 [Lumin.exe, --run-py, 脚本]，由这里接管执行，
    避免每次跑宏都弹出一个新界面。
"""

import os
import sys
import traceback

import runtime_env


def run_script_mode(args):
    """--run-py 模式：args = [脚本路径, 脚本参数...]"""
    runtime_env.ensure_stdio()

    if not args:
        sys.stderr.write("[Lumin] --run-py 缺少脚本路径\n")
        return 2

    script = args[0]
    if not os.path.isabs(script):
        script = os.path.join(runtime_env.base_dir(), script)
    if not os.path.exists(script):
        sys.stderr.write(f"[Lumin] 脚本不存在：{script}\n")
        return 2
    return runtime_env.run_script(script, args[1:])


def selftest():
    """自检：验证打包后的资源路径、依赖、Flet UI 是否都正常，不弹界面。"""
    runtime_env.ensure_stdio()
    ok = True
    print("=" * 60)
    print("Lumin（Flet 版）自检")
    print("=" * 60)

    def check(label, fn):
        nonlocal ok
        try:
            detail = fn()
            print(f"  [OK]   {label}" + (f"：{detail}" if detail else ""))
        except Exception as e:
            ok = False
            print(f"  [FAIL] {label}：{type(e).__name__}: {e}")

    root = runtime_env.base_dir()
    print(f"  运行模式：{'打包 exe' if runtime_env.IS_FROZEN else '源码'}")
    print(f"  根目录  ：{root}")
    print(f"  解释器  ：{sys.executable}")
    print("-" * 60)

    def _imports():
        import cv2, numpy, mss, win32gui  # noqa: F401
        import flet  # noqa: F401
        flet_ver = getattr(flet, "__version__", "?")
        return f"cv2 {cv2.__version__} / numpy {numpy.__version__} / flet {flet_ver}"

    def _dirs():
        missing = [d for d in ("tp_imgs", "recordings")
                   if not os.path.isdir(os.path.join(root, d))]
        if missing:
            raise FileNotFoundError("缺少目录 " + "、".join(missing))
        n_img = len([f for f in os.listdir(os.path.join(root, "tp_imgs"))
                     if f.lower().endswith(".png")])
        n_py = len([f for f in os.listdir(os.path.join(root, "recordings"))
                    if f.endswith(".py")])
        return f"模板图 {n_img} 张 / 宏脚本 {n_py} 个"

    def _scripts():
        from macro_backend import MacroManager
        items = MacroManager().scan_recorded_scripts()
        if not items:
            raise RuntimeError("recordings/ 里扫不到宏脚本")
        return f"识别 {len(items)} 个，例如「{items[0][0]}」"

    def _backend():
        # 验证 Flet 版复用的后端能正常实例化（不弹界面）
        from macro_backend import MacroManager
        m = MacroManager()
        return f"MacroManager 实例化成功"

    def _child_cmd():
        cmd = runtime_env.child_cmd(os.path.join(root, "recordings", "demo.py"), "arg1")
        return " ".join(os.path.basename(c) if i == 0 else c
                        for i, c in enumerate(cmd))

    check("依赖导入", _imports)
    check("资源目录", _dirs)
    check("宏脚本扫描", _scripts)
    check("后端实例化", _backend)
    check("子进程命令", _child_cmd)

    print("-" * 60)
    print("结论：" + ("全部通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


def main():
    runtime_env.ensure_stdio()

    if len(sys.argv) > 1 and sys.argv[1] == runtime_env.RUN_PY_FLAG:
        return run_script_mode(sys.argv[2:])
    if "--selftest" in sys.argv:
        return selftest()

    # 非管理员自动提权（复用 macro_backend 的 relaunch_as_admin）。
    # 键鼠模拟必须管理员权限，否则游戏收不到按键。LUMIN_NO_ELEVATE 用于
    # 自检等场景跳过提权（避免 --selftest 时反复弹 UAC）。
    from macro_backend import is_admin
    if not is_admin() and os.environ.get("LUMIN_NO_ELEVATE") != "1":
        from macro_backend import relaunch_as_admin
        relaunch_as_admin()
        return 0

    # 默认：启动 Flet 版界面
    import flet as ft
    from macro_ui_flet import main as flet_main
    ft.run(flet_main)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        runtime_env.ensure_stdio()
        traceback.print_exc()
        try:
            with open(os.path.join(runtime_env.base_dir(), "lumin_error.log"),
                      "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
        sys.exit(1)
