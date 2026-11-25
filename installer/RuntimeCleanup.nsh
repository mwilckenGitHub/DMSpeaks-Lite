; Maintenance runs outside Builds so Windows can release every owned runtime file.
!macro AcquireMaintenanceLock
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "Global\DMSpeaksLite-WOW-Setup") p .r8'
    StrCmp $8 0 lock_failed
    System::Call 'kernel32::WaitForSingleObject(p r8, i 0) i .r9'
    StrCmp $9 0 lock_acquired
    StrCmp $9 128 lock_acquired ; OS released the mutex after an interrupted wizard.
lock_failed:
    SetErrorLevel 1
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(MaintenanceBusy)" /SD IDOK
    Abort
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
