Target amd64-unicode
!include "MUI2.nsh"
!include "nsDialogs.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!define EDITION "DMSpeaks Lite - WOW"
Name "${EDITION}"
OutFile "${OUTPUT}"
!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_CUSTOMFUNCTION_GUIINIT RegisterSetupWizard
!define MUI_CUSTOMFUNCTION_UNGUIINIT un.RegisterMaintenanceWizard
RequestExecutionLevel user
XPStyle on
CRCCheck force
SetCompressor zlib
ShowInstDetails show
ShowUninstDetails hide
InstallColors /windows
UninstallCaption "$(MaintenanceCaption)"

!define APP_ENTRY "Software\Microsoft\Windows\CurrentVersion\Uninstall\DMSpeaksLite"
!define LITE_FOLDER "$LOCALAPPDATA\DMSpeaksLite"
!define LITE_LAUNCHER "${LITE_FOLDER}\${EDITION}.lnk"
!define LITE_ICON "${LITE_FOLDER}\Maintenance\DMSpeaks-Maintenance.exe"
!define OPEN_LITE "${LITE_FOLDER}\Maintenance\open_lite.py"
!define START_LINK "$SMPROGRAMS\${EDITION}.lnk"
!define DESKTOP_LINK "$DESKTOP\${EDITION}.lnk"

SpaceTexts none
Var PhaseText
Var OverallPhase
Var OverallPercent
Var PhaseBar
Var MaintenanceTitle
Var MaintenanceDescription
Var PhaseCancel
Var NativeCancel
Var HelperOutput

!define MUI_PAGE_HEADER_TEXT "$(EulaTitle)"
!define MUI_PAGE_HEADER_SUBTEXT "$(EulaSubtitle)"
!define MUI_LICENSEPAGE_TEXT_TOP "$(EulaDescription)"
!define MUI_LICENSEPAGE_TEXT_BOTTOM " "
!define MUI_LICENSEPAGE_CHECKBOX
!define MUI_LICENSEPAGE_CHECKBOX_TEXT "$(EulaAcceptance)"
!define MUI_LICENSEPAGE_BUTTON "$(^NextBtn)"
!insertmacro MUI_PAGE_LICENSE "${END_USER_AGREEMENT}"

!define MUI_PAGE_HEADER_TEXT "$(NoticesTitle)"
!define MUI_PAGE_HEADER_SUBTEXT "$(NoticesSubtitle)"
!define MUI_LICENSEPAGE_TEXT_TOP "$(NoticesDescription)"
!define MUI_LICENSEPAGE_TEXT_BOTTOM " "
!define MUI_LICENSEPAGE_BUTTON "$(^NextBtn)"
!insertmacro MUI_PAGE_LICENSE "${LICENSE_NOTICES}"

!define MUI_DIRECTORYPAGE_TEXT_TOP "$(ChooseGameFolderDescription)"
!define MUI_DIRECTORYPAGE_TEXT_DESTINATION "$(GameFolderLabel)"
!define MUI_PAGE_HEADER_TEXT "$(ChooseGameFolderTitle)"
!define MUI_PAGE_HEADER_SUBTEXT "$(ChooseGameFolderSubtitle)"
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipKnownFolder
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE RequireSpoken
!insertmacro MUI_PAGE_DIRECTORY
!ifdef EXTERNAL_MEDIA
Page custom VoicePackPage
!endif
Page custom ShortcutPage ReadShortcutChoices
!define MUI_PAGE_HEADER_TEXT "$(InstallingTitle)"
!define MUI_PAGE_HEADER_SUBTEXT "$(InstallingSubtitle)"
!define MUI_PAGE_CUSTOMFUNCTION_SHOW CreatePhaseBar
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "$(InstallComplete)"
!define MUI_FINISHPAGE_TEXT "$(InstallCompleteDetails)"
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_FUNCTION OpenLite
!define MUI_FINISHPAGE_RUN_TEXT "$(RunLite)"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT
!insertmacro MUI_PAGE_FINISH
UninstPage custom un.MaintenancePage un.ReadMaintenanceChoice
!define MUI_PAGE_HEADER_TEXT "$MaintenanceTitle"
!define MUI_PAGE_HEADER_SUBTEXT "$MaintenanceDescription"
!define MUI_PAGE_CUSTOMFUNCTION_SHOW un.CreatePhaseBar
!insertmacro MUI_UNPAGE_INSTFILES
!define MUI_FINISHPAGE_TITLE "$MaintenanceTitle"
!define MUI_FINISHPAGE_TEXT "$MaintenanceDescription"
!define MUI_FINISHPAGE_BUTTON "$(Close)"
!define MUI_FINISHPAGE_NOREBOOTSUPPORT
!insertmacro MUI_UNPAGE_FINISH
!include "${__FILEDIR__}/DMSpeaksLanguages.nsh"

Var DetectedWowFolder
Var PlacementOption
Var GameFolder
Var PreviousGameFolder
Var ArchivePath
!ifdef EXTERNAL_MEDIA
Var VoicePackMask

!macro StageReleaseMedia game wow options result
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    ClearErrors
    File /oname=lite-media.json "${EXTERNAL_MEDIA}"
    File /oname=stage_release_media.py "${STAGE_RELEASE_MEDIA}"
    File /oname=setup_media_progress.py "${STAGE_MEDIA_PROGRESS}"
    ${If} ${Errors}
        StrCpy ${result} 1
    ${Else}
        !insertmacro ShowPhase "$(CheckingVoices)"
        nsExec::ExecToLog '"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\setup_media_progress.py" "$PhaseBar" "${RELEASE_MEDIA_PROGRESS}" "$PhaseCancel" "${game}" "${wow}\." "$LOCALAPPDATA\DMSpeaksLiteBeta\release-downloads\${SETUP_ID}" "$PLUGINSDIR\." --pack-mask=$VoicePackMask ${options}'
        Pop ${result}
        ${If} ${result} == "0"
            ; Restore the usual percent range before the other helpers take over.
            SendMessage $PhaseBar ${PBM_SETRANGE32} 0 100
            SendMessage $PhaseBar ${PBM_SETPOS} ${RELEASE_MEDIA_PROGRESS} 0
            ReadINIStr $ArchivePath "$PLUGINSDIR\lite-media.ini" media game
            ${If} $ArchivePath == ""
                StrCpy ${result} 1
            ${EndIf}
        ${EndIf}
    ${EndIf}
!macroend

!macro ExternalPrefix package result
    !insertmacro ShowPhase "$(PreparingSpeechData)"
    SetOutPath "$PLUGINSDIR"
    ClearErrors
    File /oname=install_prefix_media.py "${PREFIX_INSTALL_SCRIPT}"
    ${If} ${Errors}
        StrCpy ${result} 1
    ${Else}
        nsExec::ExecToLog '"${package}\runtime\python\python.exe" -I -B "$PLUGINSDIR\install_prefix_media.py" "${package}\." "$PLUGINSDIR\prefix-data.zip" "${PREFIX_MANIFEST_HASH}" "${PREFIX_ARCHIVE_HASH}"'
        Pop ${result}
    ${EndIf}
!macroend
!endif
Var ActiveBuild
Var OtherClients
Var SharedOption
Var PeerGame
Var PeerBuild
Var GamePython
!include "${__FILEDIR__}/RuntimeCleanup.nsh"
Var RepairChoice
Var RemoveNamesControl
Var RemoveNameData
Var ChoiceHelp
Var RemoveChoice
Var ShortcutChoice
Var ShortcutsConfigured
Var StartShortcut
Var DesktopShortcut
Var StartControl
Var DesktopControl

!macro LoadChoice key value default link legacy_link
    ClearErrors
    ReadRegDWORD ${value} HKCU "${APP_ENTRY}" "${key}"
    ${If} ${Errors}
        StrCpy $ShortcutsConfigured 0
        StrCpy ${value} "${default}"
        ${If} $GameFolder != ""
            StrCpy ${value} 0
            ${If} ${FileExists} "${link}"
            ${OrIf} ${FileExists} "${legacy_link}"
                StrCpy ${value} 1
            ${EndIf}
        ${EndIf}
    ${EndIf}
!macroend

!macro LoadShortcutChoices
    StrCpy $ShortcutsConfigured 1
    !insertmacro LoadChoice "StartShortcut" $StartShortcut 1 "${START_LINK}" "$SMPROGRAMS\DMSpeaks Lite.lnk"
    !insertmacro LoadChoice "DesktopShortcut" $DesktopShortcut 0 "${DESKTOP_LINK}" "$DESKTOP\DMSpeaks Lite.lnk"
    ClearErrors
!macroend

!macro AddShortcutControls start_y desktop_y
    ${NSD_CreateCheckbox} 12u ${start_y} 92% 15u "$(StartShortcut)"
    Pop $StartControl
    ${NSD_CreateCheckbox} 12u ${desktop_y} 92% 15u "$(DesktopShortcut)"
    Pop $DesktopControl
    ${NSD_SetState} $StartControl $StartShortcut
    ${NSD_SetState} $DesktopControl $DesktopShortcut
!macroend

!macro ApplyShortcut link choice
    ${If} ${choice} == ${BST_CHECKED}
        !insertmacro CreateLiteShortcut "${link}"
    ${ElseIf} ${FileExists} "${link}"
        Delete "${link}"
    ${EndIf}
!macroend

!macro CreateLiteShortcut link
    CreateShortCut "${link}" "$1\runtime\python\pythonw.exe" '-I -B "${OPEN_LITE}" "$GameFolder\."' "${LITE_ICON}" 0
    ${IfNot} ${Errors}
        System::Call 'shell32::SHChangeNotify(i 0x2000, i 5, w "${link}", p 0)' ; SHCNE_UPDATEITEM, SHCNF_PATHW
    ${EndIf}
!macroend

!macro SaveShortcutChoices
    WriteRegDWORD HKCU "${APP_ENTRY}" "StartShortcut" $StartShortcut
    WriteRegDWORD HKCU "${APP_ENTRY}" "DesktopShortcut" $DesktopShortcut
!macroend

Function ShortcutPage
    ${If} $ShortcutsConfigured == 1
    ${AndIf} $GameFolder == $INSTDIR
        Abort
    ${EndIf}
    !insertmacro MUI_HEADER_TEXT "$(ShortcutTitle)" "$(ShortcutSubtitle)"
    nsDialogs::Create 1018
    Pop $0
    StrCmp $0 error skip
    ${NSD_CreateLabel} 0 3u 100% 16u "$(ShortcutLocations)"
    Pop $0
    !insertmacro AddShortcutControls 28u 49u
    ${NSD_CreateLabel} 0 78u 100% 28u "$(ChangeShortcutsLater)"
    Pop $0
    nsDialogs::Show
    Return
skip:
    Abort
FunctionEnd

Function ReadShortcutChoices
    ${NSD_GetState} $StartControl $StartShortcut
    ${NSD_GetState} $DesktopControl $DesktopShortcut
FunctionEnd

!ifdef EXTERNAL_MEDIA
!include "${__FILEDIR__}/VoicePackPage.nsh"
!endif

Function OpenLite
    ExecShell "open" "${LITE_LAUNCHER}"
FunctionEnd

; The built-in bar counts installer instructions. Give the file helper its own
; bar so NSIS cannot move it while a single long-running instruction is active.
!macro PhaseControl original class style id handle
    StrCpy $0 ${original}
    System::Call 'user32::GetWindowRect(p r0, p r2)'
    System::Call 'user32::MapWindowPoints(p 0, p r1, p r2, i 2)'
    System::Call '*$2(i .r3, i .r4, i .r5, i .r6)'
    IntOp $5 $5 - $3
    IntOp $6 $6 - $4
    System::Call 'user32::CreateWindowExW(i 0, w "${class}", w "", i ${style}, i r3, i r4, i r5, i r6, p r1, p ${id}, p 0, p 0) p .r7'
    StrCpy ${handle} $7
    ${If} $7 != 0
        SendMessage $0 ${WM_GETFONT} 0 0 $8
        SendMessage $7 ${WM_SETFONT} $8 1
        ShowWindow $0 ${SW_HIDE}
    ${EndIf}
!macroend

!macro DeclarePhaseBar prefix
Function ${prefix}CreatePhaseBar
    System::Store S
    StrCpy $1 $mui.InstFilesPage
    System::Alloc 16
    Pop $2
    !insertmacro PhaseControl $mui.InstFilesPage.Text "Static" 0x50000080 1906 $PhaseText
    !insertmacro PhaseControl $mui.InstFilesPage.ProgressBar "msctls_progress32" 0x50000000 1904 $PhaseBar
    System::Free $2
    SendMessage $PhaseBar ${PBM_SETRANGE32} 0 100
    !if "${prefix}" == "un."
        ${If} $PlacementOption == "repair"
            Call un.CreatePhaseCancel
        ${EndIf}
    !else
        !ifdef EXTERNAL_MEDIA
            Call CreatePhaseCancel
        !endif
    !endif
    System::Store L
FunctionEnd
!macroend
!insertmacro DeclarePhaseBar ""
!insertmacro DeclarePhaseBar "un."

; Use a private button while cancellation is safe. The worker reads its checked state.
!macro DeclarePhaseCancel prefix
Function ${prefix}CreatePhaseCancel
    GetDlgItem $NativeCancel $HWNDPARENT 2
    ${If} $NativeCancel != 0
        StrCpy $1 $HWNDPARENT
        System::Alloc 16
        Pop $2
        ; A push-like auto-radio button latches a request even if clicked twice.
        !insertmacro PhaseControl $NativeCancel "Button" 0x50031009 1908 $PhaseCancel
        System::Free $2
        ${If} $PhaseCancel != 0
            EnableWindow $NativeCancel 0
            SendMessage $PhaseCancel ${WM_SETTEXT} 0 "STR:$(RepairCancelButton)"
        ${EndIf}
    ${EndIf}
FunctionEnd

Function ${prefix}RestorePhaseCancel
    ${If} $PhaseCancel != 0
        EnableWindow $PhaseCancel 0
        ShowWindow $PhaseCancel ${SW_HIDE}
        ShowWindow $NativeCancel ${SW_SHOWNA}
    ${EndIf}
FunctionEnd
!macroend
!ifdef EXTERNAL_MEDIA
    !insertmacro DeclarePhaseCancel ""
!endif
!insertmacro DeclarePhaseCancel "un."

!macro ShowOverall percent phase
    SendMessage $PhaseBar ${PBM_SETPOS} ${percent} 0
    StrCpy $OverallPhase "${phase}"
    StrCpy $OverallPercent "${percent}"
    SendMessage $PhaseText ${WM_SETTEXT} 0 "STR:$(OverallProgress)"
!macroend

!macro QuietDetails
    ; NSIS 3.09 restores the previous mode when initializing a plug-in.
    ; Make both the current and previous modes quiet between activity rows.
    SetDetailsPrint none
    SetDetailsPrint none
!macroend

!macro DetailLine message
    SetDetailsPrint listonly
    DetailPrint "${message}"
    !insertmacro QuietDetails
!macroend

!macro RunSupport command result
    nsExec::ExecToStack '${command}'
    Pop ${result}
    Pop $HelperOutput
    ${If} ${result} != "0"
    ${AndIf} $HelperOutput != ""
        !insertmacro DetailLine "$HelperOutput"
    ${EndIf}
!macroend

!macro ShowPhase message
    ${If} $PhaseText != 0
        SendMessage $PhaseText ${WM_SETTEXT} 0 "STR:${message}"
    ${EndIf}
    !insertmacro DetailLine "${message}"
!macroend

!macro FailPhase message
    MessageBox MB_OK|MB_ICONEXCLAMATION "${message}" /SD IDOK
    !insertmacro ShowPhase "$(SetupFailed)"
    Abort "${message}"
!macroend

; Publish the Setup window so a duplicate launch can restore it.
Function RegisterSetupWizard
    !insertmacro RegisterMaintenanceWindow
FunctionEnd

Function .onInit
    IfSilent 0 acceptance_checked
    ${GetParameters} $0
    ClearErrors
    ${GetOptions} $0 "/ACCEPTEULA" $1
    IfErrors acceptance_missing
    StrCmp $1 "" acceptance_checked
acceptance_missing:
    SetErrorLevel 6
    Quit
acceptance_checked:
    !insertmacro AcquireMaintenanceLock
    SetRegView 64
    ReadRegStr $GameFolder HKCU "${APP_ENTRY}" "GameFolder"
    StrCpy $PreviousGameFolder $GameFolder
    !insertmacro LoadShortcutChoices
    StrCpy $PlacementOption ""
    ${GetParameters} $0
    !ifdef EXTERNAL_MEDIA
        StrCpy $VoicePackMask -1
        IfSilent 0 packs_ready
        ClearErrors
        ${GetOptions} $0 "/VOICEPACKS=" $1
        IfErrors packs_ready
        StrCpy $VoicePackMask $1
packs_ready:
    !endif
    ClearErrors
    ${GetOptions} $0 "/REPAIR" $3
    IfErrors normal_start
    StrCmp $3 "" 0 normal_start
    StrCpy $PlacementOption "--repair"
normal_start:
    StrCpy $PhaseBar 0
    StrCpy $PhaseCancel 0
    StrCmp $INSTDIR "" use_installed
    Return
use_installed:
    StrCmp $GameFolder "" search
    IfFileExists "$GameFolder\Wow.exe" 0 search
    StrCpy $INSTDIR $GameFolder
    Return
search:
    Push "$PROGRAMFILES64\World of Warcraft\_forever_"
    Call RememberWowFolder
    Push "$PROGRAMFILES32\World of Warcraft\_forever_"
    Call RememberWowFolder
    Call FindWowOnFixedDrives
    StrCmp $DetectedWowFolder "" use_default
    StrCpy $INSTDIR $DetectedWowFolder
    Return
use_default:
    StrCpy $INSTDIR "$PROGRAMFILES64\World of Warcraft\_forever_"
FunctionEnd

Function FindWowOnFixedDrives
    StrCpy $8 "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    StrCpy $7 0
next:
    StrCpy $9 $8 1 $7
    StrCmp $9 "" done
    StrCpy $9 "$9:\"
    System::Call 'kernel32::GetDriveTypeW(w r9) i .r6'
    StrCmp $6 3 0 advance ; DRIVE_FIXED
    Push "$9World of Warcraft\_forever_"
    Call RememberWowFolder
    Push "$9Blizzard Entertainment\World of Warcraft\_forever_"
    Call RememberWowFolder
    Push "$9Games\World of Warcraft\_forever_"
    Call RememberWowFolder
    Push "$9Games\Blizzard Entertainment\World of Warcraft\_forever_"
    Call RememberWowFolder
    Push "$9Program Files\World of Warcraft\_forever_"
    Call RememberWowFolder
    Push "$9Program Files (x86)\World of Warcraft\_forever_"
    Call RememberWowFolder
advance:
    IntOp $7 $7 + 1
    Goto next
done:
FunctionEnd

Function RememberWowFolder
    Exch $0
    GetFullPathName $0 "$0"
    IfFileExists "$0\Wow.exe" 0 done
    StrCmp $DetectedWowFolder "" 0 done
    StrCpy $DetectedWowFolder $0
done:
    Pop $0
FunctionEnd

Function SkipKnownFolder
    StrCmp $GameFolder $INSTDIR 0 choose
    StrCmp $ShortcutsConfigured 1 0 choose
    IfFileExists "$INSTDIR\Wow.exe" 0 choose
    IfFileExists "$INSTDIR\Interface\AddOns\Spoken\Spoken.toc" 0 choose
    IfFileExists "$INSTDIR\Interface\AddOns\Spoken_Quests\Spoken_Quests.toc" 0 choose
    Abort
choose:
FunctionEnd

Function RequireSpoken
    IfFileExists "$INSTDIR\Interface\AddOns\Spoken\Spoken.toc" 0 missing
    IfFileExists "$INSTDIR\Interface\AddOns\Spoken_Quests\Spoken_Quests.toc" 0 missing
    IfFileExists "$INSTDIR\Wow.exe" checked
missing:
    SetErrorLevel 1
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(SpokenRequired)" /SD IDOK
    Abort
checked:
FunctionEnd

!ifdef PREFIX_ARCHIVE
; $4 is the staged fresh runtime or the existing installed runtime.
Function InstallPrefixMedia
    !insertmacro ShowPhase "$(PreparingSpeechData)"
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    ClearErrors
    SetCompress off
    File /oname=prefix-data.zip "${PREFIX_ARCHIVE}"
    SetCompress auto
    File /oname=install_prefix_media.py "${PREFIX_INSTALL_SCRIPT}"
    IfErrors media_extract_failed
    !insertmacro RunSupport `"$4\runtime\python\python.exe" -I -B "$PLUGINSDIR\install_prefix_media.py" "$4\." "$PLUGINSDIR\prefix-data.zip" "${PREFIX_MANIFEST_HASH}" "${PREFIX_ARCHIVE_HASH}"` $3
    Return
media_extract_failed:
    StrCpy $3 1
FunctionEnd
!endif

Section
    Call RequireSpoken
    !insertmacro ShowPhase "$(InstallingProgram)"
    !insertmacro StageCleanupTools
    IfErrors maintenance_error
    !ifdef EXTERNAL_MEDIA
        !insertmacro StageReleaseMedia "$EXEDIR\DMSpeaks-WoW.zip" "$INSTDIR" "" $3
        Call RestorePhaseCancel
        ${If} $3 == "4"
            SetErrorLevel 4
            Quit
        ${EndIf}
        ${If} $3 != "0"
            SetErrorLevel 1
            !insertmacro FailPhase "$(ReleaseMediaError)"
        ${EndIf}
    !endif
    StrCpy $0 "$LOCALAPPDATA\DMSpeaksLite\Builds"
    StrCpy $1 "$0\${BUILD}"
    StrCpy $2 ""
    ClearErrors
    CreateDirectory "$0"
    IfErrors unpack_error
    IfFileExists "$1\runtime\python\python.exe" refresh_lite
    IfFileExists "$1\." 0 place_fresh_runtime
    SetOutPath "$PLUGINSDIR"
    ClearErrors
    File /oname=open_lite.py "${OPEN_LITE_SCRIPT}"
    File /oname=install_addon.py "${PLACEMENT}"
    File /oname=stock-files.json.gz "${STOCK_OWNERS}"
    File /oname=addon-files.txt.gz "${OWNERS}"
    File /oname=DMSpeaks-Orc.ico "${COMPATIBILITY_ICON}"
    IfErrors maintenance_error
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" reconcile-orphan "$1" "$INSTDIR\." "$PLUGINSDIR"` $3
    StrCmp $3 "0" 0 damaged
place_fresh_runtime:
    ClearErrors
    GetTempFileName $2 "$0"
    IfErrors unpack_error
    Delete "$2"
    IfErrors unpack_error
    SetOutPath "$2"
    IfErrors unpack_error
    !include "${PAYLOAD_COMMANDS}"
    IfErrors unpack_error
    !ifdef EXTERNAL_MEDIA
        !insertmacro ExternalPrefix "$2" $3
        StrCmp $3 "0" +2
        Goto unpack_error
    !else ifdef PREFIX_ARCHIVE
        StrCpy $4 "$2"
        Call InstallPrefixMedia
        StrCmp $3 "0" +2
        Goto unpack_error
    !endif
    SetOutPath "$0"
    Rename "$2" "$1"
    IfErrors concurrent_install
    Goto start_lite
concurrent_install:
    IfFileExists "$1\runtime\python\python.exe" 0 damaged
    RMDir /r "$2"
refresh_lite:
    IfFileExists "$1\runtime\python\python.exe" 0 damaged
    !insertmacro ShowPhase "$(RefreshingProgram)"
    !ifdef EXTERNAL_MEDIA
        !insertmacro ExternalPrefix "$1" $3
        ${If} $3 != "0"
            SetErrorLevel 1
            !insertmacro FailPhase "$(SpeechDataError)"
        ${EndIf}
    !else ifdef PREFIX_ARCHIVE
        StrCpy $4 "$1"
        Call InstallPrefixMedia
        StrCmp $3 "0" prefix_media_ready
        SetErrorLevel 1
        !insertmacro FailPhase "$(SpeechDataError)"
prefix_media_ready:
    !endif
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=refresh_runtime.py "${REFRESH}"
    File /oname=apply_beta_update.py "${UPDATE_SCRIPT}"
    File /oname=install_addon.py "${PLACEMENT}"
    File /oname=open_lite.py "${OPEN_LITE_SCRIPT}"
    File /oname=runtime-update.zip "${RUNTIME_DELTA}"
    !insertmacro RunSupport `"$1\runtime\python\python.exe" -I -B "$PLUGINSDIR\refresh_runtime.py" "$INSTDIR\." "$PLUGINSDIR\runtime-update.zip"` $3
    StrCmp $3 "0" start_lite
    SetErrorLevel 1
    !insertmacro FailPhase "$(RefreshProgramError)"
start_lite:
    IfFileExists "$1\runtime\python\pythonw.exe" 0 damaged
    IfFileExists "$1\source\lite_web\index.html" 0 damaged
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    File /oname=install_addon.py "${PLACEMENT}"
    File /oname=stock-files.json.gz "${STOCK_OWNERS}"
    !insertmacro ShowPhase "$(PreparingVoices)"
    !insertmacro ShowOverall 25 "$(PreparingVoicesPhase)"
    !ifdef EXTERNAL_MEDIA
        nsExec::ExecToLog '"$1\runtime\python\python.exe" -I -B "$PLUGINSDIR\install_addon.py" "$INSTDIR\." "$ArchivePath" --catalog "$PLUGINSDIR\embedded-catalog.zip" --release-packs "$PLUGINSDIR\lite-packs.json" $PlacementOption --progress-window $PhaseBar --desktop-complete'
    !else
        File /oname=addon-update.zip "${DELTA}"
        nsExec::ExecToLog '"$1\runtime\python\python.exe" -I -B "$PLUGINSDIR\install_addon.py" "$INSTDIR\." "$EXEDIR\DMSpeaks-WoW.zip" "$PLUGINSDIR\addon-update.zip" $PlacementOption --progress-window $PhaseBar --desktop-complete'
    !endif
    Pop $3
    StrCmp $3 "0" installed
    StrCmp $3 "5" install_files_in_use
    SetErrorLevel 1
    !insertmacro FailPhase "$(InstallAddonError)"
install_files_in_use:
    SetErrorLevel 5
    !insertmacro FailPhase "$(PlacementFilesInUse)"
installed:
    !insertmacro ShowPhase "$(RemovingOldFiles)"
    !insertmacro RunRuntimeCleanup upgrade "${BUILD}" "$INSTDIR"
    StrCmp $0 "0" 0 cleanup_failed
    !insertmacro ShowPhase "$(FinishingSetup)"
    !insertmacro ShowOverall 97 "$(FinishingSetupPhase)"
    SetOutPath "${LITE_FOLDER}\Maintenance"
    ClearErrors
    File /oname=install_addon.py "${PLACEMENT}"
    File /oname=stock-files.json.gz "${STOCK_OWNERS}"
    File /oname=addon-files.txt.gz "${OWNERS}"
    File /oname=DMSpeaks-Orc.ico "${COMPATIBILITY_ICON}"
    File /oname=open_lite.py "${OPEN_LITE_SCRIPT}"
    WriteUninstaller "$OUTDIR\DMSpeaks-Maintenance.exe"
    IfErrors maintenance_error
    SetOutPath "$1"
    !insertmacro ShowPhase "$(SettingUpShortcuts)"
    !insertmacro ShowOverall 99 "$(ShortcutsPhase)"
    StrCpy $GameFolder $INSTDIR
    ClearErrors
    !insertmacro CreateLiteShortcut "${LITE_LAUNCHER}"
    IfErrors shortcut_error
    !insertmacro ApplyShortcut "${START_LINK}" $StartShortcut
    IfErrors shortcut_error
    !insertmacro ApplyShortcut "${DESKTOP_LINK}" $DesktopShortcut
    IfErrors shortcut_error
    SetOutPath "${LITE_FOLDER}\Maintenance"
    !ifdef EXTERNAL_MEDIA
        Call FindInstalledVoicePacks
        ReadRegDWORD $0 HKCU "${APP_ENTRY}\VoicePacks" "$INSTDIR"
        ${IfNot} ${Errors}
            IntOp $ExistingVoicePacks $ExistingVoicePacks | $0
        ${EndIf}
    !endif
    ClearErrors
    WriteRegStr HKCU "${APP_ENTRY}" "DisplayName" "${EDITION}"
    WriteRegStr HKCU "${APP_ENTRY}" "DisplayVersion" "0.1.0-beta.12"
    WriteRegStr HKCU "${APP_ENTRY}" "InstallLocation" "${LITE_FOLDER}"
    WriteRegStr HKCU "${APP_ENTRY}" "DisplayIcon" '"${LITE_ICON}",0'
    WriteRegStr HKCU "${APP_ENTRY}" "CleanupRevision" "${SETUP_ID}"
    WriteRegStr HKCU "${APP_ENTRY}" "ActiveBuild" "${BUILD}"
    WriteRegStr HKCU "${APP_ENTRY}" "GameFolder" "$INSTDIR"
    !ifdef EXTERNAL_MEDIA
        WriteRegStr HKCU "${APP_ENTRY}" "ArchivePath" "$ArchivePath"
        WriteRegDWORD HKCU "${APP_ENTRY}\VoicePacks" "$INSTDIR" $ExistingVoicePacks
    !else
        IfFileExists "$EXEDIR\DMSpeaks-WoW.zip" 0 keep_archive_path
        WriteRegStr HKCU "${APP_ENTRY}" "ArchivePath" "$EXEDIR\DMSpeaks-WoW.zip"
keep_archive_path:
    !endif
    WriteRegStr HKCU "${APP_ENTRY}" "UninstallString" '"$OUTDIR\DMSpeaks-Maintenance.exe" /UNINSTALL'
    WriteRegStr HKCU "${APP_ENTRY}" "ModifyPath" '"$OUTDIR\DMSpeaks-Maintenance.exe" /SHORTCUTS'
    !insertmacro SaveShortcutChoices
    WriteRegDWORD HKCU "${APP_ENTRY}" "NoRepair" 1
    IfErrors maintenance_error
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" record "$1" "$GameFolder\." "$PLUGINSDIR"` $0
    StrCmp $0 "0" 0 maintenance_error
    ReadRegStr $0 HKCU "${APP_ENTRY}" "LegacyShortcutNamesMigrated"
    ${If} $0 == ""
    ${AndIf} $PreviousGameFolder != ""
        Delete "$SMPROGRAMS\DMSpeaks Lite.lnk"
        Delete "$DESKTOP\DMSpeaks Lite.lnk"
        Delete "${LITE_FOLDER}\DMSpeaks Lite.lnk"
    ${EndIf}
    WriteRegStr HKCU "${APP_ENTRY}" "LegacyShortcutNamesMigrated" "1"
    DeleteRegValue HKCU "${APP_ENTRY}" "AddonFilesRemoved"
    Goto done
cleanup_failed:
    SetErrorLevel 1
    !insertmacro FailPhase "$(CleanupIncomplete)"
maintenance_error:
    SetErrorLevel 1
    !insertmacro FailPhase "$(RegisterMaintenanceError)"
shortcut_error:
    SetErrorLevel 1
    !insertmacro FailPhase "$(CreateShortcutError)"
unpack_error:
    SetOutPath "$LOCALAPPDATA"
    StrCmp $2 "" +2
    RMDir /r "$2"
    SetErrorLevel 1
    !insertmacro FailPhase "$(SaveProgramError)"
damaged:
    SetErrorLevel 1
    !insertmacro FailPhase "$(IncompleteBuild)"
done:
    ClearErrors
    FileOpen $0 "$INSTDIR\Interface\.dmspeaks-setup-version.txt" w
    IfErrors recorded
    FileWrite $0 "${SETUP_ID}$\r$\n"
    FileClose $0
recorded:
    ClearErrors
    !insertmacro ShowOverall 100 "$(InstallComplete)"
SectionEnd


Function un.onInit
    !insertmacro AcquireMaintenanceLock
    StrCpy $OtherClients "0"
    StrCpy $RemoveNameData 0
    StrCpy $PhaseBar 0
    StrCpy $PhaseCancel 0
    SetRegView 64
    StrCpy $INSTDIR "$LOCALAPPDATA\DMSpeaksLite\Maintenance"
    ReadRegStr $GameFolder HKCU "${APP_ENTRY}" "GameFolder"
    ReadRegStr $ArchivePath HKCU "${APP_ENTRY}" "ArchivePath"
    StrCmp $GameFolder "" unknown
    !insertmacro LoadShortcutChoices
    StrCpy $PlacementOption "repair"
    ${un.GetParameters} $0
    ClearErrors
    ${un.GetOptions} $0 "/RECOVERYCHECK" $1
    ${IfNot} ${Errors}
        StrCpy $PlacementOption "recoverycheck"
        Goto launch
    ${EndIf}
    ClearErrors
    ${un.GetOptions} $0 "/UNINSTALL" $1
    ${IfNot} ${Errors}
        StrCpy $PlacementOption "remove"
        Goto launch
    ${EndIf}
    ClearErrors
    ${un.GetOptions} $0 "/SHORTCUTS" $1
    ${IfNot} ${Errors}
        StrCpy $PlacementOption "shortcuts"
    ${EndIf}
launch:
    Return
unknown:
    SetErrorLevel 1
    !insertmacro FailPhase "$(KnownGameMissing)"
FunctionEnd

; Maintenance uses the same window marker and lock as Setup.
Function un.RegisterMaintenanceWizard
    !insertmacro RegisterMaintenanceWindow
FunctionEnd

Function un.MaintenancePage
    !insertmacro MUI_HEADER_TEXT "$(MaintenanceTitle)" "$(MaintenanceSubtitle)"
    nsDialogs::Create 1018
    Pop $0
    StrCmp $0 error failure
    ${NSD_CreateRadioButton} 0 2u 100% 14u "$(RepairEdition)"
    Pop $RepairChoice
    ${NSD_CreateRadioButton} 0 21u 100% 14u "$(RemoveEdition)"
    Pop $RemoveChoice
    ${NSD_CreateRadioButton} 0 40u 100% 14u "$(ChangeShortcuts)"
    Pop $ShortcutChoice
    ${NSD_CreateCheckbox} 10u 63u 95% 16u "$(DeleteRecordings)"
    Pop $RemoveNamesControl
    ${NSD_SetState} $RemoveNamesControl $RemoveNameData
    ${NSD_CreateLabel} 10u 84u 94% 45u ""
    Pop $ChoiceHelp
    !insertmacro AddShortcutControls 63u 84u
    ${NSD_OnClick} $RepairChoice un.ShortcutControlState
    ${NSD_OnClick} $RemoveChoice un.ShortcutControlState
    ${NSD_OnClick} $ShortcutChoice un.ShortcutControlState
    ${If} $PlacementOption == "remove"
        ${NSD_Check} $RemoveChoice
    ${ElseIf} $PlacementOption == "shortcuts"
        ${NSD_Check} $ShortcutChoice
    ${Else}
        ${NSD_Check} $RepairChoice
    ${EndIf}
    Push 0
    Call un.ShortcutControlState
    nsDialogs::Show
    Return
failure:
    SetErrorLevel 1
    Quit
FunctionEnd

Function un.ShortcutControlState
    Pop $0
    GetDlgItem $1 $HWNDPARENT 1
    ShowWindow $StartControl ${SW_HIDE}
    ShowWindow $DesktopControl ${SW_HIDE}
    ShowWindow $RemoveNamesControl ${SW_HIDE}
    ShowWindow $ChoiceHelp ${SW_SHOW}
    ${NSD_GetState} $RemoveChoice $0
    ${If} $0 == ${BST_CHECKED}
        ShowWindow $RemoveNamesControl ${SW_SHOW}
        ${NSD_SetText} $ChoiceHelp "$(DeleteRecordingsHelp)"
        SendMessage $1 ${WM_SETTEXT} 0 "STR:$(UninstallButton)"
    ${Else}
        ${NSD_Uncheck} $RemoveNamesControl
        ${NSD_GetState} $ShortcutChoice $0
        ${If} $0 == ${BST_CHECKED}
            ShowWindow $ChoiceHelp ${SW_HIDE}
            ShowWindow $StartControl ${SW_SHOW}
            ShowWindow $DesktopControl ${SW_SHOW}
            SendMessage $1 ${WM_SETTEXT} 0 "STR:$(SaveShortcutsButton)"
        ${Else}
            ${NSD_SetText} $ChoiceHelp "$(RepairHelp)"
            SendMessage $1 ${WM_SETTEXT} 0 "STR:$(RepairButton)"
        ${EndIf}
    ${EndIf}
FunctionEnd

Function un.ReadMaintenanceChoice
    ${NSD_GetState} $ShortcutChoice $0
    StrCmp $0 ${BST_CHECKED} shortcuts
    ${NSD_GetState} $RemoveChoice $0
    StrCmp $0 ${BST_CHECKED} remove
    StrCpy $PlacementOption "repair"
    StrCpy $MaintenanceTitle "$(RepairingTitle)"
    StrCpy $MaintenanceDescription "$(RepairingDescription)"
    !ifdef EXTERNAL_MEDIA
        StrCmp $ArchivePath "" 0 done
    !else
        IfFileExists "$ArchivePath" done
    !endif
    nsDialogs::SelectFileDialog open "$GameFolder" "$(VoiceZipFilter)|DMSpeaks-WoW.zip|$(ZipFilter)|*.zip"
    Pop $0
    StrCmp $0 "" canceled
    StrCpy $ArchivePath $0
    Return
remove:
    ${NSD_GetState} $RemoveNamesControl $RemoveNameData
    StrCpy $MaintenanceTitle "$(RemovingTitle)"
    ${If} $RemoveNameData == ${BST_CHECKED}
        StrCpy $MaintenanceDescription "$(RemovingRecordings)"
    ${Else}
        StrCpy $MaintenanceDescription "$(KeepingRecordings)"
    ${EndIf}
    StrCpy $PlacementOption "remove"
    Return
shortcuts:
    StrCpy $MaintenanceTitle "$(ShortcutsTitle)"
    StrCpy $MaintenanceDescription "$(ShortcutsDescription)"
    StrCpy $PlacementOption "shortcuts"
    ${NSD_GetState} $StartControl $StartShortcut
    ${NSD_GetState} $DesktopControl $DesktopShortcut
done:
    Return
canceled:
    Abort
FunctionEnd

Section "Uninstall"
    !insertmacro QuietDetails
    ReadRegStr $ActiveBuild HKCU "${APP_ENTRY}" "ActiveBuild"
    StrCmp $ActiveBuild "" 0 +2
    StrCpy $ActiveBuild "${BUILD}"
    ; Only a lower-case 20-digit installation identifier can address Builds.
    StrLen $2 $ActiveBuild
    StrCmp $2 20 0 damaged
    StrCpy $2 0
validate_build:
    StrCpy $3 $ActiveBuild 1 $2
    StrCmp $3 "" build_ready
    System::Call 'shlwapi::StrSpnW(w r3, w "0123456789abcdef") p .r3'
    StrCmp $3 1 0 damaged
    IntOp $2 $2 + 1
    Goto validate_build
build_ready:
    StrCpy $1 "$LOCALAPPDATA\DMSpeaksLite\Builds\$ActiveBuild"
    StrCpy $GamePython "$1\runtime\python\python.exe"
    SetOutPath "$INSTDIR"
    StrCmp $PlacementOption "recoverycheck" remove
    StrCmp $PlacementOption "remove" remove
    StrCmp $PlacementOption "shortcuts" shortcuts
    !ifdef EXTERNAL_MEDIA
        StrCmp $ArchivePath "" repair_missing
    !else
        IfFileExists "$ArchivePath" 0 repair_missing
    !endif
    IfFileExists "$1\runtime\python\python.exe" 0 damaged
    IfSilent repair_run
    StrCmp $PhaseCancel 0 placement_error
repair_run:
    !insertmacro ShowPhase "$(CheckingVoices)"
    !ifdef EXTERNAL_MEDIA
        !insertmacro StageCleanupTools
        IfErrors placement_error
        ReadRegDWORD $VoicePackMask HKCU "${APP_ENTRY}\VoicePacks" "$GameFolder"
        IfErrors 0 +2
        StrCpy $VoicePackMask 0
        !insertmacro StageReleaseMedia "$ArchivePath" "$GameFolder" "--include-installed" $0
        StrCmp $0 "4" repair_canceled
        StrCmp $0 "0" 0 placement_error
        EnableWindow $PhaseCancel 1
        !insertmacro ExternalPrefix "$1" $0
        StrCmp $0 "0" 0 placement_error
        SetOutPath "$INSTDIR"
        !insertmacro ShowOverall 25 "$(PreparingVoicesPhase)"
        nsExec::ExecToLog '"$1\runtime\python\python.exe" -I -B "$INSTDIR\install_addon.py" "$GameFolder\." "$ArchivePath" --catalog "$PLUGINSDIR\embedded-catalog.zip" --release-packs "$PLUGINSDIR\lite-packs.json" --repair --progress-window $PhaseBar --repair-cancel-window $PhaseCancel --desktop-complete'
    !else
        nsExec::ExecToLog '"$1\runtime\python\python.exe" -I -B "$INSTDIR\install_addon.py" "$GameFolder\." "$ArchivePath" --repair --progress-window $PhaseBar --repair-cancel-window $PhaseCancel'
    !endif
    Pop $0
    Call un.RestorePhaseCancel
    StrCmp $0 "4" repair_canceled
    StrCmp $0 "5" repair_files_in_use
    StrCmp $0 "0" 0 placement_error
    WriteRegStr HKCU "${APP_ENTRY}" "ArchivePath" "$ArchivePath"
    DeleteRegValue HKCU "${APP_ENTRY}" "AddonFilesRemoved"
    Goto done
remove:
    ${If} $PlacementOption == "remove"
        SetDetailsView show
        !insertmacro ShowPhase "$(PreparingRemoval)"
    ${EndIf}
    ; The uninstaller carries its own tools so a missing Maintenance folder can be recovered.
    InitPluginsDir
    SetOutPath "$PLUGINSDIR"
    ClearErrors
    File /oname=install_addon.py "${PLACEMENT}"
    File /oname=addon-files.txt.gz "${OWNERS}"
    File /oname=stock-files.json.gz "${STOCK_OWNERS}"
    File /oname=open_lite.py "${OPEN_LITE_SCRIPT}"
    IfErrors cleanup_failed
    File /oname=DMSpeaks-Orc.ico "${COMPATIBILITY_ICON}"
    File /oname=legacy.ico "${LEGACY_ICON}"
    !insertmacro StageCleanupTools
    IfErrors cleanup_failed
    !insertmacro ShowPhase "$(ClosingLite)"
    IfFileExists "$GamePython" 0 stop_unavailable
    IfFileExists "$1\runtime\python\python313.dll" 0 stop_unavailable
    IfFileExists "$1\source\wow_fetch_desktop.py" 0 stop_unavailable
    IfSilent close_lite
    !insertmacro RunSupport `"$GamePython" -I -B "$PLUGINSDIR\open_lite.py" --check-for-uninstall "$GameFolder\."` $0
    StrCmp $0 "0" removed_game_check
    StrCmp $0 "10" 0 stop_unavailable
    MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "$(CloseLiteForRemoval)" /SD IDNO IDNO removal_canceled
close_lite:
    !insertmacro RunSupport `"$GamePython" -I -B "$PLUGINSDIR\open_lite.py" --stop-for-uninstall "$GameFolder\."` $0
    StrCmp $0 "0" removed_game_check
stop_unavailable:
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" stop-check "$1" "$GameFolder\." "$PLUGINSDIR"` $0
    StrCmp $0 "0" 0 stop_failed
    StrCpy $GamePython "$PLUGINSDIR\cleanup\python.exe"
removed_game_check:
    StrCmp $PlacementOption "recoverycheck" done
    !insertmacro ShowPhase "$(CheckingSharedInstall)"
    !insertmacro RunRuntimeCleanup inspect "$ActiveBuild" "$GameFolder"
    StrCmp $0 "0" 0 cleanup_failed
    ReadINIStr $OtherClients "$PLUGINSDIR\RuntimeCleanup.ini" "clients" "shared"
    StrCpy $SharedOption ""
    StrCmp $OtherClients "0" inspected
    StrCmp $OtherClients "1" 0 cleanup_failed
    StrCpy $SharedOption "--shared-cache"
inspected:
    IfFileExists "$GamePython" +2
    StrCpy $GamePython "$PLUGINSDIR\cleanup\python.exe"
    ReadRegDWORD $0 HKCU "${APP_ENTRY}" "AddonFilesRemoved"
    StrCmp $0 1 remove_lite
    !insertmacro ShowPhase "$(RemovingAddon)"
    nsExec::ExecToLog '"$GamePython" -I -B "$PLUGINSDIR\install_addon.py" "$GameFolder\." "$INSTDIR" --uninstall "$PLUGINSDIR\addon-files.txt.gz" --progress-window $PhaseBar'
    Pop $0
    StrCmp $0 "2" catalog_in_use
    StrCmp $0 "3" name_sync_pending
    StrCmp $0 "0" 0 placement_error
    WriteRegDWORD HKCU "${APP_ENTRY}" "AddonFilesRemoved" 1
remove_lite:
    !insertmacro ShowOverall 92 "$(FinishingRemoval)"
    ${If} $RemoveNameData == ${BST_CHECKED}
        !insertmacro ShowPhase "$(RemovingNames)"
        nsExec::ExecToLog '"$GamePython" -I -B "$PLUGINSDIR\install_addon.py" "$GameFolder\." "$INSTDIR" --remove-saved-names $SharedOption --progress-window $PhaseBar'
        Pop $0
        StrCmp $0 "3" name_sync_pending
        StrCmp $0 "0" 0 name_cleanup_failed
    ${EndIf}
    !insertmacro ShowPhase "$(RemovingGameMarkers)"
    !insertmacro RunSupport `"$GamePython" -I -B "$PLUGINSDIR\install_addon.py" "$GameFolder\." "$INSTDIR" --remove-game-markers` $0
    StrCmp $0 "0" 0 cleanup_failed
    !insertmacro ShowPhase "$(RemovingApp)"
    !insertmacro ShowOverall 96 "$(RemovingAppPhase)"
    SetOutPath "$TEMP"
    !insertmacro RunRuntimeCleanup uninstall "$ActiveBuild" "$GameFolder"
    StrCmp $0 "0" 0 cleanup_failed
    ReadINIStr $OtherClients "$PLUGINSDIR\RuntimeCleanup.ini" "clients" "shared"
    StrCmp $OtherClients "0" remove_last
    StrCmp $OtherClients "1" 0 cleanup_failed
    ReadINIStr $PeerGame "$PLUGINSDIR\RuntimeCleanup.ini" "clients" "game"
    ReadINIStr $PeerBuild "$PLUGINSDIR\RuntimeCleanup.ini" "clients" "build"
    StrCmp $PeerGame "" cleanup_failed
    StrCmp $PeerBuild "" cleanup_failed
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" remove-marker "$1" "$GameFolder\." "$PLUGINSDIR"` $0
    StrCmp $0 "0" 0 cleanup_failed
    ; Recovery can run when the shared shell was lost. Restore its owned files
    ; before pointing Installed apps or the common shortcuts at the surviving game.
    ClearErrors
    CreateDirectory "$INSTDIR"
    !insertmacro RestoreShellFile "open_lite.py"
    !insertmacro RestoreShellFile "install_addon.py"
    !insertmacro RestoreShellFile "stock-files.json.gz"
    !insertmacro RestoreShellFile "addon-files.txt.gz"
    !insertmacro RestoreShellFile "DMSpeaks-Orc.ico"
    IfFileExists "$INSTDIR\DMSpeaks-Maintenance.exe" shell_present
    CopyFiles /SILENT /FILESONLY "$EXEPATH" "$INSTDIR\DMSpeaks-Maintenance.exe"
    IfErrors cleanup_failed
shell_present:
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" record "$1" "$GameFolder\." "$PLUGINSDIR"` $0
    StrCmp $0 "0" 0 cleanup_failed
    !ifdef EXTERNAL_MEDIA
        DeleteRegValue HKCU "${APP_ENTRY}\VoicePacks" "$GameFolder"
    !endif
    StrCpy $GameFolder $PeerGame
    StrCpy $1 "$LOCALAPPDATA\DMSpeaksLite\Builds\$PeerBuild"
    SetOutPath "$1"
    ClearErrors
    !insertmacro CreateLiteShortcut "${LITE_LAUNCHER}"
    IfErrors shortcut_error
    !insertmacro ApplyShortcut "${START_LINK}" $StartShortcut
    !insertmacro ApplyShortcut "${DESKTOP_LINK}" $DesktopShortcut
    IfErrors shortcut_error
    WriteRegStr HKCU "${APP_ENTRY}" "ActiveBuild" "$PeerBuild"
    WriteRegStr HKCU "${APP_ENTRY}" "GameFolder" "$PeerGame"
    IfErrors cleanup_failed
    DeleteRegValue HKCU "${APP_ENTRY}" "AddonFilesRemoved"
    Goto done
remove_last:
    !insertmacro ShowPhase "$(RemovingShortcuts)"
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" remove-marker "$1" "$GameFolder\." "$PLUGINSDIR"` $0
    StrCmp $0 "0" 0 cleanup_failed
    !insertmacro RunSupport `"$PLUGINSDIR\cleanup\python.exe" -I -B "$PLUGINSDIR\cleanup\shell_maintenance.py" remove-last "$1" "$GameFolder\." "$PLUGINSDIR"` $0
    StrCmp $0 "0" 0 cleanup_failed
    ClearErrors
    DeleteRegKey HKCU "${APP_ENTRY}"
    IfErrors cleanup_failed
    RMDir "$INSTDIR"
    RMDir "$LOCALAPPDATA\DMSpeaksLite\Builds"
    RMDir "$LOCALAPPDATA\DMSpeaksLite"
    Goto done
shortcuts:
    IfFileExists "$1\runtime\python\pythonw.exe" 0 damaged
    SetOutPath "$1"
    ClearErrors
    !insertmacro ApplyShortcut "${START_LINK}" $StartShortcut
    IfErrors shortcut_error
    !insertmacro ApplyShortcut "${DESKTOP_LINK}" $DesktopShortcut
    IfErrors shortcut_error
    !insertmacro SaveShortcutChoices
    IfErrors shortcut_error
    Goto done
shortcut_error:
    SetErrorLevel 1
    !insertmacro FailPhase "$(UpdateShortcutsError)"
repair_missing:
    Call un.RestorePhaseCancel
    SetErrorLevel 1
    !insertmacro FailPhase "$(SelectRepairArchive)"
repair_canceled:
    Call un.RestorePhaseCancel
    StrCpy $MaintenanceTitle "$(RepairCanceled)"
    StrCpy $MaintenanceDescription "$(RepairCanceledDetails)"
    !insertmacro ShowPhase "$(RepairCanceled)"
    SetErrorLevel 4
    Goto section_end
removal_canceled:
    StrCpy $MaintenanceTitle "$(RemovalCanceled)"
    StrCpy $MaintenanceDescription "$(RemovalCanceledDetails)"
    !insertmacro ShowPhase "$(RemovalCanceled)"
    SetErrorLevel 4
    Goto section_end
repair_files_in_use:
    SetErrorLevel 5
    !insertmacro FailPhase "$(PlacementFilesInUse)"
damaged:
    Call un.RestorePhaseCancel
    SetErrorLevel 1
    !insertmacro FailPhase "$(RestoreSetupFirst)"
placement_error:
    Call un.RestorePhaseCancel
    SetErrorLevel 1
    !insertmacro FailPhase "$(MaintenanceError)"
catalog_in_use:
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(CatalogOpen)" /SD IDOK
    SetErrorLevel 2
    !insertmacro ShowPhase "$(CloseGame)"
    Abort "$(CatalogUnchanged)"
name_sync_pending:
    SetErrorLevel 3
    !insertmacro FailPhase "$(StorageOffline)"
name_cleanup_failed:
    SetErrorLevel 1
    !insertmacro FailPhase "$(PurgeIncomplete)"
stop_failed:
    SetErrorLevel 1
    !insertmacro FailPhase "$(CloseFailed)"
cleanup_failed:
    SetErrorLevel 1
    !insertmacro FailPhase "$(CleanupIncomplete)"
lite_running:
    SetErrorLevel 1
    !insertmacro FailPhase "$(SignOutFirst)"
done:
    ${If} $PlacementOption == "remove"
        StrCpy $MaintenanceTitle "$(RemovalComplete)"
        ${If} $OtherClients == "1"
            StrCpy $MaintenanceTitle "$(GameRemovalComplete)"
        ${EndIf}
        StrCpy $MaintenanceDescription ""
    ${ElseIf} $PlacementOption == "repair"
        StrCpy $MaintenanceTitle "$(RepairComplete)"
        StrCpy $MaintenanceDescription "$(RepairCompleteDetails)"
    ${Else}
        StrCpy $MaintenanceTitle "$(ShortcutsComplete)"
        StrCpy $MaintenanceDescription "$(ShortcutsCompleteDetails)"
    ${EndIf}
    !insertmacro ShowOverall 100 "$MaintenanceTitle"
section_end:
SectionEnd
