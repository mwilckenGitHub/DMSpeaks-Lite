'use strict';

recordingReview = (() => {
  const section = element('recording-review');
  const summary = element('review-open');
  const choice = element('review-choice');
  const play = element('review-play');
  const playContext = element('review-play-context');
  const player = element('review-player');
  const replace = element('review-replace');
  const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('dmspeaks-recording-playback') : null;
  const pageSize = 12;
  let enabled = typeof recordingPlaybackPreference === 'boolean' && recordingPlaybackPreference;
  let writePending = false;
  let revision = 0;
  let scope = '';
  let knownCount;
  let items = [];
  let offset = 0;
  let total = 0;
  let listRequest, audioRequest, audioURL, audioMode;
  let replacementJob = '';

  function selected() { return items[choice.selectedIndex]; }
  function profileKey() { return JSON.stringify([gameCharacter?.id, selectedProfile]); }
  function title(item) { return item.caption?.trim() || message('deskReviewNumber', {number: number(item.number)}); }

  function stopAudio() {
    audioRequest?.abort();
    audioRequest = null;
    if (audioURL) {
      player.pause();
      player.removeAttribute('src');
      player.load();
      URL.revokeObjectURL(audioURL);
    }
    audioURL = audioMode = null;
    player.hidden = true;
  }

  function controls() {
    const item = selected();
    play.hidden = play.disabled = !item;
    playContext.hidden = !enabled || !item;
    playContext.disabled = playContext.hidden || audioMode === 'context';
    replace.disabled = !item || Boolean(listRequest) || element('fetch-names').disabled;
    element('review-previous').hidden = !offset;
    element('review-next').hidden = offset + items.length >= total;
    element('review-reload').hidden = !section.open || !gameCharacter;
    if (section.open && gameCharacter) {
      const provider = profiles.find(profile => profile.id === options.provider_id);
      const providerLabel = provider && [...engine.options].find(option => providerId(option.value) === provider.id)?.text;
      tell('review-scope', providerLabel
        ? message('deskReviewScope', {name: fullName(gameCharacter), provider: providerLabel}) : '');
    }
    tell('review-readiness', fetchWorking || fetchPending || coveragePending
      ? message('deskReviewBusy') : element('fetch-readiness').textContent);
  }

  function status() {
    if (!items.length) {
      tell('review-status', scope && gameCharacter ? message('deskReviewEmpty') : '');
      return;
    }
    const range = message('deskReviewRange', {first: number(offset + 1), last: number(offset + items.length), total: number(total)});
    const legacy = selected()?.legacy_split ? message('deskLegacySplitRecording') : '';
    tell('review-status', [range, legacy, enabled ? '' : message('deskReviewPlaybackOff')].filter(Boolean).join(' '));
  }

  function apply(value) {
    const next = value === true;
    if (enabled === next) return;
    enabled = next;
    if (!enabled && audioMode === 'context') stopAudio();
    controls();
    if (!listRequest) status();
    window.dispatchEvent(new Event('dms-recording-playback-change'));
  }

  async function readPreference() {
    if (writePending) return;
    const snapshot = ++revision;
    try {
      const prefs = await api('preferences');
      if (snapshot === revision) apply(prefs.recording_playback_enabled);
    } catch {
      if (snapshot === revision) apply(false);
    }
  }

  async function set(value) {
    if (typeof value !== 'boolean' || writePending) return;
    writePending = true;
    ++revision;
    if (!value) {
      apply(false);
      channel?.postMessage('stop');
    }
    try {
      const prefs = await api('preferences', {recording_playback_enabled: value});
      apply(prefs.recording_playback_enabled);
      channel?.postMessage('saved');
    } catch (error) {
      apply(false);
      throw error;
    } finally {
      writePending = false;
    }
  }

  window.dmsRecordingPlayback = {enabled: () => enabled, set, apply};
  if (channel) channel.onmessage = event => {
    if (event.data === 'stop') {
      ++revision;
      apply(false);
    } else if (event.data === 'audition' && audioMode) {
      stopAudio();
      controls();
      status();
    } else if (event.data === 'saved') void readPreference();
  };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void readPreference();
  });

  function collapse(focus = false) {
    section.open = false;
    if (focus) summary.focus({preventScroll: true});
  }

  function reset() {
    listRequest?.abort();
    listRequest = null;
    stopAudio();
    items = [];
    total = offset = 0;
    choice.replaceChildren();
    replacementJob = '';
    tell('review-job', '');
    collapse();
    controls();
  }

  async function playSelected(mode) {
    const item = selected();
    const inContext = mode === 'context';
    if (!item || scope !== profileKey() || (inContext && !enabled)) return;
    const replay = audioMode === mode;
    if (replay && !audioURL) return;
    if (replay) audioRequest?.abort();
    else stopAudio();
    channel?.postMessage('audition');
    const request = new AbortController();
    audioRequest = request;
    audioMode = mode;
    controls();
    tell('review-status', message(inContext ? 'deskContextLoading' : 'deskReviewLoadingAudio', {number: number(item.number)}));
    try {
      if (replay) {
        player.currentTime = 0;
      } else {
        const route = inContext ? 'recording-context-audio' : 'recording-audio';
        const target = {...fetchSelection(), slot: item.slot, sha256: item.sha256};
        if (!inContext) target.explicit_play = true;
        const audioType = inContext ? 'audio/wav' : ['audio/ogg', 'audio/wav'];
        const blob = await api(route, target, {audioType, signal: request.signal});
        if (request.signal.aborted || (inContext && !enabled) || scope !== profileKey()
            || selected()?.slot !== item.slot || selected()?.sha256 !== item.sha256) return;
        audioURL = URL.createObjectURL(blob);
        player.src = audioURL;
        player.hidden = false;
        player.setAttribute('aria-label', inContext ? message('deskContextLabel', {caption:title(item)}) : title(item));
      }
      controls();
      status();
      await player.play();
    } catch (error) {
      if (!request.signal.aborted) {
        stopAudio();
        tell('review-status', error.message || message(inContext ? 'deskContextAudioError' : 'deskReviewAudioError'));
      }
    } finally {
      if (audioRequest === request) audioRequest = null;
      controls();
    }
  }

  async function loadPage(start, focusChoice = false) {
    if (!gameCharacter || scope !== profileKey()) return;
    listRequest?.abort();
    const request = new AbortController();
    listRequest = request;
    if (start !== offset) stopAudio();
    tell('review-status', message('deskReviewLoading'));
    controls();
    try {
      const page = await api('recordings', {...fetchSelection(), offset: start}, {signal: request.signal});
      if (request.signal.aborted || scope !== profileKey()) return;
      if (!Array.isArray(page.items) || page.items.length > pageSize ||
          !Number.isSafeInteger(page.offset) || page.offset < 0 || !Number.isSafeInteger(page.total) || page.total < 0 ||
          page.items.some(item => !item || !Number.isSafeInteger(item.number) || item.number < 1 ||
            (typeof item.slot !== 'string' && !Number.isSafeInteger(item.slot)) || !/^[a-f0-9]{64}$/i.test(item.sha256) ||
            (item.caption !== undefined && typeof item.caption !== 'string'))) {
        throw new Error(message('deskReviewListError'));
      }
      if (page.total && start >= page.total) return loadPage(Math.floor((page.total - 1) / pageSize) * pageSize, focusChoice);
      if (page.total && !page.items.length) throw new Error(message('deskReviewListError'));
      offset = page.offset;
      total = page.total;
      const previous = selected();
      items = page.items;
      choice.replaceChildren(...items.map(item => new Option(title(item), item.slot)));
      choice.selectedIndex = items.length ? Math.max(0, items.findIndex(item => item.slot === previous?.slot)) : -1;
      if (previous?.slot !== selected()?.slot || previous?.sha256 !== selected()?.sha256) stopAudio();
      element('review-items').hidden = !items.length;
      if (focusChoice && section.open && items.length) choice.focus({preventScroll: true});
      status();
    } catch (error) {
      if (!request.signal.aborted) tell('review-status', error.message || message('deskReviewListError'));
    } finally {
      if (listRequest === request) listRequest = null;
      controls();
    }
  }

  function sync() {
    const next = profileKey();
    const count = roster.find(row => row.id === gameCharacter?.id)?.count;
    if (scope !== next) {
      reset();
      scope = next;
      knownCount = count;
      if (gameCharacter && count !== 0) void loadPage(0);
      else status();
    } else if (count !== knownCount) {
      knownCount = count;
      void loadPage(offset);
    }
    controls();
  }

  function isCurrent(item) {
    return section.open && scope === profileKey() && !replace.disabled && item === selected();
  }

  function jobChanged(result, wasWorking, replacementStarted) {
    if (!gameCharacter || scope !== profileKey()) return;
    if (replacementStarted) replacementJob = result.job_id || 'pending';
    if (replacementJob && (!result.job_id || result.job_id === replacementJob || replacementJob === 'pending')) {
      tell('review-job', element('fetch-status').textContent);
      if (!fetchWorking) replacementJob = '';
    }
    if (!fetchWorking && (wasWorking || replacementStarted)) void loadPage(offset);
  }

  section.addEventListener('toggle', () => {
    if (!section.open) {
      if (audioMode) { stopAudio(); status(); }
    } else if (gameCharacter && !selected() && !listRequest) {
      void loadPage(0);
    }
    controls();
  });
  choice.addEventListener('change', () => { stopAudio(); controls(); status(); });
  play.addEventListener('click', () => { void playSelected('recording'); });
  playContext.addEventListener('click', () => { void playSelected('context'); });
  player.addEventListener('error', () => {
    if (audioURL && player.currentSrc === audioURL) {
      const key = audioMode === 'context' ? 'deskContextAudioError' : 'deskReviewAudioError';
      stopAudio();
      controls();
      tell('review-status', message(key));
    }
  });
  replace.addEventListener('click', () => {
    const item = selected();
    if (isCurrent(item)) void fetchAction('start', item);
  });
  element('review-previous').addEventListener('click', () => { void loadPage(Math.max(0, offset - pageSize), true); });
  element('review-next').addEventListener('click', () => { void loadPage(offset + pageSize, true); });
  element('review-reload').addEventListener('click', () => { void loadPage(offset, true); });
  section.addEventListener('keydown', event => {
    if (event.key === 'Escape' && section.open) { event.preventDefault(); collapse(true); }
  });
  window.addEventListener('pagehide', () => { reset(); scope = ''; });
  window.addEventListener('pageshow', event => { if (event.persisted) { void readPreference(); sync(); } });
  sync();
  return {sync, isCurrent, jobChanged,
    rejected(detail) { tell('review-job', detail); if (gameCharacter) void loadPage(offset); }};
})();
