'use strict';

// Message IDs belong to the UI. Character IDs, faction IDs and SavedVariables
// keys stay stable when a translator changes the wording or placeholder order.
const liteLocale = (() => {
  const english = {
    cardAccount: 'Account · {accounts}',
    hideProfile: 'Hide Profile', deleteProfile: 'Delete Profile',
    hideProfileTitle: 'Hide profile?', deleteProfileTitle: 'Delete profile?',
    hideProfileDescription: 'Hide {name} from Lite. You can unhide this profile under Hidden profiles.',
    deleteProfileDescription: 'Delete {name} from Lite. DMSpeaks Lite can add this profile again when WoW saves the character. Your recordings are kept.',
    manageProfiles: 'Manage profiles…', manageProfilesTitle: 'Manage profiles',
    manageProfilesDescription: 'Choose the profiles to hide or delete. Hidden profiles stay hidden until you unhide them. DMSpeaks Lite can add deleted profiles again when WoW saves those characters. Your recordings are kept.',
    selectAllProfiles: 'Select all profiles', hideSelectedProfiles: 'Hide selected ({count})', deleteSelectedProfiles: 'Delete selected ({count})',
    profilesHidden: {one: '{count} profile was hidden.', other: '{count} profiles were hidden.'},
    profilesDeleted: {one: '{count} profile was deleted from Lite.', other: '{count} profiles were deleted from Lite.'},
    hiddenProfiles: 'Hidden profiles', unhideProfile: 'Unhide', cancel: 'Cancel',
    profileHidden: '{name} was hidden.', profileDeleted: '{name} was deleted from Lite.', profileUnhidden: '{name} is visible again.',
    currentProfileHidden: '{name} is hidden. Choose Unhide under Hidden profiles.',
    currentProfileRestored: '{name} is back in Character profiles.',
    fullName: '{name} {realm}', accountNumber: 'WoW account {number}', accountNames: 'WoW account labels',
    labelName: 'Display name', saveLabel: 'Save label', labelSaved: 'Account label saved on this computer.',
    appMenu: 'Application menu', fileMenu: 'File', viewMenu: 'View', optionsMenu: 'Options', helpMenu: 'Help',
    importItem: 'Import…', exportItem: 'Export…', profilesItem: 'Character profiles', fetchItem: 'Fetch status',
    speechSettings: 'Speech settings', storageTitle: 'Name card storage', advanced: 'Advanced settings', helpItem: 'Using name cards',
    exportTitle: 'Export name cards', exportScope: 'Cards to include', allCards: 'All name cards',
    exportDestination: 'Save to', exportDownloads: 'Browser downloads',
    exportChooseFolder: 'Choose a local or shared folder…',
    exportSyncFolder: 'Configured storage folder',
    exportSavedTo: 'Name card backup saved to {path}.',
    exportButton: 'Export', close: 'Close', selectedCard: '{name} {realm}',
    importComplete: {one: 'Imported {cards} card ({added} new, {kept} existing recordings).',
      other: 'Imported {cards} cards ({added} new, {kept} existing recordings).'},
    importConflicts: {one: 'Existing recordings were kept for {count} conflicting entry.',
      other: 'Existing recordings were kept for {count} conflicting entries.'},
    exportReady: 'Name card backup downloaded.', importing: 'Importing name cards…', exporting: 'Preparing the backup…',
    service: 'Storage location', connect: 'Connect…', openStorageApp: 'Open desktop storage app…', connectServer: 'Connect to server…',
    serverShare: 'Server and shared folder', folder: 'Folder', browse: 'Choose folder…',
    lanOnly: 'Accessible only on my local network', saveFolder: 'Use this folder', syncNow: 'Sync now', disconnect: 'Disconnect',
    cloudHelp: 'Sign in through the provider’s desktop app, then choose the same synced folder on both computers.',
    googleDriveFolderHelp: 'Open Google Drive for desktop and sign in there if prompted. In Lite, use Choose folder… to pick a folder under Google Drive → My Drive, then select Use this folder. Choose the same synced folder on each computer. An unsynced folder keeps the files local. Direct Google Drive sign-in in Lite is a future idea.',
    advancedNetwork: 'Show advanced network storage',
    tailscaleHelp: 'Connect both devices to Tailscale. Enter the file server’s Tailscale name or address and share, then choose its mounted folder. If the share goes offline, NPCs use “Adventurer”.',
    shareHelp: 'Connect to the share with your computer’s account prompt, then choose its mounted folder.',
    folderHelp: 'Choose the same shared folder on both computers to exchange cards and recordings.',
    providerOpened: 'Finish signing in with your storage app, then choose its folder.',
    networkOpened: 'Finish connecting in your file manager, then choose the shared folder.',
    helpOpened: 'Provider setup opened. Connect its desktop app, then choose the synced folder.',
    folderSaved: 'Folder saved. Sync checks for changes automatically.',
    folderDisconnected: 'Disconnected. Your existing recordings are still available.',
    syncWorking: 'Checking the shared folder…', syncFinished: 'Folder updated.',
    cardHelp: 'WoW uses the character you are playing to pick each recording. Select a different card in Lite only when you want to prepare audio for that character. File → Export saves cards with their recordings. On another computer use File → Import, or connect both computers to the same folder in Options → Name card storage.',
    keyHelp: 'Alt+Shift+F opens File. Use the arrow keys to navigate and Escape to close a menu.',
    waitingCatalog: "Waiting for the dialogue catalog.",
    chooseProviderToFetch: "Choose a provider above, then complete Speech settings below.",
    saveToFetch: "In Speech settings below, select Save Fetch Settings to use this provider.",
    credentialNeeded: "Scroll down to Speech settings, enter the provider's API key, and select Save Fetch Settings.",
    consentNeeded: "Fetching is disabled for this provider. Save your settings to enable it.",
    pausing: "Pausing fetch…",
    resumeFor: "Resume Fetch for {name}",
    resumeHint: "Click Resume Fetch to continue.",
    heldRetryHint: "Start Fetch retries held takes and continues through the selected area.",
    selectedFetchFinished: {one: "Selected dialogue: {ready} of {total} ready.", other: "Selected dialogues: {ready} of {total} ready."},
    replacementSaved: "Replacement recording saved.",
    replacementKept: "The replacement was not saved. Your previous recording was kept.",
    fetchPassFinished: "Fetch pass finished. {ready} of {total} ready.",
    addPersonalProvider: "Add a self-hosted Fish server…",
    personalProviders: "Self-hosted Fish (SSH)",
    personalProviderName: "Connection name",
    removePersonalProvider: "Remove connection",
    personalProviderRemoved: "Connection removed.",
    removePersonalProviderPrompt: "Remove {name} from your saved connections?",
    startFor: "Start Fetch for {name}",
    stop: "Stop Fetch",
    stopping: "Stopping fetch…",
    starting: "Starting fetch…",
    checking: "Checking coverage…",
    refreshing: "Refreshing status…",
    prepareNames: "Preparing names…",
    idle: "No fetch in progress.",
    paused: "Fetch paused.",
    complete: "Fetch complete.",
    chooseProvider: "Choose a speech provider",
    savedCredential: "A credential is saved in Lite. Leave this blank to keep it, or enter a replacement.",
    noCredential: "No credential saved for this provider.",
    credentialDescription: "Credential saved. Enter a new value to replace it.",
    meteredPrompt: "Name fetching uses your speech provider account. Your provider may charge for requests. Enable fetching?",
    fetchDisabled: "Settings saved. Fetching is disabled for this account.",
    savedSettings: "Fetch settings saved.",
    testPass: "Connection check passed.",
    testFail: "Connection check failed.",
    storageSaved: "Storage preference saved.",
    checkingZip: "Checking the dialogue catalog ZIP…",
    openingCatalog: "Opening the dialogue catalog…",
    checkingCatalog: "Checking the installed dialogue catalog…",
    savingCatalog: "Saving the verified dialogue catalog…",
    checkingPlayback: "Checking the playback ZIP…",
    preparePlayback: "Preparing the WoW playback add-on…",
    installingPlayback: "Installing the WoW playback add-on…",
    preparingCatalog: "Preparing the dialogue catalog…",
    catalogReady: "Dialogue plans are ready.",
    addCatalog: "Add a prepared dialogue catalog before fetching audio.",
    coverageInterrupted: "The coverage check ended before it completed.",
    saveFailed: "The settings could not be saved.",
    liteDisconnected: "Connection to Lite lost. Open DMSpeaks Lite from the Windows Start menu to reconnect.",
    pollTimedOut: "Lite took too long to respond.",
    loadNewAudio: "Type /reload in WoW chat to use your new audio.",
    providerApiKey: "API key",
    fishToken: "Fish API token",
    azureKey: "Speech resource key",
    googleToken: "OAuth access token",
    fishHelp: "Recommended. Connects to the hosted Fish Audio API.",
    'provider.fish_omni': "Your own speech server (SSH)",
    selfHostedHelp: "Connect to a computer you manage. It must already be running a compatible Fish Audio Drama 3 service.",
    providerHelp: "Personalized audio uses matching voices from your account.",
    providerArea: "{provider} · {area}",
    nameRealm: "{name} {realm}",
    fetchProgress: "Dialogue {current} of {total} in this fetch · {status}",
    currentNameProgress: "Current name recording",
    selectedAreaAudio: "Saved name audio in the selected area",
    dialogueCoverage: "Dialogue coverage",
    npcCoverage: "NPC · {label} (all areas)",
    fetchNpcElapsed: "{npc} fetch time {duration}",
    fetchPassElapsed: "Total fetch time {duration}",
    fetchTimeSpeakers: "Fetch time by NPC",
    fetchTimeDialogues: "Dialogue times",
    incompleteTiming: "Some earlier fetch times could not be read.",
    fetchTimeHistory: "Fetch history ({count})",
    fetchTimePass: "Fetch {index}",
    fetchTimeWhen: "{pass} · {date}",
    fetchTimePaused: "Paused",
    fetchTimeStopped: "Stopped",
    fetchTimeFailed: "Could not finish",
    fetchTimeReplacement: "Replacement",
    fetchTimeSelected: "Selected dialogues",
    fetchTimeCoverage: "{ready} of {total} ready",
    fetchTimeLoading: "Loading dialogue times…",
    fetchTimeSaved: "Previously saved",
    fetchTimeHeld: "Needs attention",
    fetchTimeSourceUnavailable: "Voice reference unavailable",
    fetchSourceUnavailable: {one: "{count} dialogue needs a voice reference.", other: "{count} dialogues need a voice reference."},
    fetchTimeQuest: "Quest {id}",
    zoneCoverage: "Zone · {label}",
    zoneNpcs: "NPCs in {zone}",
    currentSpeaker: "Current speaker",
    noZoneNpcs: "No eligible NPC dialogues in this zone.",
    npcCheckHint: "Check Coverage to see which NPC dialogues are ready.",
    overallCoverage: "All zones for {name}",
    autoRetakeNumber: "Automatic retry {attempt} of {total}",
    recheckSavedTakes: "An earlier build held the recorded name audio. Start Fetch to recheck the saved takes.",
    remainingDialogues: {one: "{count} dialogue remaining.", other: "{count} dialogues remaining."},
    latestAreaCheck: {one: "Latest area check found {ready} of {total} dialogue ready.", other: "Latest area check found {ready} of {total} dialogues ready."},
    verifyCatalog: "Verifying dialogue plans ({current} of {total})…",
    verifySources: "Verifying original-game dialogue ({current} of {total})…",
    verifyPlayback: "Verifying playback recordings ({current} of {total})…",
    planCount: {"one": "{count} dialogue plan ready.", "other": "{count} dialogue plans ready."},
    verifiedPlans: {one: '{count} dialogue plan verified.', other: '{count} dialogue plans verified.'},
    verifiedReturns: {one: '{count} saved return verified.', other: '{count} saved returns verified.'},
    verifiedRecordings: {one: '{count} dialogue recording verified. {details}', other: '{count} dialogue recordings verified. {details}'},
    voiceMappingExample: 'NPC ID = provider voice ID',
    'error.loopback_required': 'Open this page on 127.0.0.1.',
    'error.card_zip_required': 'Choose a name-card ZIP file.',
    'error.card_size_limit': 'Choose a name-card backup under 2 GiB.',
    'error.settings_session_expired': 'Refresh this Lite tab and try again.',
    'error.settings_busy': 'The Lite settings database is busy. Try again in a moment.',
    'error.settings_database_error': 'Lite could not access its settings database.',
    'error.settings_json_required': 'Send a JSON settings object.',
    'error.settings_size_limit': 'Settings request is too large or empty.',
    localOnly: 'Local',
    folderSynced: 'Synced',
    syncPending: 'Changes to sync',
    lastSynced: 'Last synced',
    shared: 'Shared folder',
    onedrive: 'OneDrive',
    googledrive: 'Google Drive',
    icloud: 'iCloud Drive',
    dropbox: 'Dropbox',
    nas: 'NAS',
    tailscale: 'Tailscale',
    network: 'Network drive', sftp: 'SFTP (SSH)', webdav: 'WebDAV', nfs: 'NFS',
    syncthing: 'Syncthing', rsync: 'rsync', rclone: 'rclone',
    cacheOffline: 'Keep a copy of name audio on this computer',
    cacheOfflineHelp: 'Lite saves verified recordings in WoW’s name audio folder for offline play. Missing recordings use “Adventurer”.',
    availableOffline: 'Available offline',
    onNas: 'Stored on a NAS', viaTailscale: 'Access over Tailscale',
    sftpHelp: 'Mount the SFTP folder using your SSH client or SSHFS. Verify the server’s host key there, then choose the mounted folder.',
    nfsHelp: 'Mount the NFS export on your computer, then choose the mounted folder.',
    webdavHelp: 'Connect to your WebDAV folder over HTTPS in your file manager, then choose its mounted folder.',
    mirrorHelp: 'Choose the folder your sync tool already manages. To continue a fetch elsewhere, pause on the first computer and let both copies finish syncing. Lite confirms delivery when the other computer acknowledges the recordings.',
    followTip: 'Follows the last character saved by WoW. Choose a saved profile to fetch for that character.',
    refreshCharacter: 'Refresh character',
    characterSyncHint: 'Type /reload in WoW chat to save the character you’re playing.',
    checkingSavedCharacter: 'Checking WoW’s saved character…',
    lastSavedCharacter: 'Current character updated to {name}.',
    sameSavedCharacter: 'WoW’s latest save still shows {name}. If you switched characters, type /reload in WoW chat, wait for the game to return, then refresh here.',
    refreshedPinnedCharacter: 'WoW last saved {name}. You have {selected} selected. Choose Current character to follow WoW.',
    noSavedCharacter: 'No saved WoW character found. Type /reload in WoW chat, wait for the game to return, then refresh here.',
    fetchTip: 'Adds missing name recordings for the selected character and area. Saved recordings are reused.',
    coverageTip: 'Counts saved recordings for this character and area. You can check coverage without contacting the speech provider.',
    pauseTip: 'Pauses requests and processing at the next safe point. Resume Fetch continues later.',
    stopTip: 'Ends the current fetch at the next safe point. Saved recordings are kept.',
    areaTip: 'Limits both Fetch Names and Check Coverage to the area you choose.',
    wavTip: 'Keeps the original audio returned by the provider for later review. These files use additional disk space.',
    fetchFor: 'Fetch Names for {name}',
    fetch: 'Fetch Names',
    coverageCount: {one: '{ready} of {total} name-delimited dialogue', other: '{ready} of {total} name-delimited dialogues'},
    savedCount: {one: 'fetched recording', other: 'fetched recordings'},
    checkedDate: 'Checked {date}',
    lastChecked: 'Last checked {date}',
    levelValue: 'Level {level}',
    race_human: 'Human', race_orc: 'Orc', race_dwarf: 'Dwarf', race_nightelf: 'Night Elf',
    race_undead: 'Undead', race_tauren: 'Tauren', race_gnome: 'Gnome', race_troll: 'Troll',
    race_skyborne: 'Skyborne', race_highorderskyborne: 'High Order Skyborne', race_windshaperskyborne: 'Windshaper Skyborne',
    gender_male: 'Male', gender_female: 'Female',
    class_warrior: 'Warrior', class_mage: 'Mage', class_warlock: 'Warlock', class_hunter: 'Hunter',
    class_paladin: 'Paladin', class_shaman: 'Shaman', class_priest: 'Priest', class_rogue: 'Rogue', class_druid: 'Druid',
    savedName: 'Saved name',
    alliance: 'Alliance',
    horde: 'Horde',
    neutral: 'Neutral',
    noProfiles: 'No characters linked yet.',
    rosterError: 'Could not read saved character profiles. {error}',
  };
  Object.assign(english, window.DMSpeaksMenuHelpEnglish);
  function localeFor(tag) {
    if (typeof tag !== 'string' || !tag) return null;
    try { return new Intl.Locale(tag); } catch { return null; }
  }
  const packs = new Map();
  for (const [tag, pack] of Object.entries(window.DMSpeaksLocales || {})) {
    const locale = localeFor(tag);
    if (locale && pack?.messages) packs.set(locale.baseName, pack);
  }
  const page = document.documentElement;
  const userPreferences = [page.dataset?.uiLocale, ...(navigator.languages || []), navigator.language]
    .map(localeFor).filter(Boolean);
  const preferences = [...userPreferences, localeFor(page.lang), localeFor('en')].filter(Boolean);
  function candidates(locale) {
    const tags = [locale.baseName];
    let tag = locale.baseName;
    while (tag.includes('-')) {
      tag = tag.slice(0, tag.lastIndexOf('-'));
      if (tag !== locale.language) tags.push(tag);
    }
    if (!locale.script && locale.region) {
      const script = locale.maximize().script;
      if (script) tags.push(locale.language + '-' + script);
    }
    tags.push(locale.language);
    return [...new Set(tags)];
  }
  const preference = preferences.find(locale => candidates(locale).some(tag => tag === 'en' || packs.has(tag)));
  const tags = candidates(preference).filter(tag => packs.has(tag));
  const language = tags[0] || 'en';
  const layers = tags.map(tag => ({...packs.get(tag), rules: new Intl.PluralRules(tag)}));
  layers.push({messages: english, rules: new Intl.PluralRules('en')});
  const direction = layers.find(pack => ['ltr', 'rtl'].includes(pack.direction))?.direction;
  const locale = new Intl.Locale(language);
  page.lang = language;
  page.dir = direction || (locale.getTextInfo?.() || locale.textInfo)?.direction || 'ltr';
  const regionalPreference = userPreferences.includes(preference) ? preference : userPreferences[0] || preference;
  const formatLocale = regionalPreference.toString();
  const numbers = new Intl.NumberFormat(formatLocale);
  const dates = new Intl.DateTimeFormat(formatLocale);
  const dateTimes = new Intl.DateTimeFormat(formatLocale, {dateStyle: 'medium', timeStyle: 'short'});
  const lists = new Intl.ListFormat(formatLocale, {style: 'short', type: 'unit'});
  const units = Object.fromEntries(['hour', 'minute', 'second', 'millisecond'].map(unit =>
    [unit, new Intl.NumberFormat(formatLocale, {style: 'unit', unit, unitDisplay: 'short', maximumFractionDigits: 1})]));
  function duration(seconds) {
    if (seconds > 0 && seconds < .1) return units.millisecond.format(Math.max(1, Math.round(seconds * 1000)));
    const tenths = Math.round(seconds * 10);
    const hours = Math.floor(tenths / 36000);
    const minutes = Math.floor(tenths / 600) % 60;
    const remainder = tenths % 600 / 10;
    const parts = [];
    if (hours) parts.push(units.hour.format(hours));
    if (minutes) parts.push(units.minute.format(minutes));
    if (remainder || !parts.length) parts.push(units.second.format(remainder));
    return lists.format(parts);
  }

  function message(key, values = {}, count = values.count) {
    for (const {messages, rules} of layers) {
      if (!Object.hasOwn(messages, key)) continue;
      let pattern = messages[key];
      if (pattern && typeof pattern === 'object') pattern = pattern[rules.select(count)] ?? pattern.other;
      if (typeof pattern !== 'string') continue;
      return pattern.replace(/\{(\w+)\}/g, (original, name) => {
        if (!Object.hasOwn(values, name) || values[name] == null) return original;
        return typeof values[name] === 'number' ? numbers.format(values[name]) : String(values[name]);
      });
    }
  }
  function formatError(result, fallbackKey = 'saveFailed') {
    const translated = result?.error_code && message('error.' + result.error_code, result.error_params || {});
    return translated || result?.error || message(fallbackKey);
  }
  for (const [attribute, target] of [['data-msg', null], ['data-aria-msg', 'aria-label'], ['data-placeholder-msg', 'placeholder']]) {
    for (const element of document.querySelectorAll('[' + attribute + ']')) {
      const value = message(element.getAttribute(attribute));
      if (value === undefined) continue;
      if (target) element.setAttribute(target, value);
      else element.textContent = value;
    }
  }
  return {message, formatError, duration, number: value => numbers.format(value), list: values => lists.format(values),
    date: value => dates.format(value), dateTime: value => dateTimes.format(value)};
})();
