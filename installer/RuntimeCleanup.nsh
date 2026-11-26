; Maintenance runs outside Builds so Windows can release every owned runtime file.
!define MAINTENANCE_WIZARD_PROPERTY "DMSpeaksLite.Wizard"

; Identify this version's main window even when NSIS changes its title.
!macro RegisterMaintenanceWindow
    System::Call 'user32::SetPropW(p $HWNDPARENT, w "${MAINTENANCE_WIZARD_PROPERTY}", p 1)'
!macroend

; Older releases lack the window property. NSIS can add a space or status to their title.
!macro MatchMaintenanceCaption caption found
    StrCmp $5 "${caption}" ${found}
    StrCmp $5 "${caption} " ${found}
    StrLen $7 "${caption}: "
    StrCpy $7 $5 $7
    StrCmp $7 "${caption}: " ${found}
!macroend

; Find the active wizard and restore it without dismissing its current page or prompt.
!macro ActivateMaintenanceWindow
    StrCpy $4 0
find_maintenance_window:
    FindWindow $4 "#32770" "" "" $4
    StrCmp $4 0 maintenance_window_done
    System::Call 'user32::IsWindowVisible(p r4) i .r5'
    StrCmp $5 0 find_maintenance_window
    System::Call 'user32::GetPropW(p r4, w "${MAINTENANCE_WIZARD_PROPERTY}") p .r5'
    StrCmp $5 0 0 maintenance_window_found
    GetDlgItem $5 $4 3 ; The NSIS Back button distinguishes the wizard from message boxes.
    StrCmp $5 0 find_maintenance_window
    System::Call 'user32::GetWindowTextW(p r4, w .r5, i ${NSIS_MAX_STRLEN})'
    !insertmacro MatchMaintenanceCaption "${EDITION} Setup" maintenance_window_found
    !insertmacro MatchMaintenanceCaption "$(MaintenanceCaption)" maintenance_window_found
    Goto find_maintenance_window
maintenance_window_found:
    System::Call 'user32::IsIconic(p r4) i .r5'
    StrCmp $5 0 +2
    System::Call 'user32::ShowWindowAsync(p r4, i ${SW_RESTORE})'
    System::Call 'user32::GetLastActivePopup(p r4) p .r5'
    System::Call 'user32::SetForegroundWindow(p r5)'
maintenance_window_done:
!macroend

; Keep one Setup or maintenance wizard in charge of the shared install for its lifetime.
!macro AcquireMaintenanceLock
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Global\DMSpeaksLite-WOW-Setup") p .r8'
    StrCmp $8 0 lock_failed
    System::Call 'kernel32::WaitForSingleObject(p r8, i 0) i .r9'
    StrCpy $6 20
check_maintenance_lock:
    StrCmp $9 0 lock_acquired
    StrCmp $9 128 lock_acquired ; Windows grants ownership after an interrupted wizard.
    StrCmp $9 258 0 lock_close_failed ; Only WAIT_TIMEOUT indicates a competing operation.
    IfSilent lock_busy
    !insertmacro ActivateMaintenanceWindow
    StrCmp $4 0 0 lock_busy
    ; A second launch can arrive before the first wizard creates its window.
    IntOp $6 $6 - 1
    StrCmp $6 0 lock_busy
    System::Call 'kernel32::WaitForSingleObject(p r8, i 250) i .r9'
    Goto check_maintenance_lock
lock_busy:
    System::Call 'kernel32::CloseHandle(p r8)'
    SetErrorLevel 1618 ; Windows' code for another installation already in progress.
    Quit
lock_close_failed:
    System::Call 'kernel32::CloseHandle(p r8)'
lock_failed:
    SetErrorLevel 1
    Quit
lock_acquired:
!macroend

!macro StageCleanupTools
    InitPluginsDir
    SetOutPath "$PLUGINSDIR\cleanup"
    ClearErrors
    File /oname=prune_runtimes.py "${RUNTIME_PRUNER}"
    File /oname=runtime-files.json.gz "${RUNTIME_OWNERS}"
    File /oname=shell_maintenance.py "${__FILEDIR__}/shell_maintenance.py"
    File /oname=maintenance-core.zip "${__FILEDIR__}/maintenance-core.zip"
    File /oname=python313._pth "${__FILEDIR__}/maintenance-python._pth"
    !include "${MAINTENANCE_PYTHON_FILES}"
!macroend

!macro RunRuntimeCleanup mode build game
    Delete "$PLUGINSDIR\RuntimeCleanup.ini"
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\prune_runtimes.py" --mode ${mode} --builds "$LOCALAPPDATA\DMSpeaksLite\Builds" --build "${build}" --game "${game}\." --result "$PLUGINSDIR\RuntimeCleanup.ini"` $0
!macroend

!macro RestoreShellFile name
    ${IfNot} ${FileExists} "$INSTDIR\${name}"
        CopyFiles /SILENT /FILESONLY "$PLUGINSDIR\${name}" "$INSTDIR"
        IfErrors cleanup_failed
    ${EndIf}
!macroend
