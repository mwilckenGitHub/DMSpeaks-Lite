'use strict';

const {message, number} = liteLocale;
const element = id => document.getElementById(id);
const tokenMeta = document.querySelector('meta[name="lite-token"]');
const sessionScope = document.querySelector('meta[name="lite-session-scope"]')?.content;
let renewingSession;
const prefix = '/api/spoken/player-names/';
const setup = element('setup-form');
const engine = element('engine');
const coverage = element('coverage');
const sshFields = ['label', 'ssh_host', 'ssh_port', 'remote_host', 'remote_port', 'ssh_key'];
const sshDefaults = { ssh_port: '22', remote_host: '127.0.0.1', remote_port: '8091' };
const credentialLabels = { fish: message('fishToken'), azure: message('azureKey'), google: message('googleToken') };
let profiles = [];
let engines = {};
let options = {};
let catalogTimer;
let gameCharacter = null;
let linkedCharacter = null;
let selectedProfile = null;
let roster = [];
let catalogReady = false;
let activeFetch = null;
let fetchWorking = false;
let fetchState = 'idle';
let fetchHalting = false;
let fetchPending = false;
let fetchRevision = 0;
let recordingReview;
let pollingFetch = false;
let readingGameOptions;
let fetchError = '';
let coveragePending = false;
let setupEdited = false;
let setupSaving = false;
let areaSaving = false;
let recordingPlaybackPreference = false;
let dialogueCoverageScope;
let dialogueProgressRevision;
let progressCheckedAt = -Infinity;
const needsKey = value => ['fish', 'elevenlabs', 'openai', 'azure', 'google'].includes(value);
function tell(id, value) {
  const target = element(id);
  if (target.textContent !== value) target.textContent = value;
}
function scopeKey() {
  return JSON.stringify([gameCharacter?.id, selectedProfile, options.provider_id, options.coverage]);
}

function fetchSelection() {
  return {provider_id: options.provider_id, coverage: options.coverage, allow_metered: options.allow_metered,
    character_id: gameCharacter.id, profile_id: selectedProfile};
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function storageChips(storage = {state: 'local'}) {
  const group = node('div', 'storage-chips');
  const label = {local: 'localOnly', synced: 'folderSynced', pending: 'syncPending', previous: 'lastSynced'};
  group.append(node('span', 'storage-chip chip-' + storage.state, message(label[storage.state])));
  if (storage.nas && storage.medium !== 'nas') group.append(node('span', 'storage-chip medium-nas', message('nas')));
  const medium = message(storage.medium);
  if (medium) group.append(node('span', 'storage-chip chip-medium medium-' + storage.medium, medium));
  if (storage.via_tailscale && storage.medium !== 'tailscale') group.append(node('span', 'storage-chip medium-tailscale', message('tailscale')));
  if (storage.cached) group.append(node('span', 'storage-chip chip-location', message('availableOffline')));
  if (storage.location) group.append(node('span', 'storage-chip chip-location', storage.location));
  if (storage.path) group.title = storage.path;
  return group;
}

// Forever uses the legacy realm field for the second part of a character's name.
function fullName(player) {
  return player.realm && !player.name.includes(' ') ? message('fullName', player) : player.name;
}
function accountLabel(account) {
  return account.name || message('accountNumber', {number: number(account.number)});
}
function profilePortrait(player) {
  const portrait = node('img', 'portrait');
  const fallback = player.fallback_portrait;
  portrait.loading = 'lazy';
  if (player.portrait && fallback && player.portrait !== fallback) {
    portrait.addEventListener('error', () => { portrait.src = fallback; }, {once: true});
  }
  if (player.portrait || fallback) portrait.src = player.portrait || fallback;
  portrait.alt = '';
  portrait.width = portrait.height = 52;
  portrait.hidden = !player.portrait && !fallback;
  return portrait;
}
function profileTraits(player) {
  return [
    Number.isInteger(player.level) && player.level > 0 && message('levelValue', {level: number(player.level)}),
    ...['race', 'gender', 'class'].map(field => {
      const value = player[field];
      if (!value) return '';
      const key = field + '_' + value.toLowerCase().replace(/[ _-]/g, '');
      return message(key) || value;
    }),
  ].filter(Boolean);
}
function profileCard(player) {
  const card = node('li', player.linked ? 'profile-card current-profile' : 'profile-card');
  const faction = message(player.faction);
  const identity = node('div', 'profile-identity');
  const name = node('h3', '', fullName(player));
  name.dir = 'auto';
  identity.append(name, node('p', 'profile-meta',
    [faction, player.region?.toUpperCase(), player.ruleset].filter(Boolean).join(' · ') || message('savedName')));
  const traits = profileTraits(player);
  if (traits.length) identity.append(node('p', 'profile-traits', traits.join(' · ')));
  const accounts = (player.accounts || []).map(a => a.name).filter(Boolean);
  if (accounts.length) identity.append(node('p', 'profile-account', message('cardAccount', {accounts:liteLocale.list(accounts)})));
  const chips = storageChips(player.storage);
  chips.id = 'profile-storage-' + player.id;
  identity.append(chips);
  const summary = node('div', 'profile-count');
  const coverage = player.coverage;
  if (coverage) {
    const when = new Date(coverage.checked_at * 1000);
    summary.append(node('strong', '', coverageLabel(coverage)),
      node('small', '', message('checkedDate', {date: liteLocale.date(when)})));
    summary.title = liteLocale.dateTime(when);
  } else {
    summary.append(node('strong', '', number(player.count)),
      node('span', '', message('savedCount', {}, player.count)));
  }
  if (player.selectable) {
    const pick = node('input', 'profile-radio');
    pick.type = 'radio';
    pick.name = 'fetch-character';
    pick.value = player.id;
    pick.checked = selectedProfile === player.id;
    pick.setAttribute('aria-label', [player.name, player.realm, faction].filter(Boolean).join(' · '));
    summary.id = 'profile-count-' + player.id;
    pick.setAttribute('aria-describedby', chips.id + ' ' + summary.id);
    if (player.storage?.path) pick.title = player.storage.path;
    pick.addEventListener('change', () => selectProfile(player.id));
    card.append(pick);
  }
  card.append(profilePortrait(player), identity, summary);
  return card;
}

function displayTarget() {
  const picked = selectedProfile ? roster.find(row => row.id === selectedProfile && row.selectable) : linkedCharacter;
  showCharacter(picked);
}

function selectProfile(id) {
  fetchRevision++;
  selectedProfile = id;
  tell('character-sync-status', '');
  displayTarget();
  showSavedCoverage(roster.find(row => row.id === gameCharacter?.id)?.coverage);
  tell('fetch-status', '');
  tell('fetch-remaining', '');
  tell('fetch-reload', '');
  element('fetch-progress-label').hidden = true;
  element('fetch-progress').hidden = true;
  void pollFetch();
}

element('follow-character').addEventListener('change', () => selectProfile(null));

function setLinkedCharacter(player) {
  if (linkedCharacter?.id !== player?.id && !refreshCharacter.disabled) tell('character-sync-status', '');
  linkedCharacter = player;
  displayTarget();
}

const removalDialog = element('profile-removal');
const removalChoices = element('profile-removal-choices');
const removalSelection = element('profile-removal-selection');
const removeAll = element('remove-all-profiles');
let removalCandidate;

function drawRemovedProfiles(profiles) {
  const items = Object.entries(profiles).sort(([, left], [, right]) =>
    fullName(left).localeCompare(fullName(right), document.documentElement.lang));
  const list = element('removed-profile-list');
  element('removed-profiles').hidden = items.length === 0;
  list.replaceChildren(...items.map(([id, profile]) => {
    const row = node('li', '');
    const button = node('button', 'secondary', message('restoreProfile'));
    button.type = 'button';
    button.addEventListener('click', async () => {
      await withStatus(button, element('profile-removal-status'), async () => {
        drawRemovedProfiles(await api('profile-archive', {character_id: id, removed: false}));
        await refreshProfiles({archiveChanged: true});
        element('character-search').focus();
        return message('profileRestored', {name: fullName(profile)});
      });
    });
    row.append(node('span', '', fullName(profile)), button);
    return row;
  }));
}

function updateRemovalChoices() {
  const picked = removalChoices.querySelectorAll('input:checked').length;
  const total = removalChoices.childElementCount;
  removeAll.checked = total > 0 && picked === total;
  removeAll.indeterminate = picked > 0 && picked < total;
  element('confirm-profile-removal').disabled = !picked;
  tell('confirm-profile-removal', message('removeSelectedProfiles', {count: picked}));
}
removeAll.addEventListener('change', () => {
  for (const input of removalChoices.querySelectorAll('input')) input.checked = removeAll.checked;
  updateRemovalChoices();
});
removalChoices.addEventListener('change', updateRemovalChoices);

function openRemovalDialog(profile) {
  if (removalSelection.disabled) return;
  removalCandidate = profile;
  removalSelection.hidden = Boolean(profile);
  if (profile) {
    tell('profile-removal-title', message('removeProfileTitle'));
    tell('profile-removal-description', message('removeProfileDescription', {name: fullName(profile)}));
    tell('confirm-profile-removal', message('removeProfileConfirm'));
    element('confirm-profile-removal').disabled = false;
  } else {
    tell('profile-removal-title', message('removeProfilesTitle'));
    tell('profile-removal-description', message('removeProfilesDescription'));
    removalChoices.replaceChildren(...roster.filter(row => row.selectable).map(row => {
      const label = node('label', '');
      const input = node('input', '');
      input.type = 'checkbox';
      input.value = row.id;
      label.append(input, node('span', '', fullName(row)));
      return label;
    }));
    updateRemovalChoices();
  }
  tell('profile-removal-error', '');
  removalDialog.showModal();
}
element('remove-profiles').addEventListener('click', () => openRemovalDialog(null));
element('remove-profile').addEventListener('click', () => {
  const profile = roster.find(row => row.id === selectedProfile && row.selectable);
  if (profile) openRemovalDialog(profile);
});

element('confirm-profile-removal').addEventListener('click', async event => {
  const profile = removalCandidate;
  const ids = profile ? [profile.id] : Array.from(removalChoices.querySelectorAll('input:checked'), input => input.value);
  if (!ids.length || removalSelection.disabled) return;
  removalSelection.disabled = true;
  await withStatus(event.currentTarget, element('profile-removal-error'), async () => {
    const selection = profile ? {character_id: profile.id} : {character_ids: ids};
    const removed = await api('profile-archive', {...selection, removed: true});
    if (ids.includes(linkedCharacter?.id)) setLinkedCharacter(null);
    roster = roster.filter(row => !ids.includes(row.id));
    if (ids.includes(selectedProfile)) selectProfile(null);
    drawRoster();
    drawRemovedProfiles(removed);
    await refreshProfiles({archiveChanged: true});
    removalDialog.close();
    tell('profile-removal-status', profile ? message('profileRemoved', {name: fullName(profile)})
      : message('profilesRemoved', {count: ids.length}));
    element(profile || element('remove-profiles').hidden ? 'follow-character' : 'remove-profiles').focus();
    return '';
  });
  removalSelection.disabled = false;
});

let rosterRevision;
let readingRoster;
let rosterGeneration = 0;
function drawRoster() {
  element('remove-profiles').hidden = !roster.some(row => row.selectable);
  const list = element('character-list');
  const focused = list.contains(document.activeElement) ? document.activeElement.value : null;
  const query = element('character-search').value.trim().toLocaleLowerCase(document.documentElement.lang);
  const filtered = query ? roster.filter(row => [row.name, row.realm, fullName(row)].some(
    value => value?.toLocaleLowerCase(document.documentElement.lang).includes(query))) : roster;
  list.replaceChildren(...filtered.map(profileCard));
  if (focused) {
    const target = [...list.querySelectorAll('input')].find(input => input.value === focused);
    (target || element('character-search')).focus({preventScroll: true});
  }
  tell('roster-count', number(roster.length));
  element('roster-count').hidden = false;
  tell('search-status', !query ? '' : !filtered.length ? message('deskNoMatches')
    : message('deskSearchMatches', {count: filtered.length, total: roster.length}));
}
element('character-search').addEventListener('input', drawRoster);
function refreshProfiles({archiveChanged = false} = {}) {
  if (archiveChanged) {
    // An earlier poll can still return the roster or archive from before the change.
    rosterGeneration++;
    rosterRevision = undefined;
    readingRoster = null;
  }
  const generation = rosterGeneration;
  readingRoster ??= readProfiles(generation).finally(() => {
    if (generation === rosterGeneration) readingRoster = null;
  });
  return readingRoster;
}
async function readProfiles(generation) {
  try {
    const players = await pollRead('characters');
    if (generation !== rosterGeneration) return;
    const revision = JSON.stringify(players);
    tell('roster-status', players.length ? '' : message('noProfiles'));
    if (revision === rosterRevision) return;
    roster = players;
    if (selectedProfile && !roster.some(row => row.id === selectedProfile && row.selectable)) selectProfile(null);
    drawRoster();
    displayTarget();
    rosterRevision = revision;
    document.dispatchEvent(new Event('lite-roster'));
    try {
      const archive = await pollRead('profile-archive');
      if (generation === rosterGeneration) drawRemovedProfiles(archive);
    } catch (error) {
      if (generation === rosterGeneration) {
        rosterRevision = undefined;
        tell('profile-removal-status', error.message);
      }
    }
  } catch (error) {
    if (generation !== rosterGeneration) return;
    const detail = message('rosterError', {error: error.message});
    tell('roster-status', detail);
    return detail;
  }
}
const npcOrder = new Intl.Collator(document.documentElement.lang, {sensitivity: 'base', numeric: true});
function showZoneNpcs(zone) {
  const section = element('zone-npcs');
  section.hidden = !zone?.label || !Array.isArray(zone.npcs);
  if (section.hidden) return;
  const list = element('zone-npc-list');
  const scroll = element('zone-npc-scroll');
  const zoneKey = JSON.stringify([dialogueCoverageScope, zone.id]);
  if (list.dataset.zone !== zoneKey) {
    list.replaceChildren();
    list.dataset.zone = zoneKey;
    scroll.scrollTop = 0;
  }
  tell('zone-npcs-heading', message('zoneNpcs', {zone: zone.label}));
  const rows = zone.npcs.filter(row => row?.id != null && typeof row.label === 'string' && row.label
    && Number.isSafeInteger(row.ready) && Number.isSafeInteger(row.total) && row.ready >= 0 && row.total >= row.ready)
    .sort((a, b) => Number(b.id === zone.opening_npc_id) - Number(a.id === zone.opening_npc_id)
      || npcOrder.compare(a.label, b.label) || npcOrder.compare(String(a.id), String(b.id)));
  scroll.hidden = !rows.length;
  element('zone-npc-empty').hidden = Boolean(rows.length);
  const existing = new Map([...list.children].map(item => [item.dataset.id, item]));
  for (const [index, npc] of rows.entries()) {
    const key = String(npc.id);
    let item = existing.get(key);
    if (!item) {
      item = node('li', '');
      item.dataset.id = key;
      item.id = 'zone-npc-row-' + key;
      item.append(node('span', 'npc-name'), node('span', 'npc-ready'), node('span', 'npc-speaker', message('currentSpeaker')));
    }
    existing.delete(key);
    const count = message('deskCoverageProgress', {ready: number(npc.ready), total: number(npc.total)});
    if (item.children[0].textContent !== npc.label) item.children[0].textContent = npc.label;
    if (item.children[1].textContent !== count) item.children[1].textContent = count;
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
  }
  for (const obsolete of existing.values()) obsolete.remove();
}
function markZoneSpeaker(npcId) {
  const previous = element('zone-npc-list').querySelector('[aria-current]');
  const next = npcId == null ? null : element('zone-npc-row-' + npcId);
  if (previous !== next) {
    previous?.removeAttribute('aria-current');
    next?.setAttribute('aria-current', 'true');
  }
}
function showDialogueCoverage(progress, checkedAt, live = false) {
  if (!progress || dialogueCoverageScope !== scopeKey()) return;
  const stamp = progress.checked_at ?? checkedAt;
  if (Number.isFinite(stamp)) {
    if (stamp < progressCheckedAt) return;
    progressCheckedAt = stamp;
  } else if (!live && Number.isFinite(progressCheckedAt)) return;
  const revision = JSON.stringify([progress.npc, progress.zone, progress.overall]);
  if (revision === dialogueProgressRevision) return;
  dialogueProgressRevision = revision;
  let visible = false;
  for (const scope of ['npc', 'zone', 'overall']) {
    const group = progress[scope];
    const row = element('dialogue-' + scope);
    const valid = Number.isSafeInteger(group?.ready) && Number.isSafeInteger(group?.total)
      && group.ready >= 0 && group.total >= group.ready && (scope === 'overall' || Boolean(group.label));
    row.hidden = !valid;
    if (!valid) continue;
    const label = scope === 'overall' ? message('overallCoverage', {name: fullName(gameCharacter)})
      : message(scope + 'Coverage', {label: group.label});
    const count = message('deskCoverageProgress', {ready: number(group.ready), total: number(group.total)});
    tell(row.id + '-label', label);
    tell(row.id + '-count', count);
    const meter = element(row.id + '-meter');
    meter.hidden = group.total === 0;
    meter.max = group.total || 1;
    meter.value = group.ready;
    meter.setAttribute('aria-valuetext', count);
    visible = true;
  }
  element('dialogue-coverage').hidden = !visible;
  showZoneNpcs(progress.zone);
}
function showSavedCoverage(summary, checked = false) {
  const block = element('last-coverage');
  const valid = summary && (!summary.provider_id || (summary.provider_id === options.provider_id && summary.area === options.coverage))
    && Number.isSafeInteger(summary.ready) && Number.isSafeInteger(summary.total)
    && summary.ready >= 0 && summary.total >= summary.ready && Number.isFinite(summary.checked_at);
  block.hidden = !valid;
  if (!valid) return;
  tell('coverage-count', message('deskCoverageProgress', {ready:number(summary.ready), total:number(summary.total)}));
  const meter = element('coverage-meter');
  meter.hidden = summary.total === 0;
  meter.max = summary.total || 1;
  meter.value = summary.ready;
  const when = new Date(summary.checked_at * 1000);
  tell('coverage-date', message('lastChecked', {date: liteLocale.dateTime(when)}));
  if (checked || !fetchWorking) showDialogueCoverage(summary.coverage_progress, summary.checked_at);
}
function coverageLabel(summary) {
  return message('coverageCount', {ready: number(summary.ready), total: number(summary.total)}, summary.total);
}
async function renewSettingsSession() {
  if (!sessionScope) return false;
  const page = await fetch('/', {cache: 'no-store', mode: 'same-origin', redirect: 'error',
    signal: AbortSignal.timeout(10000)});
  if (!page.ok) return false;
  const html = new DOMParser().parseFromString(await page.text(), 'text/html');
  const next = html.querySelector('meta[name="lite-token"]')?.content;
  if (!next || html.querySelector('meta[name="lite-session-scope"]')?.content !== sessionScope) return false;
  tokenMeta.content = next;
  return true;
}

async function postSettings(route, body, contentType = 'application/json', signal) {
  const send = token => fetch(prefix + route, {
    method: 'POST', mode: 'same-origin', redirect: 'error', body, signal,
    headers: {'Content-Type': contentType, 'X-Lite-Token': token},
  });
  const sentToken = tokenMeta.content;
  const response = await send(sentToken);
  if (response.status !== 403) return response;
  const rejection = await response.clone().json().catch(() => null);
  if (rejection?.error_code !== 'settings_session_expired') return response;
  // This rejection is issued before the companion reads or applies the requested change.
  if (sentToken === tokenMeta.content) {
    renewingSession ??= renewSettingsSession().finally(() => { renewingSession = undefined; });
    if (!await renewingSession) return response;
  }
  return send(tokenMeta.content);
}

async function api(route, data, {audioType, signal} = {}) {
  let response;
  try {
    response = data === undefined ? await fetch(prefix + route, {signal})
      : await postSettings(route, JSON.stringify(data), 'application/json', signal);
  } catch (error) {
    if (error instanceof TypeError) throw new Error(message('liteDisconnected'), {cause: error});
    throw error;
  }
  const audioError = audioType === 'audio/wav' ? 'deskContextAudioError' : 'deskReviewAudioError';
  if (!response.ok) throw new Error(liteLocale.formatError(await response.json(), audioType ? audioError : 'saveFailed'));
  if (audioType) {
    const blob = await response.blob();
    const expected = Array.isArray(audioType) ? audioType : [audioType];
    if (!expected.includes(blob.type.split(';')[0]) || !blob.size) throw new Error(message(audioError));
    return blob;
  }
  return response.json();
}

function readGameOptions() {
  readingGameOptions ??= pollRead('game-options').finally(() => { readingGameOptions = null; });
  return readingGameOptions;
}

async function pollRead(route, data) {
  const signal = AbortSignal.timeout(15000);
  try {
    return await api(route, data, {signal});
  } catch (error) {
    if (signal.aborted) throw new Error(message('pollTimedOut'), {cause: error});
    throw error;
  }
}

const refreshCharacter = element('refresh-character');
refreshCharacter.addEventListener('click', () => withStatus(refreshCharacter, element('character-sync-status'), async () => {
  const previous = linkedCharacter?.id;
  // A background poll may already have read the file before the user clicked.
  if (readingGameOptions) await Promise.allSettled([readingGameOptions]);
  const state = await readGameOptions();
  setLinkedCharacter(state.game_character);
  if (readingRoster) await readingRoster;
  const rosterError = await refreshProfiles();
  if (rosterError) return rosterError;
  if (linkedCharacter?.id === state.game_character?.id && state.provider_id === options.provider_id && state.coverage === options.coverage) {
    showSavedCoverage(selectedProfile ? gameCharacter?.coverage : state.last_coverage);
  }
  if (!linkedCharacter) return message('noSavedCharacter');
  const name = fullName(linkedCharacter);
  if (selectedProfile && gameCharacter && selectedProfile !== linkedCharacter.id) {
    return message('refreshedPinnedCharacter', {name, selected: fullName(gameCharacter)});
  }
  return message(previous === linkedCharacter.id ? 'sameSavedCharacter' : 'lastSavedCharacter', {name});
}, message('checkingSavedCharacter')));

async function withStatus(button, status, operation, pending = '') {
  button.disabled = true;
  status.textContent = pending;
  try {
    const message = await operation();
    status.textContent = message;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

function pastedPath(value) {
  const trimmed = value.trim();
  // File Explorer's Copy as path surrounds the path with double quotes.
  if (trimmed.length > 1 && trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function providerId(value) {
  if (value === 'fish') return 'fish-s21-free';
  if (value.startsWith('lite-')) return value;
  return value && value !== 'fish_omni' ? 'lite-' + value : null;
}

function engineType(value) {
  return value.startsWith('lite-') ? 'fish_omni' : value;
}

function updateProviderChoices(preferred) {
  const local = profiles.filter(profile => profile.engine === 'fish_omni' && profile.transport === 'ssh'
    && (profile.id === 'lite-fish_omni' || /^lite-local-[0-9a-f]{32}$/.test(profile.id)));
  const rows = Object.entries(engines).filter(([value]) => value !== 'fish_omni')
    .map(([value, label]) => new Option(message('provider.' + value) || label, value));
  engine.replaceChildren(new Option(message('chooseProvider'), ''), ...rows);
  if (Object.hasOwn(engines, 'fish_omni')) {
    const group = document.createElement('optgroup');
    group.label = message('personalProviders');
    group.append(...local.map(profile => new Option(profile.label || profile.ssh_host, profile.id)),
      new Option(message('addPersonalProvider'), 'fish_omni'));
    engine.append(group);
  }
  const choice = preferred && [...engine.options].find(option => option.value && providerId(option.value) === preferred);
  engine.value = choice?.value || (Object.hasOwn(engines, 'fish') ? 'fish' : '');
  return choice;
}

function mayBeMetered(value) {
  return Boolean(value && value !== 'fish' && value !== 'fish_omni');
}

function showProvider() {
  const selected = engineType(engine.value);
  const saved = profiles.find(profile => profile.id === providerId(engine.value)) || {};
  const participating = new Set();
  if (selected === 'fish_omni') sshFields.forEach(name => participating.add(name));
  if (selected === 'fish') participating.add('api_key');
  const usesVoiceMappings = mayBeMetered(selected);
  if (usesVoiceMappings && selected !== 'polly') participating.add('api_key');
  if (['elevenlabs', 'openai', 'polly'].includes(selected)) participating.add('model');
  if (['azure', 'polly'].includes(selected)) participating.add('region');
  if (selected === 'google') participating.add('quota_project');
  if (selected === 'polly') participating.add('aws_profile');
  for (const wrapper of setup.querySelectorAll('[data-provider-field]')) {
    const input = wrapper.querySelector('input');
    const shown = participating.has(input.name);
    wrapper.hidden = !shown;
    input.disabled = !shown;
    input.required = shown && input.name === 'label';
    input.value = input.name === 'api_key' ? '' : (saved[input.name] || sshDefaults[input.name] || '');
  }
  element('credential-label').textContent = credentialLabels[selected] || message('providerApiKey');
  const credential = setup.elements.api_key;
  const credentialSaved = !credential.disabled && saved.has_credentials;
  const length = saved.credential_length;
  credential.placeholder = credentialSaved && Number.isInteger(length) && length > 0 && length <= 8192
    ? '•'.repeat(length) : '';
  if (credentialSaved) {
    credential.setAttribute('aria-description', message('credentialDescription'));
  } else {
    credential.removeAttribute('aria-description');
  }
  element('credential-status').textContent = credentialSaved
    ? message('savedCredential')
    : message('noCredential');
  element('voice-settings').hidden = !usesVoiceMappings;
  setup.elements.npc_voices.disabled = !usesVoiceMappings;
  setup.elements.npc_voices.value = saved.npc_voices || '';
  element('test').disabled = !saved.id;
  element('remove-connection').hidden = selected !== 'fish_omni' || !saved.id;
  const help = element('provider-help');
  help.hidden = !selected;
  const helpByProvider = {
    fish: message('fishHelp'),
    fish_omni: message('selfHostedHelp'),
  };
  help.textContent = helpByProvider[selected] || message('providerHelp');
  updateFetchButtons();
}

function updateFetchButtons() {
  const selected = providerId(engine.value);
  const unsaved = !engine.value || selected !== options.provider_id || coverage.value !== options.coverage;
  const progressScope = unsaved || !gameCharacter ? null : scopeKey();
  if (progressScope !== dialogueCoverageScope) {
    dialogueCoverageScope = progressScope;
    clearFetchTiming();
    dialogueProgressRevision = undefined;
    progressCheckedAt = -Infinity;
    element('dialogue-coverage').hidden = true;
    element('zone-npcs').hidden = true;
    element('fetch-progress-label').hidden = true;
    element('fetch-progress').hidden = true;
    tell('fetch-remaining', '');
  }
  if (unsaved) showSavedCoverage(null);
  const saved = profiles.find(profile => profile.id === selected);
  const needsCredential = needsKey(engineType(engine.value)) && !saved?.has_credentials;
  const activeProvider = [...engine.options].find(option => option.value && providerId(option.value) === options.provider_id);
  tell('selection-summary', activeProvider?.text || message('chooseProvider'));
  const busy = fetchState === 'busy';
  element('remove-connection').disabled = busy || fetchWorking || fetchPending || setupSaving;
  coverage.disabled = busy || areaSaving || setupSaving || fetchPending || fetchWorking || coveragePending;
  element('fetch-names').disabled = busy || coveragePending || fetchPending || fetchWorking || !gameCharacter || !catalogReady
    || unsaved || setupEdited || !saved || needsCredential
    || (mayBeMetered(engineType(engine.value)) && options.allow_metered !== true);
  const fetchButton = element('fetch-names');
  const pauseButton = element('pause-fetch');
  const stopButton = element('stop-fetch');
  const fetchLabel = fetchState === 'paused' ? 'resumeFor' : 'startFor';
  tell('fetch-names', gameCharacter ? message(fetchLabel, {name: gameCharacter.name}) : message('fetch'));
  fetchButton.hidden = fetchWorking;
  pauseButton.hidden = !fetchWorking;
  pauseButton.disabled = fetchPending || fetchHalting || !activeFetch;
  stopButton.hidden = !fetchWorking && fetchState !== 'paused';
  stopButton.disabled = fetchPending || fetchHalting === 'stop' || !activeFetch;
  element('check-coverage').disabled = busy || coveragePending || fetchPending || fetchWorking || !gameCharacter
    || !catalogReady || unsaved || !saved;
  element('npc-check-hint').hidden = element('check-coverage').disabled || !element('zone-npcs').hidden;
  tell('fetch-readiness', !gameCharacter ? ''
    : !catalogReady ? message('waitingCatalog')
    : unsaved || !saved || setupEdited ? message('saveToFetch')
    : needsCredential ? message('credentialNeeded')
    : mayBeMetered(engineType(engine.value)) && options.allow_metered !== true ? message('consentNeeded')
    : '');
  element('recording-review').hidden = !gameCharacter;
  recordingReview?.sync();
}

let selectedDetailsRevision;
function showCharacter(character) {
  if (gameCharacter?.id !== character?.id) {
    tell('coverage-status', '');
    fetchError = '';
    activeFetch = null;
    fetchWorking = false;
    fetchState = 'idle';
    fetchHalting = false;
  }
  gameCharacter = character || null;
  const details = character && (roster.find(row => row.id === character.id) || character);
  const revision = JSON.stringify(details);
  if (selectedDetailsRevision !== revision) {
    selectedDetailsRevision = revision;
    element('selected-character').hidden = !details;
    if (details) {
      tell('fetch-character', fullName(details));
      const accounts = (details.accounts || []).map(account => account.name).filter(Boolean);
      const traits = [...profileTraits(details), message(details.faction), details.region?.toUpperCase(), details.ruleset];
      if (accounts.length) traits.push(message('cardAccount', {accounts:liteLocale.list(accounts)}));
      tell('selected-meta', traits.filter(Boolean).join(' · '));
      element('selected-portrait').replaceChildren(profilePortrait(details));
      element('selected-storage').replaceChildren(...(details.storage ? [storageChips(details.storage)] : []));
    }
    const count = details?.count;
    tell('saved-recording-count', Number.isSafeInteger(count) ? number(count) : '');
    element('saved-recording-count').hidden = !Number.isSafeInteger(count);
  }
  element('character-hint').hidden = Boolean(gameCharacter);
  element('remove-profile').hidden = !details?.selectable || selectedProfile !== details.id;
  updateFetchButtons();
}

let timingRevision;
function clearFetchTiming() {
  timingRevision = undefined;
  element('fetch-timing').hidden = true;
  element('fetch-history').open = false;
  element('fetch-history-list').replaceChildren();
}

function dialogueTimeRow(dialogue) {
  const source = dialogue.source_id || dialogue.source_key || '';
  const quest = /^q:(\d+):/.exec(source);
  const label = [dialogue.npc, dialogue.stage, quest ? message('fetchTimeQuest', {id: quest[1]}) : source];
  if (dialogue.already_ready) label.push(message('fetchTimeSaved'));
  else if (dialogue.state === 'source_unavailable') label.push(message('fetchTimeSourceUnavailable'));
  else if (dialogue.state === 'held') label.push(message('fetchTimeHeld'));
  const row = node('li', '');
  row.append(node('span', '', label.filter(Boolean).join(' · ')),
    node('span', 'duration', liteLocale.duration(dialogue.elapsed_seconds)));
  return row;
}

function fetchPassRow(pass, index, scope) {
  const label = message('fetchTimePass', {index});
  const title = Number.isFinite(pass.started_at)
    ? message('fetchTimeWhen', {pass: label, date: liteLocale.dateTime(new Date(pass.started_at * 1000))}) : label;
  const scopeLabel = {replacement: 'fetchTimeReplacement', selected: 'fetchTimeSelected'}[pass.fetch_scope];
  const outcome = {paused: 'fetchTimePaused', stopped: 'fetchTimeStopped', unavailable: 'fetchTimeFailed'}[pass.state];
  const count = Number.isSafeInteger(pass.ready) && Number.isSafeInteger(pass.total)
    ? message('fetchTimeCoverage', {ready: pass.ready, total: pass.total}) : '';
  const caption = [title, scopeLabel && message(scopeLabel), message('fetchPassElapsed', {duration: liteLocale.duration(pass.elapsed_seconds)}),
    outcome && message(outcome), count].filter(Boolean).join(' · ');
  const item = node('li', '');
  if (!pass.dialogue_count) {
    item.textContent = caption;
    return item;
  }
  const details = node('details', '');
  const status = node('p', 'help');
  status.setAttribute('role', 'status');
  const npcHeading = node('h4', '', message('fetchTimeSpeakers'));
  const npcRows = node('ul', 'dialogue-times');
  npcRows.setAttribute('aria-label', message('fetchTimeSpeakers'));
  const dialogueHeading = node('h4', '', message('fetchTimeDialogues'));
  npcHeading.hidden = npcRows.hidden = dialogueHeading.hidden = true;
  const rows = node('ul', 'dialogue-times');
  rows.tabIndex = 0;
  rows.setAttribute('aria-label', title);
  rows.hidden = true;
  details.append(node('summary', '', caption), status, npcHeading, npcRows, dialogueHeading, rows);
  let loading = false;
  details.addEventListener('toggle', async () => {
    if (!details.open || loading || !rows.hidden || scope !== scopeKey()) return;
    loading = true;
    status.textContent = message('fetchTimeLoading');
    try {
      const data = await api('fetch', {...fetchSelection(), action: 'timing', request_id: pass.request_id});
      if (!details.isConnected || scope !== scopeKey()) return;
      const speakers = data.npc_times || [];
      npcRows.replaceChildren(...speakers.map(speaker => {
        const row = node('li', '');
        row.append(node('span', '', speaker.npc), node('span', 'duration', liteLocale.duration(speaker.elapsed_seconds)));
        return row;
      }));
      npcRows.hidden = npcHeading.hidden = !speakers.length;
      dialogueHeading.hidden = false;
      rows.replaceChildren(...data.dialogues.filter(row => Number.isFinite(row.elapsed_seconds)).map(dialogueTimeRow));
      rows.hidden = false;
      status.hidden = true;
    } catch (error) {
      status.textContent = error.message;
    } finally {
      loading = false;
    }
  });
  item.append(details);
  return item;
}

function showFetchTiming(result) {
  const timing = result.timing;
  const scope = scopeKey();
  if (dialogueCoverageScope !== scope || !Array.isArray(timing?.passes)) return;
  const passes = timing.passes;
  const running = ['running', 'queued'].includes(result.state);
  const visible = running || passes.length > 0 || timing.incomplete_history;
  element('fetch-timing').hidden = !visible;
  if (!visible) return;
  const elapsed = running ? result.pass_elapsed_seconds : passes.at(-1)?.elapsed_seconds;
  const labels = [];
  if (running && result.npc_time?.npc && Number.isFinite(result.npc_time.elapsed_seconds)) {
    labels.push(message('fetchNpcElapsed', {npc: result.npc_time.npc, duration: liteLocale.duration(result.npc_time.elapsed_seconds)}));
  }
  if (Number.isFinite(elapsed)) {
    labels.push(message('fetchPassElapsed', {duration: liteLocale.duration(elapsed)}));
  }
  if (timing.incomplete_history) labels.push(message('incompleteTiming'));
  tell('fetch-time-summary', labels.join(' · '));
  const revision = JSON.stringify(passes);
  if (timingRevision === revision) return;
  timingRevision = revision;
  element('fetch-history').hidden = !passes.length;
  tell('fetch-history-heading', message('fetchTimeHistory', {count: passes.length}));
  const list = element('fetch-history-list');
  const existing = new Map([...list.children].map(item => [item.dataset.pass, item]));
  passes.slice().reverse().forEach((pass, index) => {
    const key = JSON.stringify(pass);
    const item = existing.get(key) || fetchPassRow(pass, passes.length - index, scope);
    item.dataset.pass = key;
    existing.delete(key);
    if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
  });
  for (const obsolete of existing.values()) obsolete.remove();
}

function showFetch(result, replacementStarted = false) {
  showFetchTiming(result);
  const checked = result.newer_area_coverage;
  if (checked) result = {state: 'idle', coverage_progress: checked.coverage_progress};
  const wasWorking = fetchWorking;
  fetchState = result.state;
  fetchWorking = ['queued', 'running'].includes(fetchState);
  fetchHalting = result.stopping ? 'stop' : result.pausing ? 'pause' : false;
  activeFetch = (fetchWorking || fetchState === 'paused') && result.job_id ? result.job_id : null;
  const hasCount = result.dialogue_number && result.total;
  const idle = checked ? message('latestAreaCheck', {ready: number(checked.ready), total: number(checked.total)}, checked.total)
    : message('idle');
  const stateMessage = { paused: message('paused'), complete: message('complete'), idle };
  // Older saved pause messages used the previous button label after any reload guidance.
  const savedDetail = result.state === 'paused'
    ? result.detail?.replace(/Click (?:Fetch Names|Queue Audio Fetch)(?: in WoW)? to continue\./,
      () => message('resumeHint'))
    : result.detail?.replace('The previous request is held. Review it before retrying.',
      () => message('heldRetryHint'));
  const scopedFinish = ['complete', 'unavailable', 'preview'].includes(result.state)
    && Number.isSafeInteger(result.ready) && Number.isSafeInteger(result.total) && result.total > 0
    && result.ready >= 0 && result.ready <= result.total;
  const remaining = scopedFinish ? result.total - result.ready : 0;
  let summaryKey = remaining ? 'fetchPassFinished' : '';
  if (scopedFinish && result.fetch_scope === 'selected') summaryKey = 'selectedFetchFinished';
  if (scopedFinish && result.fetch_scope === 'replacement') {
    summaryKey = result.ready > 0 ? 'replacementSaved' : 'replacementKept';
  }
  const passSummary = summaryKey ? message(summaryKey, {ready: number(result.ready), total: number(result.total)}, result.total) : '';
  const detail = result.stopping ? message('stopping') : result.pausing ? message('pausing')
    : result.state === 'idle' ? stateMessage.idle : savedDetail || stateMessage[result.state];
  let status = passSummary || detail || (fetchWorking ? message('prepareNames') : '');
  if (fetchWorking && Number.isSafeInteger(result.retake_attempt) && Number.isSafeInteger(result.retake_total)
      && result.retake_attempt > 0 && result.retake_attempt <= result.retake_total) {
    const retry = message('autoRetakeNumber', {attempt: result.retake_attempt, total: result.retake_total});
    status = [retry, status].filter(Boolean).join(' · ');
  }
  tell('fetch-status', fetchError || (hasCount ? message('fetchProgress', {
    current: number(result.dialogue_number), total: number(result.total), status,
  }) : status));
  const reload = passSummary ? savedDetail?.match(/^(?:Reload|Restart) WoW [^.]+\./)?.[0] : '';
  const oldReason = passSummary ? savedDetail?.slice(reload?.length || 0).trim() : '';
  const savedReason = result.remaining_reason || oldReason;
  const wasLegacyCut = savedReason === 'No measured phonetic cut fits the prepared slot and preserves its neighboring speech.'
    || savedReason === 'No measured paced Name cut preserves the existing contextual base and fallback.';
  const reason = wasLegacyCut ? message('recheckSavedTakes') : savedReason;
  const blocked = result.source_unavailable || 0;
  const showRemaining = remaining && result.fetch_scope !== 'replacement';
  const remainingText = showRemaining && remaining !== blocked ? message('remainingDialogues', {count: remaining}) : '';
  const blockedText = showRemaining && blocked > 0 ? message('fetchSourceUnavailable', {count: blocked}) : '';
  tell('fetch-remaining', [remainingText, blockedText, reason].filter(Boolean).join(' '));
  const reloadHint = result.reload_hint || reload || '';
  tell('fetch-reload', reloadHint === 'Reload WoW to use your new audio.'
    ? message('loadNewAudio') : reloadHint);
  const meter = element('fetch-progress');
  meter.hidden = !fetchWorking || !Number.isFinite(result.instance_progress) || dialogueCoverageScope !== scopeKey();
  element('fetch-progress-label').hidden = meter.hidden;
  meter.value = result.instance_progress || 0;
  showDialogueCoverage(result.coverage_progress, undefined, fetchWorking);
  const progress = result.coverage_progress;
  const currentZone = element('zone-npc-list').dataset.zone === JSON.stringify([dialogueCoverageScope, progress?.zone?.id]);
  markZoneSpeaker(fetchWorking && currentZone ? progress?.npc?.id : null);
  updateFetchButtons();
  recordingReview?.jobChanged(result, wasWorking, replacementStarted);
}

async function pollFetch() {
  void refreshProfiles();
  if (pollingFetch || fetchPending) return;
  pollingFetch = true;
  let readScope = scopeKey();
  let revision = fetchRevision;
  try {
    const state = await readGameOptions();
    setLinkedCharacter(state.game_character);
    if (!setupEdited && !setupSaving && !areaSaving && (state.provider_id !== options.provider_id
        || state.coverage !== options.coverage || state.allow_metered !== options.allow_metered)) {
      readScope = scopeKey();
      const snapshot = await pollRead('setup');
      if (!setupEdited && !setupSaving && !areaSaving) {
        profiles = snapshot.profiles;
        options = snapshot.options;
        engines = snapshot.engines;
        updateProviderChoices(options.provider_id);
        coverage.value = options.coverage || 'all';
        tell('coverage-status', '');
        showProvider();
      }
    }
    catalogReady = state.preparation.state === 'ready';
    if (!setupEdited && linkedCharacter?.id === state.game_character?.id && state.provider_id === options.provider_id && state.coverage === options.coverage) {
      showSavedCoverage(selectedProfile ? gameCharacter?.coverage : state.last_coverage);
    }
    if (gameCharacter) {
      readScope = scopeKey();
      revision = fetchRevision;
      const result = await pollRead('fetch', { action: 'status', character_id: gameCharacter.id });
      if (!fetchPending && revision === fetchRevision && readScope === scopeKey()) showFetch(result);
    } else {
      showFetch({ state: 'idle' });
    }
  } catch (error) {
    if (!fetchPending && revision === fetchRevision && readScope === scopeKey()) {
      element('fetch-status').textContent = error.message;
    }
  } finally {
    pollingFetch = false;
  }
}

async function refreshCatalog() {
  clearTimeout(catalogTimer);
  const status = element('catalog-status');
  const progress = element('catalog-progress');
  try {
    const { preparation, game_character } = await readGameOptions();
    catalogReady = preparation.state === 'ready';
    setLinkedCharacter(game_character);
    progress.hidden = preparation.state !== 'preparing';
    if (preparation.state === 'preparing') {
      const { stage, current, total } = preparation;
      switch (stage) {
        case 'checking':
          status.textContent = message('checkingZip');
          break;
        case 'opening':
          status.textContent = message('openingCatalog');
          break;
        case 'verifying':
          status.textContent = message('verifyCatalog', {current: number(current), total: number(total)});
          break;
        case 'checking the installed catalog':
          status.textContent = message('checkingCatalog');
          break;
        case 'saving':
          status.textContent = message('savingCatalog');
          break;
        case 'verifying native sources':
          status.textContent = message('verifySources', {current: number(current), total: number(total)});
          break;
        case 'checking playback archive':
          status.textContent = message('checkingPlayback');
          break;
        case 'verifying playback':
          status.textContent = message('verifyPlayback', {current: number(current), total: number(total)});
          break;
        case 'packaging playback':
          status.textContent = message('preparePlayback');
          break;
        case 'installing playback':
          status.textContent = message('installingPlayback');
          break;
        default:
          status.textContent = message('preparingCatalog');
      }
      if (preparation.total) {
        progress.max = preparation.total;
        progress.value = preparation.current;
      } else progress.removeAttribute('value');
      catalogTimer = setTimeout(refreshCatalog, 750);
      return;
    }
    if (preparation.state === 'ready') {
      const { plans } = await pollRead('inputs');
      status.textContent = plans ? message('planCount', {count: number(plans.count)}, plans.count) : message('catalogReady');
    } else {
      element('catalog').open = true;
      element('advanced-settings').open = true;
      status.textContent = preparation.error || message('addCatalog');
    }
  } catch (error) {
    status.textContent = error.message;
  }
}

setup.addEventListener('input', () => { setupEdited = true; updateFetchButtons(); });
engine.addEventListener('change', () => {
  element('setup-status').textContent = '';
  fetchError = '';
  element('coverage-status').textContent = '';
  showProvider();
});
coverage.addEventListener('change', async () => {
  tell('coverage-status', '');
  if (!options.provider_id || providerId(engine.value) !== options.provider_id || setupEdited) {
    setupEdited = true;
    updateFetchButtons();
    return;
  }
  if (areaSaving || setupSaving || coverage.value === options.coverage) return;
  areaSaving = true;
  updateFetchButtons();
  try {
    options = await api('game-options', {provider_id: options.provider_id, coverage: coverage.value,
      allow_metered: options.allow_metered});
    showSavedCoverage(null);
  } catch (error) {
    coverage.value = options.coverage;
    tell('coverage-status', error.message);
  } finally {
    areaSaving = false;
    updateFetchButtons();
    void pollFetch();
  }
});

setup.addEventListener('submit', async event => {
  event.preventDefault();
  if (areaSaving || setupSaving) return;
  const selected = engineType(engine.value);
  const id = providerId(engine.value);
  const selectedCoverage = coverage.value;
  setupSaving = true;
  updateFetchButtons();
  await withStatus(element('save'), element('setup-status'), async () => {
    const metered = mayBeMetered(selected);
    const alreadyAllowed = options.provider_id === id && options.allow_metered === true;
    const allowMetered = metered && (alreadyAllowed || window.confirm(
      message('meteredPrompt')));
    const fields = Object.fromEntries([...new FormData(setup)].filter(([name]) => name !== 'coverage').map(
      ([name, value]) => [name, name === 'ssh_key' ? pastedPath(value) : value.trim()])
      .filter(([, value]) => value));
    const profile = await api('provider', { engine: selected, id, ...fields });
    profiles = [...profiles.filter(item => item.id !== profile.id), profile];
    setup.elements.api_key.value = '';
    options = await api('game-options', { provider_id: profile.id, coverage: selectedCoverage, allow_metered: allowMetered });
    updateProviderChoices(options.provider_id);
    setupEdited = false;
    showProvider();
    return metered && !allowMetered ? message('fetchDisabled') : message('savedSettings');
  });
  setupSaving = false;
  updateFetchButtons();
});

element('remove-connection').addEventListener('click', async () => {
  const button = element('remove-connection');
  const profile = profiles.find(item => item.id === providerId(engine.value));
  if (!profile || button.disabled || !window.confirm(message('removePersonalProviderPrompt', {
    name: profile.label || profile.ssh_host,
  }))) return;
  setupSaving = true;
  updateFetchButtons();
  await withStatus(button, element('setup-status'), async () => {
    options = await api('provider-remove', {provider_id: profile.id});
    profiles = profiles.filter(item => item.id !== profile.id);
    updateProviderChoices(options.provider_id);
    setupEdited = false;
    coverage.value = options.coverage || 'all';
    showProvider();
    return message('personalProviderRemoved');
  });
  setupSaving = false;
  updateFetchButtons();
  void pollFetch();
});

element('test').addEventListener('click', () => withStatus(element('test'), element('setup-status'), async () => {
  const result = await api('provider-check', { provider_id: providerId(engine.value) });
  return result.message || (result.ok ? message('testPass') : message('testFail'));
}));

async function fetchAction(action, replacement = null) {
  if (fetchPending || !gameCharacter || (coveragePending && action === 'start')) return;
  const trigger = element(replacement ? 'review-replace' : {
    start: 'fetch-names', pause: 'pause-fetch', stop: 'stop-fetch'
  }[action]);
  if (trigger.disabled) return;
  const fromButton = document.activeElement === trigger;
  const job = activeFetch;
  const scope = scopeKey();
  const selection = fetchSelection();
  if (replacement) {
    if (!recordingReview?.isCurrent(replacement)) return;
    if (mayBeMetered(engineType(engine.value)) && !window.confirm(message('deskReviewPrompt', {
      number: number(replacement.number), name: fullName(gameCharacter), provider: engine.selectedOptions[0].text,
    }))) return;
    selection.replace_recording = {slot: replacement.slot, sha256: replacement.sha256};
    selection.confirmed_replace = true;
    tell('review-job', message('deskReviewStarting'));
  }
  fetchError = '';
  fetchRevision++;
  fetchPending = true;
  updateFetchButtons();
  element('fetch-status').textContent = message({start: 'starting', pause: 'pausing', stop: 'stopping'}[action]);
  try {
    const result = await api('fetch', { ...selection, action, job_id: job });
    if (scope === scopeKey()) {
      showFetch(result, Boolean(replacement));
    }
  } catch (error) {
    if (scope === scopeKey()) {
      fetchError = element('fetch-status').textContent = error.message;
      if (replacement) recordingReview?.rejected(error.message);
    }
  } finally {
    fetchPending = false;
    updateFetchButtons();
    if (!replacement && fromButton && scope === scopeKey() && document.hasFocus()
        && (document.activeElement === document.body || document.activeElement === trigger)) {
      if (action === 'start' && fetchWorking) {
        element('fetch-activity').focus({preventScroll: true});
      } else {
        const next = element(fetchWorking ? 'pause-fetch' : 'fetch-names');
        if (!next.disabled) next.focus({preventScroll: true});
      }
    }
  }
}

element('fetch-names').addEventListener('click', () => fetchAction('start'));
element('stop-fetch').addEventListener('click', () => fetchAction('stop'));
element('pause-fetch').addEventListener('click', () => fetchAction('pause'));
element('refresh-status').addEventListener('click', async () => {
  if (pollingFetch || fetchPending) return;
  fetchError = '';
  element('fetch-status').textContent = message('refreshing');
  await pollFetch();
});
element('check-coverage').addEventListener('click', async () => {
  if (coveragePending || !gameCharacter) return;
  coveragePending = true;
  updateFetchButtons();
  const status = element('coverage-status');
  const checking = scopeKey();
  status.textContent = message('checking');
  try {
    const job = await api('coverage', { ...options, character_id: gameCharacter.id, profile_id: selectedProfile });
    let result;
    do {
      await new Promise(resolve => setTimeout(resolve, 300));
      result = await api('coverage', { job_id: job.job_id });
      if (result.state !== 'complete' && !['queued', 'running'].includes(result.state)) {
        throw new Error(result.message || message('coverageInterrupted'));
      }
    } while (result.state !== 'complete');
    if (checking === scopeKey()) {
      if (Number.isFinite(result.checked_at) && Number.isSafeInteger(result.total) && Number.isSafeInteger(result.ready)) {
        showSavedCoverage(result, true);
        status.textContent = '';
        void pollFetch();
      } else status.textContent = result.message;
    }
  } catch (error) {
    if (checking === scopeKey()) status.textContent = error.message;
  } finally {
    coveragePending = false;
    updateFetchButtons();
  }
});

const catalogForm = element('catalog-form');
catalogForm.elements.kind.addEventListener('change', () => {
  const playback = catalogForm.elements.kind.value === 'playback';
  element('playback-index-field').hidden = !playback;
  catalogForm.elements.input_sha256.disabled = !playback;
  catalogForm.elements.input_sha256.required = playback;
});
catalogForm.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = Object.fromEntries(new FormData(form));
  data.path = pastedPath(data.path);
  data.sha256 = data.sha256.trim().toLowerCase();
  if (data.input_sha256) data.input_sha256 = data.input_sha256.trim().toLowerCase();
  catalogTimer = setTimeout(refreshCatalog, 300);
  await withStatus(event.submitter, element('import-status'), async () => {
    const result = await api('inputs', data);
    const key = {playback: 'verifiedRecordings', plans: 'verifiedPlans', returns: 'verifiedReturns'}[data.kind];
    return message(key, {count: result.count, details: result.message || ''}).trim();
  });
  await refreshCatalog();
});

const keepWav = element('keep-wav');
keepWav.addEventListener('change', () => withStatus(keepWav, element('preferences-status'), async () => {
  const requested = keepWav.checked;
  try {
    const saved = await api('preferences', { keep_raw_wav: requested });
    keepWav.checked = saved.keep_raw_wav;
    return message('storageSaved');
  } catch (error) {
    keepWav.checked = !requested;
    throw error;
  }
}));

async function initialSettings() {
  for (const delay of [1000, 2500]) {
    try {
      return await pollRead('setup');
    } catch {
      tell('fetch-status', message('startupRetry'));
      await new Promise(resolve => setTimeout(resolve, delay));
    }
  }
  return pollRead('setup');
}

async function initialize() {
  const settings = await initialSettings();
  profiles = settings.profiles;
  options = settings.options;
  coverage.value = options.coverage || 'all';
  setLinkedCharacter(settings.game_character);
  engines = settings.engines;
  const requested = new URLSearchParams(window.location.search).get('provider');
  const preferred = Boolean(requested && updateProviderChoices(requested));
  if (!preferred) updateProviderChoices(options.provider_id);
  // A provider-specific link opens its editor. Ordinary launches lead with the roster,
  // including when the user is only reviewing saved audio without a speech credential.
  if (preferred) {
    element('connection-settings').open = true;
    element('connection-settings').querySelector('summary').scrollIntoView({block:'nearest'});
  }
  element('keep-wav').checked = settings.preferences.keep_raw_wav;
  recordingPlaybackPreference = settings.preferences.recording_playback_enabled === true;
  window.dmsRecordingPlayback?.apply(recordingPlaybackPreference);
  showProvider();
  await refreshCatalog();
  await pollFetch();
  setInterval(pollFetch, 1500);
}

initialize().catch(error => {
  tell('roster-status', message('startupFailed', {error:error.message}));
  tell('fetch-status', message('startupFailed', {error:error.message}));
});
