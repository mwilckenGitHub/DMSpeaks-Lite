; The bits match stage_release_media.COLLECTIONS.
!define ALL_VOICE_PACKS 15
!macro VoicePacks action
    !insertmacro ${action} 1 "Alliance" $AllianceItem
    !insertmacro ${action} 2 "Horde" $HordeItem
    !insertmacro ${action} 4 "Shared" $SharedItem
    !insertmacro ${action} 8 "Gossip" $GossipItem
!macroend

Var VoicePackFolder ; Preserve the user's choices when returning to the same game folder.
Var ExistingVoicePacks ; A bit mask read from the installed pack manifests.
Var VoicePackTree ; The native tree control for this visit to the page.
Var VoicePackUpdating ; Suppress notifications caused by programmatic checkbox updates.
; Handles for the selectable rows. Child folder rows inherit the pack's selection.
Var AllPacksItem
Var AllianceItem
Var HordeItem
Var SharedItem
Var GossipItem

!macro FindVoicePack bit name item
    ${If} ${FileExists} "$INSTDIR\Interface\AddOns\DMSpeaksPersonalized${name}\DMSpeaksPersonalized${name}.toc"
        IntOp $ExistingVoicePacks $ExistingVoicePacks | ${bit}
    ${EndIf}
!macroend

; Start with the packs already installed in the selected game folder.
Function FindInstalledVoicePacks
    StrCpy $ExistingVoicePacks 0
    !insertmacro VoicePacks FindVoicePack
FunctionEnd

!define TVM_EXPAND 0x1102
!define TVM_SETEXTENDEDSTYLE 0x112C
!define TVM_INSERTITEMW 0x1132
!define TVM_SETITEMW 0x113F
!define TVN_ITEMCHANGEDW -419
!define TVI_ROOT -65536
!define TVI_LAST -65534
!define TVS_EX_PARTIALCHECKBOXES 0x80
!define TVIS_STATEIMAGEMASK 0xF000
!define TREE_UNCHECKED 0x1000
!define TREE_CHECKED 0x2000
!define TREE_MIXED 0x3000

!macro SetVoicePackCheck item state
    ; TVITEMW on amd64, using TVIF_STATE.
    System::Call '*(i 8, i 0, p ${item}, i ${state}, i ${TVIS_STATEIMAGEMASK}, p 0, i 0, i 0, i 0, i 0, p 0) p.r3'
    SendMessage $VoicePackTree ${TVM_SETITEMW} 0 $3
    System::Free $3
!macroend

!macro InsertVoicePackItem parent label bit item
    ; TVINSERTSTRUCTW on amd64, using TVIF_TEXT and TVIF_PARAM.
    System::Call '*(&t${NSIS_MAX_STRLEN} "${label}") p.r4'
    System::Call '*(p ${parent}, p ${TVI_LAST}, i 5, i 0, p 0, i 0, i 0, p r4, i 0, i 0, i 0, i 0, p ${bit}) p.r5'
    SendMessage $VoicePackTree ${TVM_INSERTITEMW} 0 $5 ${item}
    System::Free $5
    System::Free $4
    !if ${bit} == 0
        !insertmacro SetVoicePackCheck ${item} 0
    !endif
!macroend

!macro AddVoicePackItem bit name item
    !insertmacro InsertVoicePackItem $AllPacksItem "$(VoicePack${name})" ${bit} ${item}
    !insertmacro InsertVoicePackItem ${item} "DMSpeaksSpoken${name}" 0 $2
    !insertmacro InsertVoicePackItem ${item} "DMSpeaksPersonalized${name}" 0 $2
!macroend

!macro ShowVoicePackCheck bit name item
    IntOp $1 $VoicePackMask & ${bit}
    ${If} $1 != 0
        !insertmacro SetVoicePackCheck ${item} ${TREE_CHECKED}
    ${Else}
        !insertmacro SetVoicePackCheck ${item} ${TREE_UNCHECKED}
    ${EndIf}
!macroend

; Reflect the download mask in the individual rows and the three-state All row.
Function SyncVoicePackChecks
    StrCpy $VoicePackUpdating 1
    ${If} $VoicePackMask == ${ALL_VOICE_PACKS}
        !insertmacro SetVoicePackCheck $AllPacksItem ${TREE_CHECKED}
    ${ElseIf} $VoicePackMask == 0
        !insertmacro SetVoicePackCheck $AllPacksItem ${TREE_UNCHECKED}
    ${Else}
        !insertmacro SetVoicePackCheck $AllPacksItem ${TREE_MIXED}
    ${EndIf}
    !insertmacro VoicePacks ShowVoicePackCheck
    StrCpy $VoicePackUpdating 0
FunctionEnd

; Update the download mask only when a checkbox changes. Navigating the tree
; or opening a pack's child folders has no effect on the selection.
Function VoicePackTreeChanged
    System::Store S
    Pop $0 ; Control HWND
    Pop $1 ; Notification code
    Pop $2 ; Notification data
    ; nsDialogs passes the unsigned notification code as a 64-bit string.
    ${If} $1 = ${TVN_ITEMCHANGEDW}
    ${AndIf} $VoicePackUpdating == 0
        ; NMTVITEMCHANGE on amd64. Read the handle, states and pack bit mask.
        System::Call '*$2(p, p, i, i, i, i, p .r4, i .r5, i .r6, p .r7)'
        IntOp $5 $5 & ${TVIS_STATEIMAGEMASK}
        IntOp $6 $6 & ${TVIS_STATEIMAGEMASK}
        ${If} $5 != $6
            ${If} $7 == 0
                ; Common folders are always installed, even when selected with Space.
                StrCpy $VoicePackUpdating 1
                !insertmacro SetVoicePackCheck $4 0
                StrCpy $VoicePackUpdating 0
            ${Else}
                ${If} $7 == ${ALL_VOICE_PACKS}
                    ${If} $VoicePackMask == ${ALL_VOICE_PACKS}
                        StrCpy $VoicePackMask 0
                    ${Else}
                        StrCpy $VoicePackMask ${ALL_VOICE_PACKS}
                    ${EndIf}
                ${ElseIf} $5 = ${TREE_CHECKED}
                    IntOp $VoicePackMask $VoicePackMask | $7
                ${Else}
                    IntOp $7 $7 ^ ${ALL_VOICE_PACKS}
                    IntOp $VoicePackMask $VoicePackMask & $7
                ${EndIf}
                Call SyncVoicePackChecks
            ${EndIf}
        ${EndIf}
    ${EndIf}
    System::Store L
FunctionEnd

; Display the common folders and optional packs under the game's AddOns folder.
Function VoicePackPage
    ${If} $VoicePackFolder != $INSTDIR
        StrCpy $VoicePackFolder $INSTDIR
        Call FindInstalledVoicePacks
        StrCpy $VoicePackMask $ExistingVoicePacks
    ${EndIf}
    !insertmacro MUI_HEADER_TEXT "$(VoicePackTitle)" "$(VoicePackSubtitle)"
    nsDialogs::Create 1018
    Pop $0
    StrCmp $0 error failed
    ${NSD_CreateLabel} 0 3u 100% 21u "$(VoicePackDescription)"
    Pop $0
    ${NSD_InitCommonControlsEx} 0x0002 ; ICC_TREEVIEW_CLASSES
    ; Native tree lines, expand buttons and keyboard navigation.
    nsDialogs::CreateControl "SysTreeView32" 0x50010027 0x00000200 0 27u 100% 100u ""
    Pop $VoicePackTree
    StrCmp $VoicePackTree error failed
    SendMessage $VoicePackTree ${TVM_SETEXTENDEDSTYLE} ${TVS_EX_PARTIALCHECKBOXES} ${TVS_EX_PARTIALCHECKBOXES}
    !insertmacro InsertVoicePackItem ${TVI_ROOT} "Interface\AddOns" 0 $R0
    !insertmacro InsertVoicePackItem $R0 "$(VoicePackAll)" ${ALL_VOICE_PACKS} $AllPacksItem
    !insertmacro VoicePacks AddVoicePackItem
    !insertmacro InsertVoicePackItem $R0 "DMSpeaksLite" 0 $2
    !insertmacro InsertVoicePackItem $R0 "DMSpeaksNameData" 0 $2
    !insertmacro InsertVoicePackItem $R0 "DMSpeaksSpokenNames" 0 $2
    Call SyncVoicePackChecks
    SendMessage $VoicePackTree ${TVM_EXPAND} 2 $R0
    SendMessage $VoicePackTree ${TVM_EXPAND} 2 $AllPacksItem
    ${NSD_OnNotify} $VoicePackTree VoicePackTreeChanged
    ${NSD_CreateLabel} 0 133u 100% 19u "$(VoicePackNote)"
    Pop $0
    nsDialogs::Show
    Return
failed:
    SetErrorLevel 1
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(VoicePackPageError)"
    Quit
FunctionEnd
