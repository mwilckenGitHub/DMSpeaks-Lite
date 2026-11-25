Target amd64-unicode
Name "$(RecoveryTitle)"
Caption "$(RecoveryTitle)"
OutFile "${OUTPUT}"
Icon "${ICON}"
RequestExecutionLevel user
CRCCheck force
XPStyle on
ShowInstDetails hide
InstallColors /windows
AutoCloseWindow true
Page instfiles

!define DMSPEAKS_RECOVERY
!include "${__FILEDIR__}/DMSpeaksLanguages.nsh"
!define ENTRY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DMSpeaksLite"

Section
    SetRegView 64
    ReadRegStr $0 HKCU "${ENTRY}" "GameFolder"
    StrCmp $0 "" missing
    ReadRegStr $3 HKCU "${ENTRY}" "CleanupRevision"
    StrCpy $2 "$LOCALAPPDATA\DMSpeaksLite\Maintenance\DMSpeaks-Maintenance.exe"
    StrCmp $3 "${SETUP_ID}" use_embedded
    ; A different registered version must use its own maintenance rules.
    IfFileExists "$2" 0 missing
    IfSilent done
    ClearErrors
    ExecShell "open" "$2" "/UNINSTALL"
    IfErrors failed done
use_embedded:
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    DetailPrint "$(RecoveryPreparing)"
    File /oname=maintenance.exe "${SHIPPING_MAINTENANCE}"
    IfErrors failed
    ; A silent recovery only releases the listener. Personal data removal requires the wizard.
    IfSilent check_listener
    ExecWait '"$PLUGINSDIR\maintenance.exe" /UNINSTALL _?=$LOCALAPPDATA\DMSpeaksLite\Maintenance' $3
    Goto result
check_listener:
    ExecWait '"$PLUGINSDIR\maintenance.exe" /S /RECOVERYCHECK _?=$LOCALAPPDATA\DMSpeaksLite\Maintenance' $3
result:
    IfErrors failed
    SetErrorLevel $3
    Goto done
missing:
    SetErrorLevel 1
    Abort "$(RecoveryMissing)"
failed:
    SetErrorLevel 1
    Abort "$(RecoveryOpenApps)"
done:
SectionEnd
