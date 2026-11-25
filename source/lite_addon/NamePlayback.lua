-- Play a dialogue's manifest clips as one item in Spoken's quest queue.
local addonName, addon = ...
local core = SpokenEnv
local quests = VoiceOver

local function SupportedSpokenVersion(version)
    return version == "3.8.7" or version == "3.9.0" or version == "3.10.0" or version == "3.10.1"
end

local function HasMethods(owner, ...)
    if type(owner) ~= "table" then return false end
    for index = 1, select("#", ...) do
        local method = select(index, ...)
        if type(owner[method]) ~= "function" then return false end
    end
    return true
end

if type(core) ~= "table" or type(quests) ~= "table"
    or not SupportedSpokenVersion(core.AddonVersion) or quests.AddonVersion ~= core.AddonVersion
    or not HasMethods(quests.DataModules, "ResolveSoundFile", "PrepareSound")
    or not HasMethods(core.SoundUtils, "TestSound", "PlaySound", "StopSound")
    or not HasMethods(core.SoundQueue, "PauseQueue")
    or not HasMethods(Spoken, "NotSecret", "RegisterCallback")
    or not HasMethods(C_Timer, "NewTimer") or type(GetTime) ~= "function"
    or type(addon) ~= "table" or type(addon.dialoguesByStockPath) ~= "table" then
    return
end

local registry = addon.dialoguesByStockPath
local questVariants = addon.questVariantsByStockPath or {}
local followupVariants = addon.followupVariantsByStockPath or {}
local followupsByBroadcast = addon.followupVariantsByBroadcast or {}
local standaloneVariants = addon.questVariantsWithoutStock or {}
local legacyNamedQuests = addon.legacyNamedQuestVariants or {}
local legacyNamedFollowups = addon.legacyNamedFollowupVariants or {}
local retainedStoreNames = addon.retainedStoreNames or {}
local gossipByGiver = addon.gossipByGiver or {}
local standalone = addon.dialoguesWithoutStock or {}
local standaloneModule = { METADATA = { AddonName = addonName, Language = "enUS" } }
local sounds = core.SoundUtils
local queue = core.SoundQueue
local modules = quests.DataModules
local resolveStock = modules.ResolveSoundFile
local prepareStock = modules.PrepareSound
local testStock = sounds.TestSound
local playStock = sounds.PlaySound
local stopStock = sounds.StopSound
local basePath = "Interface\\AddOns\\"
local prepared = setmetatable({}, { __mode = "k" })
local playing = setmetatable({}, { __mode = "k" })
local events = quests.Enums and quests.Enums.SoundEvent
local enqueue = type(quests.Player) == "table" and quests.Player.EnqueuePrepared
local hookEnqueue = events and type(enqueue) == "function"

-- Lite refreshes these silent Oggs only after reading the selected library.
-- Probe on the normal sound path so a cached name cannot mask an offline share.
local sharePath = basePath .. "DMSpeaksLiteStatus\\SharePlayback\\"
local function ShareUnavailable()
    if not testStock(sounds, { path = sharePath .. "shared.ogg" }, "Master") then return false end
    local now = time or GetServerTime
    if not now then return true end
    local slot = math.floor(now() / 15)
    return not testStock(sounds, { path = sharePath .. "ready-" .. slot .. ".ogg" }, "Master")
end

local function HasNameSlot(entry)
    for _, part in ipairs(entry.sequence or {}) do
        if part.kind == "name" then return true end
    end
    return entry.byName ~= nil
end

-- Match Spoken's current-character rule. A missing or load-on-demand pack
-- does not mean the player turned it off.
local getAddonInfo = quests.GetAddOnInfo or GetAddOnInfo or (C_AddOns and C_AddOns.GetAddOnInfo)
local getAddonMetadata = quests.GetAddOnMetadata or GetAddOnMetadata or (C_AddOns and C_AddOns.GetAddOnMetadata)

local function SwitchedOff(pack)
    if not pack or not getAddonInfo then return false end
    local _, _, _, loadable, reason = getAddonInfo(pack)
    return not loadable and reason == "DISABLED"
end

local function PackFromPath(path)
    if type(path) ~= "string" then return end
    return path:match("^Interface[\\/]AddOns[\\/]([^\\/]+)[\\/]")
        or path:match("^([^\\/]+)[\\/]")
end

local collections = {
    DMSpeaksSpokenAlliance = "Alliance",
    DMSpeaksSpokenHorde = "Horde",
    DMSpeaksSpokenShared = "Shared",
    DMSpeaksSpokenGossip = "Gossip",
}

local function LegacyOwner(entry)
    local collection
    local function ReadPart(clip)
        local owner = clip and collections[PackFromPath(clip.path)]
        if not owner then return true end
        if collection and collection ~= owner then return false end
        collection = owner
        return true
    end
    for _, part in ipairs(entry.sequence or {}) do
        if not ReadPart(part.fixed) then return end
        for _, clip in pairs(part.choices or {}) do
            if not ReadPart(clip) or not ReadPart(clip.prefix) or not ReadPart(clip.suffix) then return end
        end
    end
    if collection then return collection, PackFromPath(entry.stockPath or entry.stock) end
end

local function PlaybackAllowed(clip, entry)
    if not entry then return false end
    local collection, original
    if addon.playbackPackOwners then
        local owned = addon.playbackPackOwners[entry.storeRevision]
        if not owned then return false end
        collection, original = owned[1], owned[2]
    else
        -- Old generated collections have no owner table. Preserve their
        -- behavior when no historic owner can be identified.
        collection, original = LegacyOwner(entry)
    end
    if collection then
        local selector = "DMSpeaksPersonalized" .. collection
        if getAddonMetadata and getAddonMetadata(selector, "X-DMSpeaks-Playback-Selector") == "1"
            and SwitchedOff(selector) then return false end
        if SwitchedOff(original or "SpokenQuestsAudio" .. collection) then return false end
    end
    -- The resolver may have chosen a different recording from another pack.
    -- Respect that pack as well, while leaving Spoken's own choice intact.
    local module = clip.module
    if module == standaloneModule then return true end
    local function ResolvedPackAllows(pack)
        return not pack or pack:match("^DMSpeaks") or not SwitchedOff(pack)
    end
    return ResolvedPackAllows(module and module.METADATA and module.METADATA.AddonName)
        and ResolvedPackAllows(PackFromPath(clip.filePath))
end

local function PlayerName()
    local name = Spoken.NotSecret(UnitName("player"))
    return type(name) == "string" and name:match("^%s*(%S+)") or ""
end

local function PlayerNameKey()
    local key = PlayerName():lower()
    return addon.nameAliases and addon.nameAliases[key] or key
end

local function PlayerClass()
    local _, token = UnitClass("player")
    token = Spoken.NotSecret(token)
    return type(token) == "string" and token or ""
end

local raceNames = {
    nightelf = "night elf",
    bloodelf = "blood elf",
    scourge = "undead",
}

local skyborneByID = { [95] = "high order skyborne", [96] = "windshaper skyborne" }
local skyborneByToken = {
    highorderskyborne = "high order skyborne", skybornealliance = "high order skyborne",
    windshaperskyborne = "windshaper skyborne", skybornehorde = "windshaper skyborne",
}

-- Both factions use one spoken word. Route restrictions can name an individual faction variant.
local function PlayerRace()
    local label, token, id = UnitRace("player")
    local variant = skyborneByID[Spoken.NotSecret(id)]
    token = Spoken.NotSecret(token) or Spoken.NotSecret(label)
    token = type(token) == "string" and token:lower() or ""
    local compact = token:gsub("[%s_%-]", "")
    variant = variant or skyborneByToken[compact]
    if variant or compact == "skyborne" then
        if not variant and type(UnitFactionGroup) == "function" then
            local faction = Spoken.NotSecret(UnitFactionGroup("player"))
            if faction == "Alliance" then variant = skyborneByID[95]
            elseif faction == "Horde" then variant = skyborneByID[96] end
        end
        return "skyborne", variant or "skyborne"
    end
    local race = raceNames[compact] or token
    return race, race
end

local legacyMarkers = { name = "$n", class = "$c", race = "$r" }

-- Match a played infill to its stock caption word on the same source clock.
local function InfillCaption(text, timings, clips, rate)
    if type(text) ~= "string" then return text end
    if type(timings) == "string" then
        local words = {}
        for value in timings:gmatch("%S+") do words[#words + 1] = tonumber(value) end
        timings = words
    end
    if type(timings) ~= "table" then return text end

    local words, count, piece = {}, 0, 1
    local previous
    for first, word, after in text:gmatch("()(%S+)()") do
        count = count + 1
        local seconds = timings[count]
        if type(seconds) ~= "number" then return text end
        local sample = seconds * rate
        while piece < #clips and sample >= clips[piece].last do piece = piece + 1 end
        local clip = clips[piece]
        if type(clip.captionFallback) == "string" and sample >= clip.first and sample < clip.last then
            local prefix, stock, suffix = word:match("^(%p-)(%$?%a+)(.*)$")
            local replacement
            local spoken = clip.captionValue or clip.captionFallback
            if clip.captionKind == "gender" then
                local lead, male, female, tail = word:match("^(%p-)%$[gG]([^:;]+):([^:;]+);(%p*)$")
                if male and (male:lower() == spoken:lower() or female:lower() == spoken:lower()) then
                    prefix, replacement, suffix = lead, male:lower() == spoken:lower() and male or female, tail
                end
            end
            if not replacement and stock and (suffix:match("^%p*$") or suffix:match("^'[sS]%p*$")
                or suffix:match("^’[sS]%p*$")) then
                if stock:lower() == legacyMarkers[clip.captionKind] then
                    replacement = spoken
                    if stock:sub(2):match("^%u") then replacement = replacement:gsub("^%l", string.upper) end
                elseif stock:lower() == clip.captionFallback:lower() then
                    replacement = clip.captionValue
                    if replacement and stock:match("^%u") then replacement = replacement:gsub("^%l", string.upper) end
                end
            end
            if replacement then
                if clip.captionValue and clip.articleFrom and previous and previous.piece == piece
                    and text:sub(previous.after, first - 1):match("^%s+$") then
                    local startPunctuation, article, endPunctuation = previous.word:match("^(%p*)([Aa][Nn]?)(%p*)$")
                    if article and article:lower() == clip.articleFrom:lower() then
                        local replacement = clip.articleTo:lower()
                        if article:match("^[A-Z]") then replacement = replacement:gsub("^%l", string.upper) end
                        words[#words + 1] = { previous.first, previous.after,
                                              startPunctuation .. replacement .. endPunctuation }
                    end
                end
                words[#words + 1] = { first, after, prefix .. replacement .. suffix }
            end
        end
        previous = { first = first, word = word, after = after, piece = piece }
    end
    if count ~= #timings then return text end
    for index = #words, 1, -1 do
        local word = words[index]
        text = text:sub(1, word[1] - 1) .. word[3] .. text:sub(word[2])
    end
    return text
end

local function NameForCaption(path, name)
    if not name or not name:match("^[A-Za-z]+$") or name:lower() == "adventurer" then return end
    local file = path:match("([^\\]+)$")
    local value = file and file:match("%-name%-([a-z]+)%-%d+[%.%-]")
    if value == name:lower() then return name end
end

-- Typed deliveries seal lexical occurrences separately from audio ownership.
-- Multiword choices expand the caption clock by interpolation on the native
-- word interval; these are caption estimates, not newly aligned phonemes.
local function TypedCaption(text, timings, clips, rate)
    if type(text) ~= "string" or type(timings) ~= "table" then return text, timings end
    local tokens, positions = {}, {}
    for first, word, after in text:gmatch("()(%S+)()") do
        tokens[#tokens + 1] = word
        positions[#positions + 1] = { first, after }
    end
    if #tokens ~= #timings then return text, timings end
    local edits = {}
    for _, clip in ipairs(clips) do
        local binding = clip.captionBinding
        if binding then edits[binding.first] = clip end
    end
    local output, starts, measured, index, cursor = {}, {}, {}, 1, 1
    while index <= #tokens do
        local edit = edits[index]
        if edit then
            local binding = edit.captionBinding
            local original = table.concat(tokens, " ", binding.first, binding.last)
            if original ~= binding.source or type(edit.captionText) ~= "string" then return text, timings end
            local replacement = {}
            for word in edit.captionText:gmatch("%S+") do replacement[#replacement + 1] = word end
            output[#output + 1] = text:sub(cursor, positions[binding.first][1] - 1)
            output[#output + 1] = edit.captionText
            cursor = positions[binding.last][2]
            local first = timings[index]
            local last = timings[binding.last + 1] or clips[#clips].last / rate
            for offset = 1, #replacement do
                starts[#starts + 1] = #replacement == binding.last - binding.first + 1
                    and timings[binding.first + offset - 1]
                    or first + (last - first) * (offset - 1) / #replacement
                if edit.captionStarts then
                    measured[#starts] = edit.start + edit.captionStarts[offset]
                end
            end
            index = binding.last + 1
        else
            starts[#starts + 1] = timings[index]
            index = index + 1
        end
    end
    output[#output + 1] = text:sub(cursor)
    return table.concat(output), starts, next(measured) and measured
end

local function NormalizeQuestText(text)
    return (text:gsub("%s+", " "):match("^%s*(.-)%s*$"))
end

local function WorldStateValues(shown, entry)
    if not addon.MatchWorldState or type(shown) ~= "string"
        or not quests.Utils or not quests.Utils.FillPlayerTokens then return end
    return addon.MatchWorldState(shown, quests.Utils:FillPlayerTokens(entry.worldStateText))
end

local function MatchesDialogueText(shown, entry)
    if entry.worldStateText then return WorldStateValues(shown, entry) ~= nil end
    return shown == NormalizeQuestText(entry.text)
        or shown == NormalizeQuestText(quests.Utils:FillPlayerTokens(entry.text))
end

local function FetchedName(part, key)
    if part.kind ~= "name" or key == "" or key == part.fallback or not part.nameSlot
        or not DMSpeaksFetchedNames then return end
    local names = DMSpeaksFetchedNames[part.nameSlot]
    return names and names[key]
end

local function HasFetchedName(sequence, key, complete, blocked)
    local found = false
    for index, part in ipairs(sequence) do
        if (not blocked or not blocked[index]) and FetchedName(part, key) then
            if not complete then return true end
            found = true
        elseif complete and part.kind == "name" then
            return false
        end
    end
    return found
end

local function BuildSequence(entry, clip, fallback, skipRepair, offline)
    local clips, samples = {}, 0
    local worldState = entry.worldStateText and WorldStateValues(Spoken.NotSecret(clip.text), entry)
    local skipNamed = offline or fallback == true or
        (entry.byName and type(fallback) == "table" and fallback.name)
    local choices = { name = skipNamed and "" or PlayerNameKey(),
                      class = PlayerClass():lower(), race = PlayerRace() }
    local sex = Spoken.NotSecret(UnitSex("player"))
    local named = entry.byName and entry.byName[choices.name]
    local blockedNames = type(fallback) == "table" and fallback or nil
    local repair = not named and not skipRepair and not HasFetchedName(entry.sequence, choices.name, false, blockedNames)
        and (entry.playbackRepairByClass
        and entry.playbackRepairByClass[choices.class] or entry.playbackRepair)
    if repair and ((repair.class and repair.class ~= choices.class)
            or (repair.race and repair.race ~= choices.race)
            or (repair.sex and repair.sex ~= sex)) then repair = nil end
    local originalCuts
    if not named and not repair and not skipRepair and addon.originalRecordingCuts then
        originalCuts = addon.originalRecordingCuts[entry.storeRevision]
    end
    local sequence = named and (named.sequence or named) or entry.sequence
    for index, part in ipairs(sequence) do
        part = repair and repair.parts and repair.parts[index] or part
        local clip = part.fixed
        local captionValue
        local personalizedName
        local fallbackSlot
        local captionText
        local captionStarts
        local originalCut
        if not clip then
            local value = choices[part.kind]
            if part.kind == "gender" then
                value = part.bySex[sex]
            elseif part.kind == "world_state" then
                value = worldState and worldState[part.worldStateID]
            end
            local fallbackClip = part.choices[part.fallback]
            if type(fallback) == "table" and fallback[index] then value = part.fallback end
            clip = part.choices[value]
            local fetched = FetchedName(part, value)
            if fetched then clip = fetched end
            if not clip and part.kind ~= "world_state" then
                value, clip = part.fallback, fallbackClip
            end
            if fetched then
                captionText, captionStarts = fetched.caption, fetched.starts
            elseif clip and part.caption then
                captionText = part.caption.choices[value]
                captionStarts = part.caption.deliveredStarts and part.caption.deliveredStarts[value]
            end
            if clip and fallbackClip and clip ~= fallbackClip then fallbackSlot = index end
            if part.kind == "name" and clip and clip ~= fallbackClip then
                personalizedName = true
                captionValue = PlayerName()
            elseif named and clip and part.kind == "name" then
                -- Older per-character routes can declare the player take as their default.
                captionValue = NameForCaption(clip.path, PlayerName())
            elseif clip and captionText == nil and part.kind ~= "name" and value ~= part.fallback
                and type(value) == "string" then
                captionValue = value
            end
            local alternatives = originalCuts and originalCuts[index]
            if alternatives and clip and not fetched
                and not (blockedNames and blockedNames.skipCuts and blockedNames.skipCuts[index]) then
                originalCut = alternatives[value]
            end
        end
        if not clip or clip.samples <= 0 then return end
        local alternate = originalCut or (repair and repair.audio and repair.audio[index])
        local prefix = not alternate and (personalizedName or part.kind == "race") and clip.prefix
        local suffix = not alternate and part.kind == "race" and clip.suffix
        local frames = (alternate and alternate.samples or clip.samples)
            + (prefix and prefix.samples or 0) + (suffix and suffix.samples or 0)
        if alternate and alternate.captionStarts then captionStarts = alternate.captionStarts end
        local defaultPart = entry.sequence[index]
        local captionFallback
        if part.kind then
            captionFallback = defaultPart and defaultPart.kind == part.kind and defaultPart.fallback or part.fallback
        end
        clips[index] = { path = basePath .. (alternate and alternate.path or clip.path), samples = frames, start = samples,
                         first = part.first, last = part.last, captionValue = captionValue, fallbackSlot = fallbackSlot, prefix = prefix, suffix = suffix,
                         captionKind = part.kind,
                         typedAlternate = alternate and (part.kind == "class" or part.kind == "race") and index,
                         captionFallback = captionFallback,
                         captionBinding = part.caption, captionText = captionText, captionStarts = captionStarts,
                         articleFrom = clip.articleFrom, articleTo = clip.articleTo }
        samples = samples + frames
    end
    return clips, samples, repair, originalCuts ~= nil
end

-- Captions use the logical slot duration. WoW plays each physical Ogg once in succession.
local function PlaybackPieces(slots)
    local pieces = {}
    for _, slot in ipairs(slots) do
        local prefix, suffix = slot.prefix, slot.suffix
        if prefix or suffix then
            local before, after = prefix and prefix.samples or 0, suffix and suffix.samples or 0
            if prefix then
                pieces[#pieces + 1] = { path = basePath .. prefix.path, samples = before,
                    start = slot.start, fallbackSlot = slot.fallbackSlot, captionKind = slot.captionKind }
            end
            pieces[#pieces + 1] = { path = slot.path, samples = slot.samples - before - after,
                start = slot.start + before, fallbackSlot = slot.fallbackSlot, captionKind = slot.captionKind }
            if suffix then
                pieces[#pieces + 1] = { path = basePath .. suffix.path, samples = after,
                    start = slot.start + slot.samples - after, fallbackSlot = slot.fallbackSlot, captionKind = slot.captionKind }
            end
        else
            pieces[#pieces + 1] = slot
        end
    end
    return pieces
end

-- Move stock caption starts through the same source-to-playback timeline as the audio.
local function MoveTimings(timings, clips, rate, measured)
    if type(timings) == "string" then
        local tokens = {}
        for token in timings:gmatch("%S+") do
            local seconds = tonumber(token)
            if not seconds then return end
            tokens[#tokens + 1] = seconds
        end
        timings = tokens
    end
    if type(timings) ~= "table" then return end
    local adjusted, index = {}, 1
    for word, seconds in ipairs(timings) do
        if type(seconds) ~= "number" then return end
        local sample = seconds * rate
        while index < #clips and sample >= clips[index].last do index = index + 1 end
        local clip = clips[index]
        local span = clip.last - clip.first
        local fraction = span > 0 and math.max(0, math.min(1, (sample - clip.first) / span)) or 0
        adjusted[word] = (clip.start + clip.samples * fraction) / rate
    end
    if measured then
        local last = 0
        for word, seconds in ipairs(adjusted) do
            local start = measured[word] and measured[word] / rate or seconds
            if start < last then return adjusted end
            last = start
        end
        for word, sample in pairs(measured) do adjusted[word] = sample / rate end
    end
    return adjusted
end

local function RestoreStock(clip)
    local entry = prepared[clip]
    if not entry then return end
    clip.length, clip.timings = entry.stockLength, entry.stockTimings
    clip.packText = entry.stockPackText
    if entry.standalone then
        clip.fileName, clip.filePath, clip.module, clip.language =
            entry.stockFileName, entry.stockFilePath, entry.stockModule, entry.stockLanguage
    end
    if entry.englishCaptions and clip.present then
        clip.present.timings = entry.stockTimings
        clip.present.transcript = entry.stockPackText
    end
    prepared[clip] = nil
end

local function EnglishCaptions(language)
    -- Spoken 3.10 can display a translation independently of its voice pack.
    return not HasMethods(language, "GetTextLanguage") or language:GetTextLanguage() == "enUS"
end

local function StandaloneEnglishVoice(language)
    if not language or language:GetClientLanguage() ~= "enUS" then return false end
    if not HasMethods(language, "ResolutionOrder") then return true end
    for _, locale in ipairs(language:ResolutionOrder()) do
        if locale == "enUS" then return true end
    end
    return false
end

local function RememberSequenceTiming(clip)
    local entry = prepared[clip]
    entry.sequenceLength = clip.length
    if type(clip.timings) == "table" then
        entry.sequenceTimings = {}
        for word, seconds in ipairs(clip.timings) do entry.sequenceTimings[word] = seconds end
    end
end

local function PrepareStandalone(clip, entry, fallback, skipRepair)
    if not PlaybackAllowed(clip, entry) then return end
    local language = quests.Language or Language
    if not StandaloneEnglishVoice(language) then return end
    local offline = HasNameSlot(entry) and ShareUnavailable()
    local clips, total, repair, hasOriginalCuts = BuildSequence(entry, clip, fallback, skipRepair, offline)
    if not clips then return end
    local englishCaptions = EnglishCaptions(language)
    prepared[clip] = { clips = PlaybackPieces(clips), rate = entry.rate, route = entry, shareOffline = offline, repair = repair, hasOriginalCuts = hasOriginalCuts,
                       standalone = true, englishCaptions = englishCaptions,
                       stockFileName = clip.fileName, stockFilePath = clip.filePath,
                       stockModule = clip.module, stockLanguage = clip.language,
                       stockLength = clip.length, stockTimings = clip.timings, stockPackText = clip.packText }
    clip.fileName, clip.filePath = entry.key, prepared[clip].clips[1].path
    clip.language, clip.length = "enUS", total / entry.rate
    clip.module = standaloneModule
    clip.timings = nil
    if englishCaptions and repair and repair.caption then
        clip.packText, clip.timings = repair.caption, { unpack(repair.timings) }
        if clip.present then clip.present.transcript, clip.present.timings = clip.packText, clip.timings end
    elseif englishCaptions and entry.deliveryClock and entry.baseText and entry.baseTimings then
        local text, timings, measured = TypedCaption(entry.baseText, entry.baseTimings, clips, entry.rate)
        clip.packText, clip.timings = text, MoveTimings(timings, clips, entry.rate, measured)
        if repair and repair.head and type(clip.timings) == 'table' then
            for index, seconds in ipairs(repair.head) do clip.timings[index] = seconds end
        end
        if clip.present then clip.present.transcript, clip.present.timings = clip.packText, clip.timings end
    end
    RememberSequenceTiming(clip)
    return true
end

local function NamedEntry(clip)
    local entry = addon.playerNameDialogues and addon.playerNameDialogues[clip.fileName]
    if entry and entry.byName and entry.byName[PlayerNameKey()] then return entry end
end

local function HasPlayerName(entry)
    if not entry then return false end
    if entry.allowedRaces then
        local _, eligibilityRace = PlayerRace()
        if not entry.allowedRaces[eligibilityRace] then return false end
    end
    if entry.allowedClasses and not entry.allowedClasses[PlayerClass():lower()] then return false end
    if entry.allowedSex and not entry.allowedSex[Spoken.NotSecret(UnitSex("player"))] then return false end
    return not entry.names or entry.names[PlayerNameKey()]
end

local function CarriedItemID(guid)
    if type(NUM_BAG_SLOTS) ~= "number"
        or not HasMethods(C_Container, "GetContainerNumSlots", "GetContainerItemID")
        or not HasMethods(ItemLocation, "CreateFromBagAndSlot") or not HasMethods(C_Item, "GetItemGUID") then return end
    -- Quest records can name a different giver. Resolve this item's own GUID
    -- once so two copies or two items with the same quest text stay distinct.
    for bag = 0, NUM_BAG_SLOTS do
        local count = Spoken.NotSecret(C_Container.GetContainerNumSlots(bag)) or 0
        for slot = 1, count do
            local itemID = Spoken.NotSecret(C_Container.GetContainerItemID(bag, slot))
            if type(itemID) == "number" then
                local location = ItemLocation:CreateFromBagAndSlot(bag, slot)
                if Spoken.NotSecret(C_Item.GetItemGUID(location)) == guid then return itemID end
            end
        end
    end
    return false
end

local function QuestVariant(clip, candidates, key, stockBound)
    if not candidates or not clip.unitGUID or not quests.Utils or not quests.Utils.FillPlayerTokens then return end
    local text = Spoken.NotSecret(clip.text)
    local shown = type(text) == "string" and NormalizeQuestText(text)
    if shown == "" then shown = nil end

    -- A replay can reach the resolver without the original quest dialog.
    -- Match its exact stock key and require a unique route for the original giver below.
    local stockReplay
    if not shown and stockBound and events and type(clip.questID) == "number" then
        local stage = clip.event == events.QuestAccept and "accept"
            or clip.event == events.QuestComplete and "complete"
        if stage then
            local canonical = tostring(clip.questID) .. "-" .. stage
            stockReplay = key == canonical or key == modules:AddPlayerGenderToFilename(canonical)
        end
    end
    if not shown and not stockReplay then return end
    local guidType = quests.Utils:GetGUIDType(clip.unitGUID)
    local itemGiver = guidType == quests.Enums.GUID.Item
    local objectGiver = guidType == quests.Enums.GUID.GameObject
    if stockReplay and clip.event == events.QuestComplete and (itemGiver or objectGiver) then return end
    local giver
    if itemGiver then
        if type(clip.questID) ~= "number" then return end
        giver = CarriedItemID(clip.unitGUID)
        if giver == nil then
            -- Earlier clients can identify the item type only from the quest.
            if not HasMethods(modules, "GetQuestLogQuestGiverTypeAndID") then return end
            local kind
            kind, giver = modules:GetQuestLogQuestGiverTypeAndID(clip.questID)
            if kind ~= quests.Enums.GUID.Item then return end
        end
    elseif objectGiver then
        giver = quests.Utils:GetIDFromGUID(clip.unitGUID)
    else
        giver = quests.Utils:GetCreatureIDFromGUID(clip.unitGUID)
    end
    if not giver then return end
    local result, aliasResult, ambiguousAlias
    for _, entry in ipairs(candidates) do
        if HasPlayerName(entry) and itemGiver == (entry.itemGiver == true)
            and objectGiver == (entry.objectGiver == true)
            and (not entry.questID or entry.questID == clip.questID)
            and (not entry.speakerBody or entry.speakerBody == (clip.npcBody
                or quests.Utils.GetBodyForGUID and quests.Utils:GetBodyForGUID(clip.unitGUID)))
            and entry.event == clip.event and entry.key == key
            and (shown and MatchesDialogueText(shown, entry)
                or stockReplay and entry.deliveryClock and not entry.requireShownText
                    and not entry.worldStateText and giver == entry.npc) then
            if entry.npc == giver then
                if result then return end
                result = entry
            elseif entry.questGiverAliases and entry.questGiverAliases[giver] then
                if aliasResult then ambiguousAlias = true end
                aliasResult = entry
            end
        end
    end
    return result or (not ambiguousAlias and aliasResult or nil)
end

local function MeetsGossipInventory(requirement)
    if not requirement then return true end
    local countItems = C_Item and C_Item.GetItemCount or GetItemCount
    if type(countItems) ~= "function" then return false end
    local count = Spoken.NotSecret(countItems(requirement.item, false))
    return type(count) == "number" and count >= requirement.count
end

local function ScopedGossip(clip)
    local utils = quests.Utils
    if not events or not (clip.event == events.Gossip or clip.event == events.QuestGreeting) or not clip.unitGUID
        or not utils or not utils.FillPlayerTokens then return end
    local text = Spoken.NotSecret(clip.text)
    if type(text) ~= "string" or text == "" then return end
    local guid = quests.Enums.GUID
    local kind = utils:GetGUIDType(clip.unitGUID)
    local giverType = guid:IsCreature(kind) and "Creature" or kind == guid.GameObject and "GameObject"
    if not giverType then return end
    local candidates = gossipByGiver[utils:GetIDFromGUID(clip.unitGUID)]
    if not candidates then return end
    local body = giverType == "Creature" and utils.GetBodyForGUID and utils:GetBodyForGUID(clip.unitGUID)
    local sex = Spoken.NotSecret(UnitSex("player"))
    local shown = NormalizeQuestText(text)
    local best, score, leastTokens, ambiguous
    for _, entry in ipairs(candidates) do
        if HasPlayerName(entry) and entry.giverType == giverType and (not entry.playerSex or entry.playerSex == sex)
            and (not entry.speakerBody or entry.speakerBody == body)
            and MatchesDialogueText(shown, entry) and MeetsGossipInventory(entry.gossipInventory) then
            local priority = (entry.key == clip.fileName and 4 or 0)
                + (entry.speakerBody and 2 or 0) + (entry.playerSex and 1 or 0)
            -- A fixed class word is more specific when it renders to the same
            -- line as a class token and Spoken has selected a third stock key.
            local _, playerTokens = entry.text:gsub("%$[nNcCrRgG]", "")
            local _, worldStates = entry.text:gsub("%$%d+[wW]", "")
            local tokens = playerTokens + worldStates
            if not score or priority > score or (priority == score and tokens < leastTokens) then
                best, score, leastTokens, ambiguous = entry, priority, tokens, false
            elseif priority == score and tokens == leastTokens then
                ambiguous = true
            end
        end
    end
    if not ambiguous and best then
        if best.worldParts and addon.RenderWorldState then
            return addon.RenderWorldState(best, WorldStateValues(shown, best))
        end
        return best
    end
end

local function MissingStockEntry(clip, instead)
    local named = NamedEntry(clip)
    if named and named.standalone then return named end
    for _, key in ipairs({ clip.fileName, instead }) do
        local gendered = modules:AddPlayerGenderToFilename(key)
        local variant = QuestVariant(clip, standaloneVariants[gendered], gendered)
            or QuestVariant(clip, standaloneVariants[key], key)
        if variant then return variant end
        if HasPlayerName(standalone[gendered]) then return standalone[gendered] end
        if HasPlayerName(standalone[key]) then return standalone[key] end
    end
end

local function ScopedFollowup(clip, candidates)
    local followup, utils = quests.Followup, quests.Utils
    if not candidates or not events or not clip.event
        or (clip.event ~= events.QuestFollowup and clip.event ~= events.NPCChatter)
        or not clip.unitGUID or not HasMethods(followup, "Render")
        or not HasMethods(utils, "GetCreatureIDFromGUID") then return end
    local text = Spoken.NotSecret(clip.text)
    if type(text) ~= "string" or text == "" then return end
    local speaker = utils:GetCreatureIDFromGUID(clip.unitGUID)
    local sender = Spoken.NotSecret(clip.name)
    local shown = NormalizeQuestText(text):lower()
    local selected
    for _, entry in ipairs(candidates) do
        if HasPlayerName(entry) and entry.event == clip.event and entry.key == clip.fileName
            and (clip.event ~= events.NPCChatter or (entry.lineKey and entry.lineKey == clip.lineKey))
            and entry.npc == speaker and followup.Render(entry.text, sender):lower() == shown then
            if selected then return end
            selected = entry
        end
    end
    return selected
end

local function NativeQuestVariant(clip, key)
    local gendered = modules:AddPlayerGenderToFilename(key)
    local native = QuestVariant(clip, standaloneVariants[gendered], gendered)
        or QuestVariant(clip, standaloneVariants[key], key)
    if native and native.deliveryClock then return native end
end

local function QuestMomentAlternative(clip, instead)
    if not events or type(clip.questID) ~= "number" then return end
    local stage = clip.event == events.QuestAccept and "accept"
        or clip.event == events.QuestProgress and "progress"
        or clip.event == events.QuestComplete and "complete"
    if not stage then return end
    local canonical = tostring(clip.questID) .. "-" .. stage
    if instead == canonical and instead ~= clip.fileName then
        local native = NativeQuestVariant(clip, instead)
        if native and native.verifiedStockPath == clip.filePath then return native end
        -- Spoken can resolve this moment under a giver-specific voice key.
        -- The event, giver and wording already match in NativeQuestVariant.
        local giverVoicePrefix = canonical .. "-"
        if native and not native.verifiedStockPath and type(clip.fileName) == "string"
            and clip.fileName:sub(1, #giverVoicePrefix) == giverVoicePrefix then
            return native
        end
    end
    if clip.fileName ~= canonical and clip.fileName == modules:AddPlayerGenderToFilename(canonical) then
        local native = QuestVariant(clip, standaloneVariants[canonical], canonical)
        if native and native.deliveryClock and not native.verifiedStockPath then return native end
    end
end

local function EntryForStockPath(clip, instead)
    local named = NamedEntry(clip)
    if named and clip.filePath == basePath .. named.stock then return named end
    local path = clip.filePath
    if followupVariants[path] then return ScopedFollowup(clip, followupVariants[path]), false end
    local variant = QuestVariant(clip, questVariants[path], clip.fileName, true)
    if variant then return variant, false end
    -- A typed native route need not have a signed stock file binding. Match its
    -- admitted giver, event and text even when another stock pack resolves it.
    local native = NativeQuestVariant(clip, clip.fileName)
    if native then return native, false end
    -- A verified quest may use its scoped recording when Spoken resolves an
    -- explicitly bound giver's file. Other stock voice variants keep priority.
    local alternative = QuestMomentAlternative(clip, instead)
    if alternative then return alternative, false, true end
    local gossip = ScopedGossip(clip)
    if gossip then return gossip, gossip.stockPath == path, true end
    local entry = registry[path]
    if entry then
        if entry.baseTimings and not QuestVariant(clip, { entry }, clip.fileName) then return end
        return not entry.gossipScoped and HasPlayerName(entry) and entry or nil, true
    end
    if not addon.legacyAliases or not path then return end

    if path:find(basePath .. "DMSpeaksCorpus_enUS_20261104\\", 1, true) == 1 then
        entry = addon.legacyAliases[clip.fileName] or standalone[clip.fileName]
    else
        local pack, key = path:match("^Interface\\AddOns\\([^\\]+)\\generated\\sounds\\[^\\]+\\([^\\]+)\\")
        if key == clip.fileName and (pack == "DMSpeaksSpokenAlliance"
            or pack == "DMSpeaksSpokenHorde" or pack == "DMSpeaksSpokenShared") then
            entry = standalone[key]
        end
    end
    if HasPlayerName(entry) then return entry, false end
end

local function PrepareSequence(clip, entry, useStockTimings, scopedMatch, fallback, skipRepair)
    if not PlaybackAllowed(clip, entry) or clip.language ~= "enUS"
        or (clip.fileName ~= entry.key and not scopedMatch) then return end
    local language = quests.Language or Language
    if entry.deliveryClock and (not language or language:GetClientLanguage() ~= "enUS") then return end
    if (entry.onlyClass and PlayerClass() ~= entry.onlyClass)
        or (entry.onlySex and Spoken.NotSecret(UnitSex("player")) ~= entry.onlySex) then
        return
    end
    local offline = HasNameSlot(entry) and ShareUnavailable()
    local clips, total, repair, hasOriginalCuts = BuildSequence(entry, clip, fallback, skipRepair, offline)
    if not clips then return end
    local englishCaptions = EnglishCaptions(language)
    prepared[clip] = { clips = PlaybackPieces(clips), rate = entry.rate, route = entry, shareOffline = offline, repair = repair, hasOriginalCuts = hasOriginalCuts,
                       useStockTimings = useStockTimings, scopedMatch = scopedMatch,
                       englishCaptions = englishCaptions,
                       stockLength = clip.length, stockTimings = clip.timings,
                       stockPackText = clip.packText }
    clip.length = total / entry.rate
    if englishCaptions then
        if repair and repair.caption then
            clip.packText, clip.timings = repair.caption, { unpack(repair.timings) }
        elseif entry.legacyNameCaption and not offline and type(entry.text) == "string" then
            local followup = quests.Followup
            if clip.event == events.QuestFollowup and HasMethods(followup, "Render") then
                clip.packText = followup.Render(entry.text, clip.name)
            elseif HasMethods(quests.Utils, "FillPlayerTokens") then
                clip.packText = quests.Utils:FillPlayerTokens(entry.text)
            end
            clip.timings = nil
        elseif entry.baseTimings or useStockTimings ~= false then
            local timings = entry.baseTimings or clip.timings
            local text = entry.baseText or clip.packText
            local measured
            if entry.deliveryClock then text, timings, measured = TypedCaption(text, timings, clips, entry.rate)
            else text = InfillCaption(text, timings, clips, entry.rate) end
            clip.packText = text
            clip.timings = MoveTimings(timings, clips, entry.rate, measured)
            if repair and repair.head and type(clip.timings) == 'table' then
                for index, seconds in ipairs(repair.head) do clip.timings[index] = seconds end
            end
        else
            clip.timings = nil
        end
        if clip.present then
            clip.present.timings = clip.timings
            clip.present.transcript = clip.packText
        end
    end
    RememberSequenceTiming(clip)
end

local function HasCurrentName(entry)
    local key = PlayerNameKey()
    if entry.byName and entry.byName[key] then return true end
    for _, part in ipairs(entry.sequence) do
        if part.kind == "name" and key ~= part.fallback and part.choices[key] then return true end
    end
    return false
end

local function PreparePlaybackSequence(clip, entry, useStockTimings, scopedMatch)
    if not PlaybackAllowed(clip, entry) then return end
    local name = PlayerNameKey()
    if entry.deliveryClock and not (HasNameSlot(entry) and ShareUnavailable())
        and not HasCurrentName(entry) and not HasFetchedName(entry.sequence, name, true) then
        for _, prior in ipairs(retainedStoreNames[entry.storeRevision] or {}) do
            if HasFetchedName(prior.sequence, name, true) then
                PrepareSequence(clip, prior, false, scopedMatch)
                if prepared[clip] then
                    prepared[clip].catalogFallback = { entry, useStockTimings, scopedMatch }
                    return
                end
            end
        end
        local legacy
        if clip.event == events.QuestFollowup then
            legacy = ScopedFollowup(clip, legacyNamedFollowups[entry.key])
        else
            legacy = QuestVariant(clip, legacyNamedQuests[entry.key], entry.key)
        end
        if legacy and not HasFetchedName(entry.sequence, name) then
            PrepareSequence(clip, legacy, false, scopedMatch)
            if prepared[clip] then
                prepared[clip].catalogFallback = { entry, useStockTimings, scopedMatch }
                return
            end
        end
    end
    PrepareSequence(clip, entry, useStockTimings, scopedMatch)
end

function modules:PrepareSound(clip)
    local found, reason = prepareStock(self, clip)
    if not found and PrepareStandalone(clip, ScopedGossip(clip)) then return true end
    return found, reason
end

function modules:ResolveSoundFile(clip, instead)
    RestoreStock(clip)
    local found = resolveStock(self, clip, instead)
    if not found then
        local entry = ScopedGossip(clip) or MissingStockEntry(clip, instead)
        return PrepareStandalone(clip, entry) or found
    end
    if not (hookEnqueue and clip.event == events.QuestFollowup) then
        PreparePlaybackSequence(clip, EntryForStockPath(clip, instead))
    end
    return found
end

-- Spoken resolves a probe for follow-ups and chatter, then creates a new clip
-- from its stock path. Apply the sequence to that final clip before queueing.
if hookEnqueue then
    function quests.Player:EnqueuePrepared(clip, asked)
        if clip.event == events.QuestFollowup or clip.event == events.NPCChatter then
            local previous = prepared[clip]
            local standaloneClip = clip.module == standaloneModule
            RestoreStock(clip)
            if standaloneClip then
                if not PrepareStandalone(clip, previous and previous.route or MissingStockEntry(clip)) then
                    return false
                end
            else
                PreparePlaybackSequence(clip, EntryForStockPath(clip))
            end
        end
        return enqueue(self, clip, asked)
    end
end

-- Spoken has already matched this trigger to its armed quest script. Offer a
-- local recording only when that exact line, creature and model are known.
local followup, npcModel, utils = quests.Followup, quests.NPCModel, quests.Utils
if hookEnqueue and events.QuestFollowup and next(followupsByBroadcast)
    and HasMethods(followup, "Play", "Render", "OwnClip")
    and HasMethods(npcModel, "Get") and HasMethods(utils, "GetCreatureIDFromGUID") then
    local playFollowup = followup.Play

    local function FindNativeFollowup(trigger, message, sender, guid, npcID, event)
        if event and event ~= events.QuestFollowup then return end
        local line = trigger and trigger.line
        local candidates = line and followupsByBroadcast[line.id]
        local speaker = guid and utils:GetCreatureIDFromGUID(guid)
        if not candidates or not speaker or speaker ~= line.speaker or (npcID and npcID ~= speaker)
            or type(message) ~= "string" then return end
        local model = Spoken.NotSecret(npcModel:Get(guid))
        if type(model) ~= "number" then return end
        local shown = NormalizeQuestText(message):lower()
        local result
        for _, entry in ipairs(candidates) do
            local matchesModel = entry.modelFileID == model or
                (entry.verifiedModelAliases and entry.verifiedModelAliases[model] == true)
            if entry.npc == speaker and matchesModel and HasPlayerName(entry)
                and followup.Render(entry.text, sender):lower() == shown then
                if result then return end
                result = entry
            end
        end
        return result
    end

    function followup:Play(trigger, message, sender, guid, npcID, asked, event)
        local entry = FindNativeFollowup(trigger, Spoken.NotSecret(message), Spoken.NotSecret(sender),
            Spoken.NotSecret(guid), Spoken.NotSecret(npcID), event)
        if entry and not self.OwnClip(trigger.line, npcID, guid) then
            local clip = { event = events.QuestFollowup, name = sender or "", title = trigger.title,
                lineKey = trigger.lineKey, text = message, unitGUID = guid, unitIsObjectOrItem = false }
            if PrepareStandalone(clip, entry) then
                return enqueue(quests.Player, clip, asked)
            end
        end
        return playFollowup(self, trigger, message, sender, guid, npcID, asked, event)
    end
end

local function PiecesAvailable(parts, channel, missingChoices, missingAlternates)
    local ready = true
    for _, piece in ipairs(parts) do
        if not testStock(sounds, piece, channel) then
            if not missingChoices then return false end
            if missingAlternates and piece.typedAlternate then missingAlternates[piece.typedAlternate] = true end
            if piece.fallbackSlot then
                missingChoices[piece.fallbackSlot] = true
                if piece.captionKind == "name" then missingChoices.name = true end
            end
            ready = false
        end
    end
    return ready
end

function sounds:TestSound(clip, channel)
    local entry = prepared[clip]
    if not entry then return testStock(self, clip, channel) end
    if not PlaybackAllowed(clip, entry.route)
        or (entry.catalogFallback and not PlaybackAllowed(clip, entry.catalogFallback[1])) then
        RestoreStock(clip)
        return not entry.standalone and testStock(self, clip, channel) or false
    end
    if HasNameSlot(entry.route) and ShareUnavailable() ~= entry.shareOffline then
        -- The share may have changed while this dialogue waited in Spoken's queue.
        local route = entry.catalogFallback
        RestoreStock(clip)
        if entry.standalone then
            PrepareStandalone(clip, entry.route)
        else
            PreparePlaybackSequence(clip, route and route[1] or entry.route,
                route and route[2] or entry.useStockTimings, route and route[3] or entry.scopedMatch)
        end
        entry = prepared[clip]
        if not entry then return testStock(self, clip, channel) end
    end
    local missingChoices, missingAlternates = {}, {}
    if PiecesAvailable(entry.clips, channel, missingChoices, missingAlternates) then return true end
    RestoreStock(clip)
    local hasAlternates = entry.repair or entry.hasOriginalCuts
    local function Rebuild(choices, skipRepair)
        if entry.standalone then
            PrepareStandalone(clip, entry.route, choices, skipRepair)
        else
            PrepareSequence(clip, entry.route, entry.useStockTimings, entry.scopedMatch, choices, skipRepair)
        end
        if prepared[clip] then prepared[clip].catalogFallback = entry.catalogFallback end
        return prepared[clip]
    end
    -- An optional cut can disappear while the ordinary class or race take is
    -- still present. Check that take before marking the slot generic. Repairs
    -- with authored full-line captions must be omitted as a unit.
    local skipRepair = entry.repair and next(missingAlternates) ~= nil
    if next(missingAlternates) then
        local ordinary = Rebuild({skipCuts = missingAlternates}, skipRepair)
        if ordinary then
            missingChoices = {}
            if PiecesAvailable(ordinary.clips, channel, missingChoices) then return true end
        end
        RestoreStock(clip)
    end
    local fallback = (entry.route.byName and not next(missingChoices)) and true or missingChoices
    if type(fallback) == "table" and next(missingAlternates) then fallback.skipCuts = missingAlternates end
    -- Rebuild captions with the same fallback choices used for playback.
    if next(missingChoices) then
        local withFallback = Rebuild(fallback, skipRepair)
        if withFallback and PiecesAvailable(withFallback.clips, channel) then return true end
        RestoreStock(clip)
    end
    if hasAlternates then
        Rebuild(fallback, true)
    elseif entry.catalogFallback then
        local fallback = entry.catalogFallback
        PrepareSequence(clip, fallback[1], fallback[2], fallback[3])
    else
        Rebuild(fallback)
    end
    local preparedFallback = prepared[clip]
    if preparedFallback and PiecesAvailable(preparedFallback.clips, channel) then return true end
    RestoreStock(clip)
    if not entry.standalone and entry.stockLength then return testStock(self, clip, channel) end
    return false
end

local function CancelPlayback(clip, fadeMs)
    local state = playing[clip]
    if not state then return end
    playing[clip] = nil
    clip.handle = nil
    if state.timer then state.timer:Cancel() end
    if state.cutTimer then state.cutTimer:Cancel() end
    if state.piece then stopStock(sounds, state.piece, fadeMs) end
end

function sounds:PlaySound(clip, channel)
    CancelPlayback(clip)
    -- Some queue sources skip the admission probe. Files admitted earlier can
    -- also disappear while a clip waits, so validate before playing its prefix.
    if prepared[clip] and not self:TestSound(clip, channel) then return false end
    local entry = prepared[clip]
    if not entry then return playStock(self, clip, channel) end

    local state = { started = GetTime() }
    if entry.sequenceTimings then
        clip.timings = {}
        for word, seconds in ipairs(entry.sequenceTimings) do clip.timings[word] = seconds end
        if clip.present then clip.present.timings = clip.timings end
    end
    clip.length = entry.sequenceLength or clip.length
    playing[clip] = state
    local function PlayPiece(index)
        if playing[clip] ~= state then return end
        local piece = { path = entry.clips[index].path }
        local accepted = playStock(sounds, piece, channel)
        if not accepted then
            if index == 1 then
                CancelPlayback(clip)
                RestoreStock(clip)
                if entry.standalone or not entry.stockLength then return false end
                return playStock(sounds, clip, channel)
            end
            queue:PauseQueue()
            RestoreStock(clip)
            return false
        end
        state.piece = piece
        local elapsed = GetTime() - state.started
        local nominal = entry.clips[index].start / entry.rate
        local delay = math.max(0, elapsed - nominal)
        state.endsAt = state.started + (entry.sequenceLength or clip.length) + delay
        clip.length = (entry.sequenceLength or clip.length) + delay
        if clip.spokenAt then clip.spokenAt = state.endsAt end
        state.lastPiece = not entry.clips[index + 1]
        if entry.sequenceTimings then
            for word, seconds in ipairs(entry.sequenceTimings) do
                if seconds >= nominal then clip.timings[word] = seconds + delay end
            end
        end
        if index == 1 then state.awaitingQueueTimer = true end
        -- The queue uses the presence of a handle to enable pause and skip.
        -- The numeric handle belongs to the active piece and changes at each join.
        clip.handle = true
        local following = entry.clips[index + 1]
        if following then
            -- Decoded duration advances the sequence. Source/phoneme offsets
            -- only describe extraction and captions, never overlapping starts.
            state.timer = C_Timer.NewTimer(entry.clips[index].samples / entry.rate, function()
                if playing[clip] ~= state or state.piece ~= piece then return end
                stopStock(sounds, piece, 0)
                PlayPiece(index + 1)
            end)
        end
        -- Spoken's stock watcher follows one handle. Follow the active piece instead.
        if state.cutTimer then state.cutTimer:Cancel() end
        if C_Sound and C_Sound.IsPlaying and type(piece.handle) == "number"
            and entry.clips[index].samples / entry.rate > 1 then
            state.cutTimer = C_Timer.NewTimer(0.5, function()
                if playing[clip] == state and state.piece == piece then
                    local ok, audible = pcall(C_Sound.IsPlaying, piece.handle)
                    if ok and audible == false then queue:PauseQueue() end
                end
            end)
        end
        return true
    end
    return PlayPiece(1)
end

-- Spoken starts one completion timer for the whole dialogue immediately after
-- PlaySound. Scope its callback to this exact queue head and prepared playback;
-- late internal joins may extend it, but cannot advance the outer queue early.
local queueTimerOwner = HasMethods(core.Addon, "ScheduleTimer") and core.Addon or Spoken
if HasMethods(queueTimerOwner, "ScheduleTimer") then
    local scheduleQueueTimer = queueTimerOwner.ScheduleTimer
    function queueTimerOwner:ScheduleTimer(callback, seconds, ...)
        local head = type(queue.sounds) == "table" and queue.sounds[1]
        local state = head and playing[head]
        if type(callback) ~= "function" or not state or not state.awaitingQueueTimer
            or type(seconds) ~= "number" or math.abs(seconds - head.length) > 0.000001 then
            return scheduleQueueTimer(self, callback, seconds, ...)
        end
        state.awaitingQueueTimer = false
        local arguments = { ... }
        local function FinishWhenComplete()
            if playing[head] ~= state then return end
            local remaining = state.endsAt - GetTime()
            if not state.lastPiece or remaining > 0.000001 then
                head.nextSoundTimer = scheduleQueueTimer(self, FinishWhenComplete, math.max(0.001, remaining))
                return
            end
            return callback(unpack(arguments))
        end
        return scheduleQueueTimer(self, FinishWhenComplete, seconds)
    end
end

function sounds:StopSound(clip, fadeMs)
    CancelPlayback(clip, fadeMs)
    return stopStock(self, clip, fadeMs)
end

Spoken:RegisterCallback("CLIP_STOPPED", function(clip)
    local state = playing[clip]
    if not state then return end
    playing[clip] = nil
    if state.timer then state.timer:Cancel() end
    if state.cutTimer then state.cutTimer:Cancel() end
end)
