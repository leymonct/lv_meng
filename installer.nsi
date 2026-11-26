; ============================================================
; 绿梦小助手（Lumin Assistant）安装脚本
; 由 build_flet.py 打包后调用 makensis 生成安装程序
;
; 关键设计：
;   - 默认安装到 %LOCALAPPDATA%\Lumin（普通权限可写，因为
;     recordings/ 和 tp_imgs/ 运行时需要读写，不能装进 Program Files）
;   - 允许用户自选安装目录（DirPage）
;   - 创建桌面快捷方式 + 开始菜单快捷方式
;   - 生成标准卸载程序（uninstall.exe + 控制面板卸载项）
;   - 快捷方式运行 Lumin.exe（程序自身会请求管理员权限）
; ============================================================

; ---- 基础设置 ----
Unicode true
!define APP_NAME "绿梦小助手"
!define APP_NAME_EN "Lumin"
; 版本号：必须由 build_flet.py 通过 makensis 命令行 -DAPP_VERSION=xxx 传入，
; 与 runtime_env.py 的 APP_VERSION 保持一致（唯一权威来源）。
; 若未传（例如手动单独运行 makensis），此处用 !error 直接报错，避免静默用旧版本号打包。
!ifndef APP_VERSION
  !error "APP_VERSION 未定义：请通过 build_flet.py 打包（它会从 runtime_env.APP_VERSION 传入），不要单独运行 makensis。"
!endif
!define APP_PUBLISHER "Lumin"
!define APP_EXE "Lumin.exe"

!define PRODUCT_UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME_EN}"

Name "${APP_NAME}"
OutFile "dist_flet\Lumin-Setup.exe"   ; 输出到 dist_flet/ 下（相对项目根）
InstallDir "$LOCALAPPDATA\${APP_NAME_EN}"
InstallDirRegKey HKCU "${PRODUCT_UNINST_KEY}" "InstallLocation"
RequestExecutionLevel admin            ; 需要管理员权限（键鼠模拟必需）

; 用 zlib 压缩：lzma（尤其 solid 模式）在压缩 OpenBLAS 这类几十 MB 的 DLL 时
; 内存峰值过高，会被系统杀掉（构建中断）。zlib 快且稳，体积略大但可接受。
SetCompressor zlib
CRCCheck on
XPStyle on
ShowInstDetails show
ShowUninstDetails show

; ---- 界面 ----
!include "MUI2.nsh"
!include "WinMessages.nsh"

!define MUI_ABORTWARNING
!define MUI_ICON "Lumin.ico"
!define MUI_UNICON "Lumin.ico"

!define MUI_WELCOMEPAGE_TITLE "欢迎使用 ${APP_NAME} 安装向导"
; 欢迎页文案默认给个占位，实际内容由 WelcomeShowFunc（自定义 SHOW 函数）动态生成：
; 检测到旧版本时显示「升级」提示，全新安装时显示普通欢迎语。
!define MUI_WELCOMEPAGE_TEXT "本向导将引导你完成 ${APP_NAME} v${APP_VERSION} 的安装。"

!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_NAME}"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "创建桌面快捷方式"

; 页面顺序（欢迎页注册自定义 SHOW 函数，用于动态设置文案）
!define MUI_PAGE_CUSTOMFUNCTION_SHOW WelcomeShowFunc
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; 卸载页面
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; 已安装的旧版本号（空 = 未安装）。在 .onInit 里读取注册表设置，
; 供欢迎页文案和安装区段（区分「全新安装」还是「升级」）使用。
Var OLD_VERSION

; ---- 安装区段 ----
Section "主程序" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; 安装主程序与运行时。
  ; 升级策略：本安装程序对「旧版本升级」采用「卸载重装」——在 .onInit 里检测到
  ; 旧版本时，会先提示用户备份、自动清除旧数据（tp_imgs / recordings / _internal /
  ; exe），并把 $OLD_VERSION 清空，让安装走到这里时等价于「全新安装」。
  ; 因此本节无需再区分「升级/全新」，直接用 File /r 完整装入内置内容：
  ;   - 程序文件（_internal、exe、*.py、图标）
  ;   - 内置传送点配置（teleport.py + teleport_user.py）
  ;   - 内置模板图（tp_imgs/ 全部，含公共图 + 内置传送点图）
  ;   - 内置录制脚本（recordings/ 全部，含 .py/.json 与 names/teleports/plans 配置）
  ; ============================================================

  ; —— 程序文件 ——
  File /r "dist_flet\Lumin\_internal"
  File "dist_flet\Lumin\${APP_EXE}"
  File "dist_flet\Lumin\imgfind.py"
  File "dist_flet\Lumin\record_to_script.py"
  File "dist_flet\Lumin\recorder.py"
  File "dist_flet\Lumin\runtime_env.py"
  File "dist_flet\Lumin\macro_backend.py"
  File "dist_flet\Lumin\macro_ui_flet.py"
  File "dist_flet\Lumin\app_icon.png"
  File "dist_flet\Lumin\Lumin.ico"
  File "dist_flet\Lumin\teleport.py"
  File "dist_flet\Lumin\teleport_user.py"

  ; —— 内置模板图（tp_imgs/ 全部）——
  SetOutPath "$INSTDIR\tp_imgs"
  File /r "dist_flet\Lumin\tp_imgs\*"
  SetOutPath "$INSTDIR"

  ; —— 内置录制脚本（recordings/ 全部）——
  SetOutPath "$INSTDIR\recordings"
  File /r "dist_flet\Lumin\recordings\*"
  SetOutPath "$INSTDIR"

  ; 写入卸载信息（控制面板「程序和功能」里可卸载）
  WriteUninstaller "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "DisplayName" "${APP_NAME} ${APP_VERSION}"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "DisplayVersion" "${APP_VERSION}"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "Publisher" "${APP_PUBLISHER}"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${PRODUCT_UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINST_KEY}" "NoRepair" 1
  WriteRegDWORD HKCU "${PRODUCT_UNINST_KEY}" "EstimatedSize" 190000
SectionEnd

Section "桌面快捷方式" SecDesktop
  CreateShortcut "$DESKTOP\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
SectionEnd

Section "开始菜单快捷方式" SecStartMenu
  CreateDirectory "$SMPROGRAMS\${APP_NAME}"
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
  CreateShortcut "$SMPROGRAMS\${APP_NAME}\卸载.lnk" "$INSTDIR\uninstall.exe" "" "$INSTDIR\uninstall.exe" 0
SectionEnd

; 把安装时实际用到的桌面/开始菜单路径写入注册表，供卸载段读取，
; 避免「安装时 admin 上下文 vs 卸载时普通上下文」导致 $DESKTOP / $SMPROGRAMS
; 解析到不同目录、快捷方式删不掉的问题。
Function .onInstSuccess
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "DesktopShortcut" "$DESKTOP\${APP_NAME}.lnk"
  WriteRegStr HKCU "${PRODUCT_UNINST_KEY}" "StartMenuDir" "$SMPROGRAMS\${APP_NAME}"
FunctionEnd

; ============================================================
; 已安装版本检测
; ============================================================
; 在安装开始前检测注册表里是否已有旧版本：
;   - 有旧版本（版本号不同）→ 记录旧版本号，欢迎页显示「升级」提示，
;     并默认把安装目录定位到旧版本目录（InstallDirRegKey 已做，这里补读版本）
;   - 已装相同版本         → 提示「已安装此版本」，询问是否覆盖重装
;   - 未安装               → 正常全新安装
; ============================================================

Function .onInit
  ReadRegStr $OLD_VERSION HKCU "${PRODUCT_UNINST_KEY}" "DisplayVersion"
  ${If} $OLD_VERSION == ""
    ; 未安装，正常全新安装
    Return
  ${EndIf}

  ; 已装相同版本：询问是否覆盖重装（选「是」继续，选「否」中止）
  ${If} $OLD_VERSION == "${APP_VERSION}"
    MessageBox MB_YESNO|MB_ICONQUESTION \
      "检测到本机已安装 ${APP_NAME} v${APP_VERSION}（相同版本）。$\r$\n$\r$\n是否仍要重新安装/覆盖？" \
      IDYES continue_overwrite IDNO abort_install
    abort_install:
      Abort
    continue_overwrite:
      Return
  ${EndIf}

  ; 已装旧版本：升级采用「卸载重装」策略。
  ; 提示用户先导出脚本备份，确认后自动卸载旧版，再全新安装。
  MessageBox MB_OKCANCEL|MB_ICONEXCLAMATION \
    "检测到本机已安装旧版本 v$OLD_VERSION。$\r$\n$\r$\n本次升级将「卸载旧版并重新安装」，旧版数据（含你录制的脚本、配置的传送点）会被清除。$\r$\n$\r$\n请先导出你录制过的脚本：取消本次安装，打开旧版，用「导出脚本」功能把你自己录制的脚本备份出来，再回来升级。$\r$\n$\r$\n点击「确定」继续升级（确认已导出自己录制的脚本）。" \
    IDOK continue_upgrade IDCANCEL abort_upgrade
  abort_upgrade:
    Abort
  continue_upgrade:
    ; 关闭可能正在运行的旧版程序
    nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
    Sleep 500
    ; 清除旧版的用户数据目录（tp_imgs / recordings / _internal / exe），让全新安装
    ; 装入内置的最新内容。$INSTDIR 已由 InstallDirRegKey 定位到旧版安装目录。
    ; 程序文件（_internal/exe/*.py）其实会被全新安装的 File /r 覆盖，但这里也一并
    ; 清掉 tp_imgs/recordings，确保「重装」语义彻底、不会残留旧脚本/旧图。
    RMDir /r "$INSTDIR\tp_imgs"
    RMDir /r "$INSTDIR\recordings"
    RMDir /r "$INSTDIR\_internal"
    Delete "$INSTDIR\${APP_EXE}"
    ; 清空 OLD_VERSION，让后续安装走「全新安装」分支（完整装入内置数据）
    StrCpy $OLD_VERSION ""
FunctionEnd

; 欢迎页 SHOW 函数：页面显示时根据是否检测到旧版本，动态设置欢迎文案
; （此时 $mui.WelcomePage.Text 控件已创建，可直接 SendMessage 改文本）。
Function WelcomeShowFunc
  ${If} $OLD_VERSION == ""
    ; 全新安装
    SendMessage $mui.WelcomePage.Text ${WM_SETTEXT} 0 "STR:$\r$\n本向导将引导你完成 ${APP_NAME} v${APP_VERSION} 的安装。$\r$\n$\r$\n本工具用于游戏《绿梦》的自动化操作（录制/回放/传送），需以管理员权限运行。"
  ${Else}
    ; 升级安装（卸载重装）
    SendMessage $mui.WelcomePage.Text ${WM_SETTEXT} 0 "STR:$\r$\n检测到本机已安装旧版本 v$OLD_VERSION。$\r$\n$\r$\n本向导将把 ${APP_NAME} 升级到 v${APP_VERSION}。$\r$\n$\r$\n升级采用「卸载重装」：旧版数据（含你录制的脚本）会被清除，请先导出你录制过的脚本备份，再继续升级。"
  ${EndIf}
FunctionEnd

; ---- 卸载区段 ----
Section "Uninstall"
  ; 关闭可能正在运行的程序（用系统自带 taskkill，不依赖第三方插件）
  nsExec::ExecToStack 'tasklist /FI "IMAGENAME eq ${APP_EXE}" /NH'
  Pop $R0
  Pop $R1
  ${If} $R1 != ""
    MessageBox MB_YESNO|MB_ICONQUESTION "检测到 ${APP_NAME} 正在运行，是否先关闭再卸载？" IDYES +2 IDNO +1
    nsExec::Exec 'taskkill /F /IM ${APP_EXE}'
  ${EndIf}

  ; 删除快捷方式
  ; 优先读注册表里保存的「安装时实际路径」（见 .onInstSuccess），
  ; 这样即使卸载时上下文不同也能删对；读不到时再退回用常量兜底。
  ReadRegStr $R0 HKCU "${PRODUCT_UNINST_KEY}" "DesktopShortcut"
  ${If} $R0 != ""
    Delete "$R0"
  ${EndIf}
  Delete "$DESKTOP\${APP_NAME}.lnk"
  Delete "$COMMONDESKTOP\${APP_NAME}.lnk"

  ReadRegStr $R0 HKCU "${PRODUCT_UNINST_KEY}" "StartMenuDir"
  ${If} $R0 != ""
    Delete "$R0\${APP_NAME}.lnk"
    Delete "$R0\卸载.lnk"
    RMDir "$R0"
  ${EndIf}
  Delete "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk"
  Delete "$SMPROGRAMS\${APP_NAME}\卸载.lnk"
  RMDir "$SMPROGRAMS\${APP_NAME}"

  ; 删除程序文件
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APP_EXE}"
  Delete "$INSTDIR\imgfind.py"
  Delete "$INSTDIR\teleport.py"
  Delete "$INSTDIR\teleport_user.py"
  Delete "$INSTDIR\teleport_legacy_backup.py"
  Delete "$INSTDIR\recorder.py"
  Delete "$INSTDIR\record_to_script.py"
  Delete "$INSTDIR\runtime_env.py"
  Delete "$INSTDIR\macro_backend.py"
  Delete "$INSTDIR\macro_ui_flet.py"
  Delete "$INSTDIR\app_icon.png"
  Delete "$INSTDIR\Lumin.ico"
  Delete "$INSTDIR\使用说明.txt"
  Delete "$INSTDIR\lumin_error.log"

  ; tp_imgs / recordings 可能含用户自己录的脚本和截图，卸载时保留一份提示
  ; （用 RMDir /r 会连同用户数据一起删，这里选择删除，因为这是「卸载」的预期语义；
  ;   但日志类文件先删，避免残留）
  RMDir /r "$INSTDIR\tp_imgs"
  RMDir /r "$INSTDIR\recordings"

  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"

  ; 清除注册表卸载项
  DeleteRegKey HKCU "${PRODUCT_UNINST_KEY}"
SectionEnd
