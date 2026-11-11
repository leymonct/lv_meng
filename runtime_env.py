# -*- coding: utf-8 -*-
"""
运行环境适配层 —— 让同一份代码既能「源码运行」也能「打包成 exe 运行」
================================================================
两种运行形态的区别只有三处，全部集中在本文件里处理：

    1. 根目录怎么算
       源码： 本文件所在目录
       打包： exe 所在目录（PyInstaller 的 __file__ 指向临时解压目录 _MEIPASS，
              绝不能用来定位 tp_imgs / recordings，否则会出现「资源找不到、
              录的新脚本第二天消失」这类灵异问题）

    2. 怎么启动子脚本（宏脚本 / 传送脚本 / 录制器）
       源码：  [python.exe, 脚本路径, 参数...]
       打包：  [Lumin.exe, --run-py, 脚本路径, 参数...]
       因为打包后 sys.executable 就是 Lumin.exe 本身，照旧拼命令会不断弹出新界面。

    3. 窗口模式下的标准输出
       exe 以 --noconsole 构建时，Python 的 sys.stdout / sys.stderr 是 None。
       而 UI 已经把子进程的 stdout 重定向到 macro_log.txt，
       这里把原始文件描述符接回来，日志才能正常落盘。
"""

import os
import sys

# 是否运行在 PyInstaller 打包出的 exe 里
IS_FROZEN = bool(getattr(sys, "frozen", False))

# 「脚本运行器」模式的命令行标记：Lumin.exe --run-py xxx.py
RUN_PY_FLAG = "--run-py"


def base_dir():
    """程序根目录（tp_imgs / recordings / 日志文件都以它为基准）"""
    if IS_FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_stdio():
    """把 sys.stdout / sys.stderr 统一成 UTF-8，并接回真实的文件描述符。

    打包后有两条坑，缺一不可：

    1. sys.stdout 可能是 None（--noconsole 构建且没有可用控制台）
       → 用 fd 1/2 重新包装，否则 print 全部丢失。

    2. sys.stdout 存在但按系统默认编码（中文 Windows 是 GBK）打开
       → 子进程日志写进 macro_log.txt 是 GBK 字节，而 UI 按 UTF-8 读，
         中文全变乱码；严重时 print 到无法编码的字符（如 ✅）直接抛
         UnicodeEncodeError 把脚本打挂。
       所以这里统一 reconfigure 成 UTF-8，并用 errors="replace" 兜底。

    返回是否真的改动过（已正确配置时返回 False）。
    """
    fixed = False
    for name, fd in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                stream = open(fd, "w", encoding="utf-8", errors="replace",
                              buffering=1, closefd=False)
                setattr(sys, name, stream)
                fixed = True
            except Exception:
                pass
            continue

        # 已经是 UTF-8 就不动它（比如源码运行、或脚本自己 reconfigure 过）
        if str(getattr(stream, "encoding", "") or "").lower().replace("-", "") == "utf8":
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            fixed = True
        except Exception:
            pass
    return fixed


def child_cmd(script_path, *args):
    """构造启动子脚本的命令行（列表形式，直接给 subprocess.Popen）。"""
    if IS_FROZEN:
        return [sys.executable, RUN_PY_FLAG, script_path] + list(args)
    return [sys.executable, script_path] + list(args)


def elevation_params():
    """提权重启时 ShellExecuteW 需要的参数串。

    源码模式必须带上脚本路径，否则 python.exe 会进交互解释器；
    打包模式不能再带参数，否则 exe 会把它当作命令行开关。
    """
    if IS_FROZEN:
        return ""
    return f'"{os.path.abspath(sys.argv[0])}"'


def run_script(script_path, args=()):
    """在当前进程里以 __main__ 身份执行一个 .py 脚本（供 --run-py 模式调用）。

    等价于命令行 `python script_path args...`：
        - sys.argv 被改写为脚本视角的参数
        - 根目录加入 sys.path（脚本里的 import imgfind 等才能生效）
        - 工作目录切到根目录（与 UI 启动子进程时的 cwd 一致）
    """
    import runpy

    runtime_root = base_dir()
    script_path = os.path.abspath(script_path)

    sys.argv = [script_path] + list(args)
    if runtime_root not in sys.path:
        sys.path.insert(0, runtime_root)
    try:
        os.chdir(runtime_root)
    except Exception:
        pass

    try:
        runpy.run_path(script_path, run_name="__main__")
    except SystemExit as e:          # 脚本里的 sys.exit() / exit()
        code = e.code
        return 0 if code is None else (code if isinstance(code, int) else 0)
    return 0
