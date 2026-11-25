local _, addon = ...
-- Locale packs may populate addon.locales before Settings.lua. SavedVariables
-- and request actions always use their original IDs; only display text varies.
local english = {
    currentCharacter = "Current character",
    chooseCharacter = "Choose a character",
    character = "Fetch recordings for",
    notSelected = "Not selected",
    fetch = "Fetch Names",
    check = "Check Coverage",
    pause = "Pause Fetch",
    desktopControls = "Open DMSpeaks Lite from the Windows Start menu to manage fetching.",
    syncCharacter = "Reload UI to sync character",
    syncCharacterHint = "If Lite shows another character, use this button or type /reload in WoW.",
    newCharacterSync = "Reload the WoW UI to make {name} available in DMSpeaks Lite. To do this later, type /reload in chat.",
    settings = "Fetch Settings",
    refresh = "Check Lite Connection",
    help = "Setup Help",
    copyUrl = "Copy Settings URL",
    followsDesktop = "Choose the speech provider and fetch area in Lite’s Fetch Settings.",
    progress = "Fetch progress",
    bar = "Progress bar",
    compact = "Count only",
    hideProgress = "Hide progress",
    bottomRight = "Bottom right",
    bottomLeft = "Bottom left",
    topLeft = "Top left",
    position = "In-game progress overlay position",
    displayHeading = "Display options",
    setupHeading = "Setup / advanced",
    contentHeading = "Installed content",
    contentAlliance = "Alliance Audio",
    contentHorde = "Horde Audio",
    contentShared = "Shared Audio",
    contentGossip = "Gossip Audio",
    contentCatalog = "Name Fetch Data",
    contentPresent = "Present",
    contentMissing = "Missing",
    contentDisabled = "Disabled",
    contentUnchecked = "Not checked",
    contentRow = "{name} · {status}",
    contentChecked = "Last checked by Lite {time}",
    contentHint = "Start Lite and reload WoW to see installed content.",
    emptyCoverage = "No saved coverage for this character and selection. Check Coverage counts it again in Lite.",
    startLite = "Start Lite from your desktop or app launcher.",
    reloadHint = "Reload when prompted to send your request to Lite.",
    sendRequest = "Reload to send your request.",
    canceled = "Request canceled.",
    tryAgain = "Close another dialog, then try again.",
    reload = "Reload UI",
    later = "Later",
    confirmFetch = "Reload to send your request to DMSpeaks Lite.",
    confirmCoverage = "Check Coverage asks Lite to count saved name audio again. Reload to send the request; DMSpeaks will reopen after the reload.",
    confirmSetup = "Open DMSpeaks Lite - WOW from your desktop or app launcher first. Reload the UI to ask Lite to open your browser.",
    chooseFirst = "Choose an available character first.",
    scopeStarting = "Starting area",
    scopeNearby = "Nearby zones",
    scopeAll = "All areas",
    connected = "Lite reported connected",
    coverageChecked = "Coverage checked",
    fetching = "Fetching",
    aligning = "Checking the join",
    planning = "Planning",
    completed = "Fetch complete",
    preview = "Preview audio installed",
    paused = "Fetch paused",
    incomplete = "Some clips need attention",
    reportedProvider = "Last TTS System Used: {provider}",
    nameRealm = "{name} {realm}",
    coverageSummary = "Saved coverage · {area}: {ready} of {total} · checked {date}",
    readyCount = "{ready} of {total} ready",
    combatRetry = "When combat ends, click {action} again.",
    fetchTip = "Adds missing recordings for the selected character and area. Lite reuses saved recordings.",
    checkTip = "Counts saved name recordings. Checking coverage does not contact the speech provider.",
    pauseTip = "Stops the active fetch for this character. The recordings already saved remain available.",
    settingsTip = "Opens the desktop page where you choose a speech provider and area.",
    currentTip = "Current character follows the character you play. Select another character to prepare their recordings. Dialogue always uses the character you are playing.",
    refreshTip = "Reloads WoW to read Lite’s latest saved report, then reopens this panel.",
    displayTip = "Changes how progress appears while you play.",
    brand = "DMSpeaks Lite - WOW",
    minimapTooltip = "Open audio fetch settings",
    providerFish = "Fish Audio (free)",
    providerSsh = "Your own speech server (SSH)",
    providerLocalFish = "Shared local Fish",
    providerElevenLabs = "ElevenLabs",
    providerOpenAI = "OpenAI",
    providerAzure = "Azure Speech",
    providerGoogle = "Google Cloud Text-to-Speech",
    providerPolly = "Amazon Polly",
    findSetup = "Find the Lite installer and Read Me in your DMSpeaks download.",
    copySetup = "Copy the highlighted address into your web browser to download DMSpeaks Lite.",
    close = "Close",
    copyBrowser = "Copy this address into your browser. Fetch Settings selects this character.",
    welcome = "DMSpeaks personalizes quest and gossip audio. Open its settings to connect to Lite and fetch audio.",
    openSettings = "Open Settings",
    restoreHud = "Restore audio progress",
    minimizeHud = "Minimize audio progress",
    enterWorld = "Enter the world to register your character",
    retryAfterCombat = "When combat ends, try again.",
    reloadWithCompanion = "Open DMSpeaks Lite on your desktop, then reload WoW.",
    refreshAfterCombat = "When combat ends, click Check Lite Connection again.",
    copySettingsHint = "Copy this address if Fetch Settings does not open.",
    characterActions = "Check Coverage or Fetch Names for this character.",
    openingSettings = "Opening Fetch Settings",
    waitingForLite = "Waiting for Lite",
    dialogue = "Dialogue",
    stageOffer = "Quest offer",
    stageProgress = "Quest progress",
    stageTurnIn = "Quest turn-in",
    stageQuest = "Quest dialogue",
    stageGossip = "Gossip",
    stageFollowup = "Follow-up",
    working = "Working",
    openLite = "Open DMSpeaks Lite",
    etaUnknown = "unknown",
    audioReady = "Name audio ready",
    fetchingAudio = "Fetching audio",
    readingRequest = "Reading the request from WoW",
    preparingAudio = "Preparing name audio",
    chooseAction = "Choose Check Coverage or Fetch Names.",
    nextStart = "Next: Start DMSpeaks Lite, then open Fetch Settings.",
    nextProvider = "Next: Open Fetch Settings and save a provider.",
    nextRefresh = "Next: Check Lite Connection for the latest update.",
    nextCoverage = "Next: Check Coverage for this character.",
    nextFetch = "Next: Fetch Names when you are ready.",
    openThenRefresh = "Open DMSpeaks Lite, then click Check Lite Connection.",
    lookForBrowser = "Look for Fetch Settings in your browser.",
    pendingCoverage = "Lite is checking. Click Check Lite Connection after it finishes.",
    refreshLatest = "Click Check Lite Connection for the latest status.",
    setupInstructions = "Click Setup Help for installation steps.",
    setupSteps = "1. {download}\n2. Close WoW, run setup, then open DMSpeaks Lite - WOW from the desktop or launcher.\n3. Save and test your connection. Keep Lite open, return to WoW, and click Fetch Names.",
    opacityValue = "Overlay opacity {percent}%",
    characterTraits = "{name} · {race} {class}",
    npcActivity = "{npc} · {stage} · {phase}",
    etaSeconds = "{seconds} sec",
    dialogueNumber = "Dialogue {current} of {total}",
    npcNumber = "NPC {current} of {total} · {dialogue}",
    stepEta = "{progress} · Step ETA {eta}",
    fetchCounter = "Fetching {current} of {total}",
    reportedStale = "Last reported {time} · Check Lite Connection for the latest status",
    reportedRefresh = "Last reported {time} · Check Lite Connection to update",
    coverageDate = "%b %d %H:%M",
    shortTime = "%H:%M",
}
local locale = GetLocale and GetLocale() or "enUS"
local messages = addon.locales and addon.locales[locale] or {}
local function Text(key, values)
    local template = type(messages[key]) == "string" and messages[key] or english[key]
    if not values then return template end
    return (template:gsub("{(%w+)}", function(name)
        return values[name] ~= nil and tostring(values[name]) or "{" .. name .. "}"
    end))
end

local contentCollections = {
    {"alliance", "contentAlliance", "alliance-quests-framed", "DMSpeaksPersonalizedAlliance"},
    {"horde", "contentHorde", "horde-quests-framed", "DMSpeaksPersonalizedHorde"},
    {"shared", "contentShared", "shared", "DMSpeaksPersonalizedShared"},
    {"gossip", "contentGossip", "gossip-framed", "DMSpeaksPersonalizedGossip"},
    {"catalog", "contentCatalog", "dmspeaks-framed"},
}

local function BindPlaybackCheck(check, label, collection, roster, stamp)
    local selector = collection[4]
    local metadata = (C_AddOns and C_AddOns.GetAddOnMetadata) or GetAddOnMetadata
    local enable = (C_AddOns and C_AddOns.EnableAddOn) or EnableAddOn
    local disable = (C_AddOns and C_AddOns.DisableAddOn) or DisableAddOn
    local character = UnitName("player")
    local function Enabled()
        if C_AddOns and C_AddOns.GetAddOnEnableState then
            return C_AddOns.GetAddOnEnableState(selector, character) ~= 0
        end
        return not GetAddOnEnableState or GetAddOnEnableState(character, selector) ~= 0
    end
    local function Update()
        local known = metadata and metadata(selector, "X-DMSpeaks-Playback-Selector") == "1"
        local available = known and (not stamp or roster[collection[1]] ~= false)
        local enabled = available and Enabled()
        check:SetEnabled(available and enable and disable and true or false)
        check:SetChecked(enabled == true)
        local status = not available and "contentMissing" or enabled and "contentPresent" or "contentDisabled"
        label:SetText(Text("contentRow", {name = Text(collection[2]), status = Text(status)}))
    end
    check:SetScript("OnClick", function(self)
        if self:GetChecked() then enable(selector, character)
        else disable(selector, character) end
        Update()
    end)
    return Update
end

local function CreateInstalledContent(panel, resize)
    local body = CreateFrame("Frame", nil, panel)
    body:SetSize(484, 174)
    local rows = {}
    local lastChecked = body:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
    lastChecked:SetPoint("TOPLEFT", 0, -3)
    lastChecked:SetPoint("RIGHT", -6, 0)
    lastChecked:SetJustifyH("LEFT")
    local roster = type(DMSpeaksInstalledContent) == "table" and DMSpeaksInstalledContent or {}
    local timestamp = roster.checkedAt
    local stamp = type(timestamp) == "number" and timestamp > 0 and timestamp < 4102444800
    lastChecked:SetText(stamp and Text("contentChecked", {time = date(Text("coverageDate"), timestamp)})
        or Text("contentHint"))
    for _, collection in ipairs(contentCollections) do
        local icon = body:CreateTexture(nil, "ARTWORK")
        icon:SetSize(24, 24)
        icon:SetTexture("Interface\\AddOns\\DMSpeaksLite\\Icons\\" .. collection[3] .. ".tga")
        local label = body:CreateFontString(nil, "ARTWORK", "GameFontHighlight")
        local check, update
        if collection[4] then
            check = CreateFrame("CheckButton", nil, body, "UICheckButtonTemplate")
            check:SetSize(24, 24)
            check:SetPoint("LEFT", icon, "RIGHT", 5, 0)
            update = BindPlaybackCheck(check, label, collection, roster, stamp)
        end
        label:SetPoint("TOPLEFT", check or icon, "TOPRIGHT", 8, -3)
        label:SetPoint("RIGHT", body, "RIGHT", -6, 0)
        label:SetJustifyH("LEFT")
        local present = stamp and roster[collection[1]]
        local status = not stamp and "contentUnchecked" or present == true and "contentPresent"
            or present == false and "contentMissing" or "contentUnchecked"
        label:SetText(Text("contentRow", {name = Text(collection[2]), status = Text(status)}))
        rows[#rows + 1] = {icon = icon, label = label, update = update}
    end
    resize[#resize + 1] = function(width)
        body:SetWidth(math.max(1, width - 36))
        local height = math.max(14, lastChecked:GetStringHeight() or 0) + 13
        for _, row in ipairs(rows) do
            if row.update then row.update() end
            row.icon:ClearAllPoints()
            row.icon:SetPoint("TOPLEFT", body, "TOPLEFT", 0, -height)
            height = height + math.max(24, (row.label:GetStringHeight() or 0) + 3) + 4
        end
        body:SetHeight(height - 4)
    end
    return body
end

local function Explain(control, key)
    control:HookScript("OnEnter", function(self)
        if not GameTooltip then return end
        GameTooltip:SetOwner(self, "ANCHOR_RIGHT")
        GameTooltip:SetText(Text(key), 1, 1, 1, true)
        GameTooltip:Show()
    end)
    control:HookScript("OnLeave", function(self)
        if GameTooltip and GameTooltip:IsOwned(self) then GameTooltip:Hide() end
    end)
end

local runtimeSnapshot = DMSpeaksLiteRuntimeSnapshot
local legacyTime = addon.snapshot and addon.snapshot.generatedAt
if type(runtimeSnapshot) == "table" and runtimeSnapshot.schema == 1
    and type(runtimeSnapshot.generatedAt) == "number"
    and (type(legacyTime) ~= "number" or runtimeSnapshot.generatedAt >= legacyTime) then
    addon.snapshot = runtimeSnapshot
end
local providers = {
    { "fish-s21-free", Text("providerFish") },
    { "lite-fish_omni", Text("providerSsh") },
    { "fish-local", Text("providerLocalFish") },
    { "lite-elevenlabs", Text("providerElevenLabs") },
    { "lite-openai", Text("providerOpenAI") },
    { "lite-azure", Text("providerAzure") },
    { "lite-google", Text("providerGoogle") },
    { "lite-polly", Text("providerPolly") },
}
local providerLabels = {}
for _, choice in ipairs(providers) do providerLabels[choice[1]] = choice[2] end
local progressModes = {
    { "bar", Text("bar") },
    { "compact", Text("compact") },
    { "off", Text("hideProgress") },
}
local validProgressMode = { bar = true, compact = true, off = true }

local hudPositions = {
    { "BOTTOMRIGHT", Text("bottomRight"), -92, 104 },
    { "BOTTOMLEFT", Text("bottomLeft"), 22, 228 },
    { "TOPLEFT", Text("topLeft"), 22, -112 },
}
local hudPositionById = {}
for _, choice in ipairs(hudPositions) do hudPositionById[choice[1]] = choice end

local characterLabel, category, drawProgress
local minimap, minimapPosition
local lastSavedName, lastSavedRealm
local inWorld, retryPrompt

local function OpenSettings()
    if category then Settings.OpenToCategory(category:GetID()) end
end

local function MinimapTooltip(tooltip)
    tooltip:AddLine(Text("brand"))
    tooltip:AddLine(Text("minimapTooltip"), 1, 1, 1)
end

local function CreateMinimapButton(texture, angle, coords)
    if not Minimap then return end
    local button = CreateFrame("Button", "DMSpeaksMinimapButton", Minimap)
    button:SetSize(31, 31)
    button:SetFrameStrata("MEDIUM")
    button:SetFrameLevel(Minimap:GetFrameLevel() + 8)
    button:SetPoint("CENTER", Minimap, "CENTER",
        math.cos(angle) * Minimap:GetWidth() / 2, math.sin(angle) * Minimap:GetHeight() / 2)
    button:SetHighlightTexture(136477)
    local ring = button:CreateTexture(nil, "OVERLAY")
    ring:SetSize(53, 53)
    ring:SetPoint("TOPLEFT")
    ring:SetTexture(136430)
    local background = button:CreateTexture(nil, "BACKGROUND")
    background:SetSize(20, 20)
    background:SetPoint("TOPLEFT", 7, -5)
    background:SetTexture(136467)
    local face = button:CreateTexture(nil, "ARTWORK")
    face:SetSize(17, 17)
    face:SetPoint("TOPLEFT", 7, -6)
    face:SetTexture(texture)
    face:SetTexCoord(unpack(coords))
    button:SetScript("OnClick", OpenSettings)
    button:SetScript("OnEnter", function(self)
        GameTooltip:SetOwner(self, "ANCHOR_LEFT")
        MinimapTooltip(GameTooltip)
        GameTooltip:Show()
    end)
    button:SetScript("OnLeave", function() GameTooltip:Hide() end)
    return button
end

local function SetupMinimap()
    if minimap then return end
    local faction = UnitFactionGroup("player")
    if faction ~= "Alliance" and faction ~= "Horde" then return end
    local texture = "Interface\\AddOns\\DMSpeaksLite\\Icons\\minimap-waveform.tga"
    local coords = { 0, 1, 0, 1 }
    local angle = tonumber(DMSpeaksLiteSettings.minimapAngle) or 315
    local broker = LibStub and LibStub("LibDataBroker-1.1", true)
    local icon = LibStub and LibStub("LibDBIcon-1.0", true)
    if not broker or not icon then
        minimap = CreateMinimapButton(texture, math.rad(angle), coords)
        return
    end
    minimapPosition = { minimapPos = angle }
    minimap = broker:NewDataObject("DMSpeaks", {
        type = "launcher",
        icon = texture,
        iconCoords = coords,
        OnClick = OpenSettings,
        OnTooltipShow = MinimapTooltip,
    })
    icon:Register("DMSpeaks", minimap, minimapPosition)
end

local function CaptureCharacter()
    if not DMSpeaksLiteSettings then return end
    local name, realm = UnitFullName("player")
    if not realm or realm == "" then realm = GetRealmName() end
    local raceLabel, race, raceID = UnitRace("player")
    if raceID == 95 or raceID == 96 then race = "Skyborne" end
    local classLabel, class = UnitClass("player")
    if not name or name == "" or not realm or realm == "" or not race or not class then return end
    local sex = UnitSex("player")
    local saved = DMSpeaksLiteSettings
    saved.playerName, saved.playerRealm = name, realm
    local portal = GetCVar and GetCVar("portal")
    local regions = {us = true, eu = true, kr = true, tw = true}
    saved.playerRegion = type(portal) == "string" and regions[portal:lower()] and portal:lower() or nil
    saved.playerRace, saved.playerClass = race, class
    saved.playerRaceID = (raceID == 95 or raceID == 96) and raceID or nil
    local faction = UnitFactionGroup and UnitFactionGroup("player")
    saved.playerFaction = (faction == "Alliance" or faction == "Horde" or faction == "Neutral")
        and faction or nil
    local level = type(UnitLevel) == "function" and UnitLevel("player")
    saved.playerLevel = type(level) == "number" and level >= 1 and level == math.floor(level) and level or nil
    saved.playerSex = sex == 2 and "male" or sex == 3 and "female" or ""
    if characterLabel then
        characterLabel:SetText(Text("characterTraits", {name = name, race = raceLabel or race, class = classLabel or class}))
    end
    if drawProgress then drawProgress() end
    return true
end

local function SyncCharacter()
    if InCombatLockdown and InCombatLockdown() then
        return Text("combatRetry", {action = Text("syncCharacter")})
    end
    if not CaptureCharacter() then return Text("enterWorld") end
    ReloadUI()
end

local syncDialog = "DMSPEAKS_LITE_NEW_CHARACTER_SYNC"
if StaticPopupDialogs then
    StaticPopupDialogs[syncDialog] = {
        text = Text("newCharacterSync", {name = "%s"}),
        button1 = Text("reload"), button2 = Text("later"),
        timeout = 0, whileDead = true, hideOnEscape = true,
        OnAccept = function()
            -- Combat may have started while the prompt was on screen.
            if SyncCharacter() then DMSpeaksLiteSyncPromptSeen = nil end
        end,
    }
end

local function CheckLoginCharacter()
    if not inWorld then return end
    local captured = CaptureCharacter()
    if captured then SetupMinimap() end
    if DMSpeaksLiteSyncPromptSeen == true or not StaticPopup_Show
        or (InCombatLockdown and InCombatLockdown()) then return end
    if captured then
        local name, realm = DMSpeaksLiteSettings.playerName, DMSpeaksLiteSettings.playerRealm
        local known = name == lastSavedName and realm == lastSavedRealm
        for _, profile in ipairs(DMSpeaksLiteRoster or {}) do
            if profile.name == name and profile.realm == realm then known = true; break end
        end
        if known or StaticPopup_Show(syncDialog, Text("nameRealm", {name = name, realm = realm})) then
            DMSpeaksLiteSyncPromptSeen = true
            return
        end
    end
    -- Character details can arrive late, and WoW can have all dialog slots in use.
    if not retryPrompt and C_Timer and C_Timer.After then
        retryPrompt = true
        C_Timer.After(2, function()
            retryPrompt = false
            if DMSpeaksLiteSyncPromptSeen ~= true then CheckLoginCharacter() end
        end)
    end
end

local function CreateSpinner(parent)
    local spinner = CreateFrame("Frame", nil, parent)
    spinner:SetSize(9, 9)
    local dots = {}
    for _, point in ipairs({ "TOP", "RIGHT", "BOTTOM", "LEFT" }) do
        local dot = spinner:CreateTexture(nil, "ARTWORK")
        dot:SetPoint("CENTER", spinner, point)
        dot:SetSize(2, 2)
        dot:SetColorTexture(1, 1, 1, 1)
        dots[#dots + 1] = dot
    end
    local elapsedTime, activeDot = 0, 0
    spinner:SetScript("OnUpdate", function(_, elapsed)
        elapsedTime = elapsedTime + elapsed
        if elapsedTime < .14 then return end
        elapsedTime = elapsedTime % .14
        activeDot = (activeDot % #dots) + 1
        for index, dot in ipairs(dots) do dot:SetAlpha(index == activeDot and 1 or .28) end
    end)
    spinner:Hide()
    return spinner
end

local function CreateProgressBar(parent)
    local meter = CreateFrame("StatusBar", nil, parent)
    meter:SetHeight(4)
    local track = meter:CreateTexture(nil, "BACKGROUND")
    track:SetAllPoints()
    track:SetColorTexture(.23, .22, .20, 1)
    meter:SetStatusBarTexture("Interface\\TargetingFrame\\UI-StatusBar")
    meter:SetStatusBarColor(.61, .57, .48)
    return meter
end

local function CreateGameplayProgress(panel, anchor, prefs, resize)
    local hud = CreateFrame("Frame", nil, UIParent)
    hud:SetFrameStrata("LOW")
    hud:SetClampedToScreen(true)
    hud:EnableMouse(false)
    hud:Hide()
    local background = hud:CreateTexture(nil, "BACKGROUND")
    background:SetAllPoints()
    background:SetColorTexture(.08, .075, .068, .93)
    local spinner = CreateSpinner(hud)
    spinner:SetPoint("TOPLEFT", hud, "TOPLEFT", 9, -8)
    local phase = hud:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
    phase:SetPoint("TOPLEFT", hud, "TOPLEFT", 24, -7)
    phase:SetJustifyH("LEFT")
    local minimize = CreateFrame("Button", nil, hud, "UIPanelButtonTemplate")
    minimize:SetSize(19, 17)
    minimize:SetPoint("TOPRIGHT", hud, "TOPRIGHT", -4, -4)
    phase:SetPoint("RIGHT", minimize, "LEFT", -4, 0)
    minimize:SetScript("OnEnter", function(self)
        if not GameTooltip then return end
        GameTooltip:SetOwner(self, "ANCHOR_TOP")
        GameTooltip:SetText(prefs.hudMinimized and Text("restoreHud") or Text("minimizeHud"))
        GameTooltip:Show()
    end)
    minimize:SetScript("OnLeave", function() if GameTooltip then GameTooltip:Hide() end end)
    local line = hud:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
    line:SetPoint("TOPLEFT", hud, "TOPLEFT", 9, -24)
    line:SetPoint("RIGHT", hud, "RIGHT", -9, 0)
    line:SetJustifyH("LEFT")
    line:SetWordWrap(false)
    local meter = CreateProgressBar(hud)
    meter:SetPoint("TOPLEFT", hud, "TOPLEFT", 9, -40)
    meter:SetPoint("RIGHT", hud, "RIGHT", -9, 0)
    local count = hud:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
    count:SetPoint("TOPLEFT", meter, "BOTTOMLEFT", 0, -4)

    local function Layout()
        local compact = prefs.progressMode == "compact"
        hud:SetSize((compact or prefs.hudMinimized) and 146 or 244, (compact or prefs.hudMinimized) and 24 or 65)
        if compact then minimize:Hide() else minimize:Show() end
        minimize:SetText(prefs.hudMinimized and "+" or "-")
        if compact or prefs.hudMinimized then
            line:Hide()
            meter:Hide()
            count:Hide()
        else
            line:Show()
            meter:Show()
            count:Show()
        end
    end
    minimize:SetScript("OnClick", function()
        prefs.hudMinimized = not prefs.hudMinimized
        Layout()
    end)
    local function Position()
        local corner = hudPositionById[prefs.hudCorner]
        hud:ClearAllPoints()
        hud:SetPoint(corner[1], UIParent, corner[1], corner[3], corner[4])
    end
    Layout()
    Position()
    hud:SetAlpha(prefs.hudAlpha / 100)

    local positionLabel = panel:CreateFontString(nil, "ARTWORK", "GameFontNormalSmall")
    positionLabel:SetPoint("TOPLEFT", anchor, "BOTTOMLEFT", 0, -12)
    positionLabel:SetText(Text("position"))
    local position = CreateFrame("DropdownButton", nil, panel, "WowStyle1DropdownTemplate")
    position:SetPoint("TOPLEFT", positionLabel, "BOTTOMLEFT", 0, -6)
    position:SetWidth(155)
    position:SetupMenu(function(_, root)
        for _, choice in ipairs(hudPositions) do
            local id, label = choice[1], choice[2]
            root:CreateRadio(label, function() return prefs.hudCorner == id end, function()
                prefs.hudCorner = id
                Position()
            end)
        end
    end)
    local slider = CreateFrame("Slider", nil, panel)
    slider:SetPoint("TOPLEFT", position, "BOTTOMLEFT", 0, -12)
    slider:SetOrientation("HORIZONTAL")
    slider:SetSize(118, 14)
    slider:SetMinMaxValues(25, 100)
    slider:SetValueStep(5)
    slider:SetThumbTexture("Interface\\Buttons\\UI-SliderBar-Button-Horizontal")
    local sliderTrack = slider:CreateTexture(nil, "BACKGROUND")
    sliderTrack:SetPoint("LEFT", slider, "LEFT", 3, 0)
    sliderTrack:SetPoint("RIGHT", slider, "RIGHT", -3, 0)
    sliderTrack:SetHeight(3)
    sliderTrack:SetColorTexture(.37, .34, .29, 1)
    slider:SetValue(prefs.hudAlpha)
    local label = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlightSmall")
    label:SetPoint("TOPLEFT", slider, "BOTTOMLEFT", 0, -5)
    label:SetJustifyH("LEFT")
    resize[#resize + 1] = function(width)
        local available = math.max(1, width - 36)
        positionLabel:SetWidth(available)
        positionLabel:SetJustifyH("LEFT")
        position:SetWidth(math.min(155, available))
        slider:SetWidth(math.min(118, available))
        label:SetWidth(available)
    end
    label:SetText(Text("opacityValue", {percent = prefs.hudAlpha}))
    slider:SetScript("OnValueChanged", function(_, value)
        prefs.hudAlpha = math.floor(value + .5)
        hud:SetAlpha(prefs.hudAlpha / 100)
        label:SetText(Text("opacityValue", {percent = prefs.hudAlpha}))
    end)

    return function(visible, working, title, dialogue, barValue, barTotal, detail)
        if not visible then
            spinner:Hide()
            hud:Hide()
            return
        end
        Layout()
        phase:SetText(prefs.progressMode == "compact" and detail ~= "" and detail or title)
        line:SetText(dialogue)
        meter:SetMinMaxValues(0, math.max(barTotal, 1))
        meter:SetValue(barValue)
        count:SetText(detail)
        if working then spinner:Show() else spinner:Hide() end
        hud:Show()
    end, label
end

local function Initialize()
    local saved = type(DMSpeaksLiteSettings) == "table" and DMSpeaksLiteSettings or {}
    lastSavedName, lastSavedRealm = saved.playerName, saved.playerRealm
    DMSpeaksLiteSettings = {
        schema = 2, provider = providerLabels[saved.provider] and saved.provider or providers[1][1],
        progressMode = validProgressMode[saved.progressMode] and saved.progressMode
            or (saved.showProgress == false and "off" or "bar"),
        showProgress = validProgressMode[saved.progressMode] and saved.progressMode ~= "off"
            or not validProgressMode[saved.progressMode] and saved.showProgress ~= false,
        hudCorner = hudPositionById[saved.hudCorner] and saved.hudCorner or "BOTTOMRIGHT",
        hudAlpha = type(saved.hudAlpha) == "number" and saved.hudAlpha >= 25 and saved.hudAlpha <= 100
            and math.floor(saved.hudAlpha + .5) or 85,
        hudMinimized = saved.hudMinimized == true,
        fetchCoverage = "all",
        starterOnly = false,
        setupPromptSeen = saved.setupPromptSeen == true,
        minimapAngle = type(saved.minimapAngle) == "string" and saved.minimapAngle:match("^%d%d?%d?$")
            or nil,
    }
    local settingsPanel = CreateFrame("Frame")
    settingsPanel.name = Text("brand")
    local panelBackground = settingsPanel:CreateTexture(nil, "BACKGROUND")
    panelBackground:SetAllPoints()
    panelBackground:SetColorTexture(.035, .03, .025, .96)
    local scroll = CreateFrame("ScrollFrame", nil, settingsPanel, "UIPanelScrollFrameTemplate")
    scroll:SetPoint("TOPLEFT", settingsPanel, "TOPLEFT", 0, 0)
    scroll:SetPoint("BOTTOMRIGHT", settingsPanel, "BOTTOMRIGHT", -26, 0)
    local panel = CreateFrame("Frame", nil, scroll)
    panel:SetSize(520, 760)
    scroll:SetScrollChild(panel)
    local resize = {}
    local function Resize(_, width)
        if width and width > 0 then
            panel:SetWidth(width)
            for _, callback in ipairs(resize) do callback(width) end
        end
    end
    scroll:SetScript("OnSizeChanged", Resize)
    scroll:EnableMouseWheel(true)
    scroll:SetScript("OnMouseWheel", function(self, delta)
        self:SetVerticalScroll(math.max(0, math.min(self:GetVerticalScrollRange(), self:GetVerticalScroll() - delta * 35)))
    end)
    local title = panel:CreateFontString(nil, "ARTWORK", "GameFontNormalLarge")
    title:SetPoint("TOPLEFT", 16, -16)
    title:SetText(settingsPanel.name)
    characterLabel = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlight")
    characterLabel:SetPoint("TOPLEFT", title, "BOTTOMLEFT", 0, -10)
    characterLabel:SetText(Text("enterWorld"))
    CaptureCharacter()
    local desktopLabel = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlight")
    desktopLabel:SetPoint("TOPLEFT", characterLabel, "BOTTOMLEFT", 0, -10)
    desktopLabel:SetJustifyH("LEFT")
    desktopLabel:SetText(Text("desktopControls"))
    local syncButton = CreateFrame("Button", nil, panel, "UIPanelButtonTemplate")
    syncButton:SetSize(220, 24)
    syncButton:SetPoint("TOPLEFT", desktopLabel, "BOTTOMLEFT", 0, -10)
    syncButton:SetText(Text("syncCharacter"))
    local syncHint = panel:CreateFontString(nil, "ARTWORK", "GameFontHighlight")
    syncHint:SetPoint("TOPLEFT", syncButton, "BOTTOMLEFT", 0, -5)
    syncHint:SetJustifyH("LEFT")
    syncHint:SetText(Text("syncCharacterHint"))
    syncButton:SetScript("OnClick", function()
        local hint = SyncCharacter()
        if hint then syncHint:SetText(hint) end
    end)
    resize[#resize + 1] = function(width)
        local available = math.max(1, width - 36)
        desktopLabel:SetWidth(available)
        syncButton:SetWidth(math.min(220, available))
        syncHint:SetWidth(available)
    end
    local displayBody = CreateFrame("Frame", nil, panel)
    displayBody:SetSize(484, 1)
    local prefs = DMSpeaksLiteSettings
    local progressLabel = displayBody:CreateFontString(nil, "ARTWORK", "GameFontNormal")
    progressLabel:SetPoint("TOPLEFT", displayBody, "TOPLEFT", 0, -4)
    progressLabel:SetText(Text("progress"))
    local progress = CreateFrame("DropdownButton", nil, displayBody, "WowStyle1DropdownTemplate")
    progress:SetPoint("TOPLEFT", progressLabel, "BOTTOMLEFT", 0, -5)
    progress:SetWidth(134)
    progress:SetupMenu(function(_, root)
        for _, choice in ipairs(progressModes) do
            local id, label = choice[1], choice[2]
            root:CreateRadio(label, function() return prefs.progressMode == id end, function()
                prefs.progressMode = id
                prefs.showProgress = id ~= "off"
                if drawProgress then drawProgress() end
            end)
        end
    end)
    Explain(progress, "displayTip")

    local function AddSection(key, previous, body)
        local heading = panel:CreateFontString(nil, "ARTWORK", "GameFontNormal")
        heading:SetText(Text(key))
        heading:SetJustifyH("LEFT")
        heading:SetPoint("TOPLEFT", previous, "BOTTOMLEFT", 0, -17)
        heading:SetPoint("RIGHT", panel, "RIGHT", -20, 0)
        local divider = panel:CreateTexture(nil, "BACKGROUND")
        divider:SetPoint("TOPLEFT", heading, "BOTTOMLEFT", 0, -4)
        divider:SetPoint("RIGHT", heading, "RIGHT", 0, 0)
        divider:SetHeight(1)
        divider:SetColorTexture(.50, .39, .23, .45)
        body:SetPoint("TOPLEFT", divider, "BOTTOMLEFT", 0, -7)
    end

    resize[#resize + 1] = function(width)
        local available = math.max(1, width - 36)
        displayBody:SetWidth(available)
        progressLabel:SetWidth(available)
        progressLabel:SetJustifyH("LEFT")
        progress:SetWidth(math.min(134, available))
    end
    AddSection("displayHeading", syncHint, displayBody)
    local ShowGameplayProgress, displayLast = CreateGameplayProgress(displayBody, progress, prefs, resize)
    local contentBody = CreateInstalledContent(panel, resize)
    AddSection("contentHeading", displayLast, contentBody)
    local function FitScroll()
        local top, bottom = panel:GetTop(), contentBody:GetBottom()
        if top and bottom then panel:SetHeight(math.max(1, math.ceil(top - bottom) + 16)) end
    end
    resize[#resize + 1] = FitScroll

    local expiryScheduled = false
    local function Draw()
        local snapshot = addon.snapshot or {}
        local active = snapshot.state == "planning" or snapshot.state == "fetching" or snapshot.state == "aligning"
        local elapsed = type(snapshot.generatedAt) == "number" and time() - snapshot.generatedAt
        local visible = prefs.progressMode ~= "off" and active and elapsed and elapsed >= 0
            and elapsed < 30 and snapshot.liveHud ~= 1
        if visible and not expiryScheduled then
            expiryScheduled = true
            C_Timer.After(30 - elapsed, function()
                expiryScheduled = false
                Draw()
            end)
        end
        local ready, total = snapshot.ready or 0, snapshot.total or 0
        local progressValue = type(snapshot.instanceProgress) == "number" and snapshot.instanceProgress
        ShowGameplayProgress(visible, visible, snapshot.playerName or Text("brand"), snapshot.npc or "",
            progressValue or ready, progressValue and 100 or total, snapshot.detail or "")
        Resize(scroll, scroll:GetWidth())
    end
    drawProgress = Draw
    panel:SetScript("OnShow", Draw)
    category = Settings.RegisterCanvasLayoutCategory(settingsPanel, settingsPanel.name)
    Settings.RegisterAddOnCategory(category)
    Draw()
end

local frame = CreateFrame("Frame")
frame:RegisterEvent("ADDON_LOADED")
frame:RegisterEvent("PLAYER_LOGIN")
frame:RegisterEvent("PLAYER_ENTERING_WORLD")
frame:RegisterEvent("PLAYER_REGEN_ENABLED")
frame:RegisterEvent("PLAYER_LOGOUT")
frame:SetScript("OnEvent", function(self, event, name)
    if event == "ADDON_LOADED" then
        if name ~= "DMSpeaksLite" then return end
        self:UnregisterEvent("ADDON_LOADED")
        Initialize()
    elseif event == "PLAYER_ENTERING_WORLD" then
        inWorld = true
        CheckLoginCharacter()
    elseif event == "PLAYER_REGEN_ENABLED" then
        if DMSpeaksLiteSyncPromptSeen ~= true then CheckLoginCharacter() end
    else
        if event == "PLAYER_LOGOUT" then inWorld = false end
        CaptureCharacter()
        if event == "PLAYER_LOGOUT" and minimapPosition then
            DMSpeaksLiteSettings.minimapAngle = tostring(math.floor(minimapPosition.minimapPos + .5) % 360)
        end
    end
end)
