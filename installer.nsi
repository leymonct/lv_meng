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
!define APP_VERSION "1.1.1"
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

!define MUI_ABORTWARNING
!define MUI_ICON "Lumin.ico"
!define MUI_UNICON "Lumin.ico"

!define MUI_WELCOMEPAGE_TITLE "欢迎安装 ${APP_NAME}"
!define MUI_WELCOMEPAGE_TEXT "本向导将引导你完成 ${APP_NAME} ${APP_VERSION} 的安装。$\r$\n$\r$\n本工具用于游戏《绿梦》的自动化操作（录制/回放/传送），需以管理员权限运行。"

!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_EXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即运行 ${APP_NAME}"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "创建桌面快捷方式"

; 页面顺序
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

; 卸载页面
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; ---- 安装区段 ----
Section "主程序" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"

  ; 安装主程序与运行时（递归打包 _internal、tp_imgs、recordings 等）
  File /r "dist_flet\Lumin\*"

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
