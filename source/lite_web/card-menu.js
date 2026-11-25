'use strict';

(() => {
  const menuBar = element('app-menu');
  menuBar.setAttribute('role', 'menubar');
  const dialog = element('card-dialog');
  const media = element('storage-medium');
  const folder = element('storage-path');
  const network = new Set(['nas', 'network', 'tailscale']);
  const mounts = new Set(['sftp', 'nfs', 'webdav']);
  const mirrors = new Set(['syncthing', 'rsync', 'rclone']);
  const advanced = new Set(['tailscale', ...mounts, ...mirrors]);
  const cloud = new Set(['googledrive', 'onedrive', 'icloud', 'dropbox']);
  let exporting = false;
  let menuIndex = 0;
  let popup = null;
  let configured;
  let changingPlayback = false;
  const menus = [
    ['fileMenu', 'f', [['deskImport', () => element('card-file').click()], ['deskExport', exportDialog]]],
    ['viewMenu', 'v', [['profilesItem', () => navigate('characters')], ['deskFind', () => navigate('character-search')],
      ['deskReviewTitle', () => navigate('saved-recordings')], ['fetchItem', () => navigate('name-fetch')]]],
    ['optionsMenu', 'o', [['deskSpeechCommand', () => navigate('connection-settings')], ['deskPlaybackCommand', togglePlayback],
      ['deskStorageCommand', () => navigate('card-storage')],
      ['advanced', () => navigate('advanced-settings')]]],
    ['helpMenu', 'h', [['deskHelpTopics', () => desktopHelp('deskStart', navigate)],
      ['deskLocalSpeech', () => desktopHelp('deskLocalSpeech', navigate)],
      ['deskAbout', () => desktopHelp('deskAbout', navigate)]]],
  ];
  const heads = [];
  let typed = '';
  let typedAt = 0;
  function navigate(id) {
    const panel = element(id);
    if (id === 'character-search' || id === 'saved-recordings') {
      const target = id === 'character-search' ? panel : element('saved-recordings-heading');
      target.focus();
      target.scrollIntoView({block: 'nearest'});
      return;
    }
    if (id === 'characters' || id === 'name-fetch') {
      const area = id === 'characters' ? panel : element('fetch-activity');
      const target = id === 'characters'
        ? panel.querySelector('input[type="radio"]:checked') || element('character-search')
        : area;
      area.classList.add('view-destination');
      target.focus({preventScroll: true});
      target.scrollIntoView({block: 'nearest'});
      area.scrollIntoView({block: 'nearest'});
      return;
    }
    if (panel.tagName === 'DETAILS') panel.open = true;
    const target = panel.querySelector('summary,h2');
    target.tabIndex = -1;
    target.focus();
    target.scrollIntoView({block: 'nearest'});
  }
  element('selection-summary').addEventListener('click', () => navigate('connection-settings'));
  function updatePlaybackItem() {
    const control = popup?.querySelector('[role=menuitemcheckbox]');
    if (!control) return;
    control.setAttribute('aria-checked', String(window.dmsRecordingPlayback?.enabled() === true));
    control.setAttribute('aria-disabled', String(changingPlayback || !window.dmsRecordingPlayback));
  }
  async function togglePlayback() {
    const preference = window.dmsRecordingPlayback;
    if (changingPlayback || !preference) return;
    changingPlayback = true;
    try {
      await preference.set(!preference.enabled());
      tell('card-status', message(preference.enabled() ? 'deskPlaybackEnabled' : 'deskPlaybackDisabled'));
    } catch (error) {
      tell('card-status', error.message || message('saveFailed'));
    } finally {
      changingPlayback = false;
      updatePlaybackItem();
    }
  }
  window.addEventListener('dms-recording-playback-change', updatePlaybackItem);
  function dismiss(focus = false) {
    typed = '';
    if (popup) {
      heads[menuIndex].setAttribute('aria-expanded', 'false');
      popup.remove();
      popup = null;
    }
    if (focus) heads[menuIndex].focus();
  }
  function focusHead(i) {
    heads[menuIndex].tabIndex = -1;
    menuIndex = (i + menus.length) % menus.length;
    heads[menuIndex].tabIndex = 0;
    heads[menuIndex].focus();
  }
  function open(i, last = false) {
    dismiss();
    focusHead(i);
    const head = heads[i];
    popup = node('div', 'menu-popup');
    popup.setAttribute('role', 'menu');
    popup.setAttribute('aria-labelledby', head.id);
    for (const [label, action] of menus[i][2]) {
      const item = node('button', '', message(label));
      item.type = 'button';
      item.setAttribute('role', label === 'deskPlaybackCommand' ? 'menuitemcheckbox' : 'menuitem');
      if (label === 'deskPlaybackCommand') {
        const mark = node('span', 'menu-checkbox-mark');
        mark.setAttribute('aria-hidden', 'true');
        item.prepend(mark);
      }
      item.tabIndex = -1;
      item.addEventListener('click', () => { dismiss(true); action(); });
      popup.append(item);
    }
    head.parentElement.append(popup);
    updatePlaybackItem();
    head.setAttribute('aria-expanded', 'true');
    (last ? popup.lastElementChild : popup.firstElementChild).focus();
  }
  for (const [i, [label, key]] of menus.entries()) {
    const group = node('div', 'menu-group');
    group.setAttribute('role', 'none');
    const head = node('button', 'menu-head', message(label));
    head.id = 'menu-' + key;
    head.type = 'button';
    head.tabIndex = i === 0 ? 0 : -1;
    head.setAttribute('role', 'menuitem');
    head.setAttribute('aria-haspopup', 'menu');
    head.setAttribute('aria-expanded', 'false');
    head.setAttribute('aria-keyshortcuts', 'Alt+Shift+' + key.toUpperCase());
    head.addEventListener('click', () => popup && i === menuIndex ? dismiss(true) : open(i));
    heads.push(head);
    group.append(head);
    menuBar.append(group);
  }
  document.addEventListener('pointerdown', event => { if (!menuBar.contains(event.target)) dismiss(); });
  menuBar.addEventListener('focusout', event => {
    if (event.relatedTarget && !menuBar.contains(event.relatedTarget)) dismiss();
  });
  document.addEventListener('keydown', event => {
    if (event.altKey && event.shiftKey && !event.ctrlKey && !event.metaKey && !event.isComposing
        && !document.querySelector('dialog:modal')) {
      const i = menus.findIndex(([, key]) => key === event.key.toLowerCase());
      if (i >= 0) { event.preventDefault(); open(i); }
    }
  });
  menuBar.addEventListener('keydown', event => {
    const key = event.key;
    if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing) return;
    if (key === 'Tab') return;
    if (key === 'Escape') { dismiss(true); event.preventDefault(); return; }
    if (['ArrowLeft', 'ArrowRight'].includes(key)) {
      const step = (key === 'ArrowRight') === (document.documentElement.dir !== 'rtl') ? 1 : -1;
      const i = (menuIndex + step + menus.length) % menus.length;
      if (popup) open(i); else focusHead(i);
    } else if (!popup && (key === 'Home' || key === 'End')) {
      focusHead(key === 'Home' ? 0 : menus.length - 1);
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(key)) {
      if (!popup) open(menuIndex, key === 'ArrowUp' || key === 'End');
      else {
        const items = [...popup.children];
        const i = items.indexOf(document.activeElement);
        const next = key === 'Home' ? 0 : key === 'End' ? items.length - 1 : i + (key === 'ArrowUp' ? -1 : 1);
        items[(next + items.length) % items.length].focus();
      }
    } else if (popup && key.length === 1 && key.trim()) {
      const now = performance.now();
      const letter = key.toLocaleLowerCase(document.documentElement.lang);
      typed = now - typedAt < 700 && typed !== letter ? typed + letter : letter;
      typedAt = now;
      const items = [...popup.children];
      const next = items.indexOf(document.activeElement) + 1;
      const sequence = items.slice(next).concat(items.slice(0, next));
      const match = sequence.find(item => item.textContent.toLocaleLowerCase(document.documentElement.lang).startsWith(typed));
      if (match) match.focus();
    } else return;
    event.preventDefault();
  });

  function showDialog(title, body, confirm) {
    dialog.querySelector('h2').textContent = message(title);
    element('card-dialog-body').replaceChildren(body);
    element('card-confirm').hidden = !confirm;
    dialog.showModal();
  }
  function exportDialog() {
    const label = node('label', '', message('exportScope'));
    const select = node('select', '');
    select.id = 'export-scope';
    select.add(new Option(message('allCards'), ''));
    for (const player of roster.filter(p => p.selectable)) {
      select.add(new Option(fullName(player), player.id));
    }
    if ([...select.options].some(opt => opt.value === gameCharacter?.id)) select.value = gameCharacter.id;
    label.append(select);
    const body = node('div', '');
    const destinationLabel = node('label', '', message('exportDestination'));
    const destination = node('select', '');
    destination.id = 'export-destination';
    destination.add(new Option(message('exportDownloads'), 'download'));
    destination.add(new Option(message('exportChooseFolder'), 'choose'));
    if (configured?.folder) destination.add(new Option(message('exportSyncFolder'), 'configured'));
    destinationLabel.append(destination);
    body.append(node('p', '', message('deskExportHint')), label, destinationLabel);
    showDialog('exportTitle', body, true);
  }
  element('card-close').onclick = () => dialog.close();
  element('card-confirm').onclick = async () => {
    if (exporting) return;
    const selected = element('export-scope').value;
    const destination = element('export-destination').value;
    dialog.close();
    exporting = true;
    tell('card-status', message('exporting'));
    try {
      if (destination !== 'download') {
        const folder = destination === 'configured' ? configured?.folder : (await api('card-folder', {})).folder;
        if (folder) {
          const result = await api('card-export-folder', {character_id: selected || null, folder});
          tell('card-status', message('exportSavedTo', {path: result.path}));
        } else {
          tell('card-status', '');
        }
        return;
      }
      const response = await postSettings('card-export', JSON.stringify({character_id: selected || null}));
      if (!response.ok) throw new Error(liteLocale.formatError(await response.json(), 'saveFailed'));
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = 'DMSpeaks-' + (roster.find(p => p.id === selected)?.name || 'name-cards') + '.zip';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      tell('card-status', message('exportReady'));
    } catch (error) { tell('card-status', error.message); }
    finally { exporting = false; }
  };
  element('card-file').onchange = async event => {
    const file = event.target.files[0];
    event.target.value = '';
    if (!file) return;
    tell('card-status', message('importing'));
    try {
      const response = await postSettings('card-import', file, 'application/zip');
      const result = await response.json();
      if (!response.ok) throw new Error(liteLocale.formatError(result, 'saveFailed'));
      tell('card-status', message('importComplete', result, result.cards) + (result.conflicts ? ' ' + message('importConflicts', {count:result.conflicts}) : ''));
      await refreshProfiles();
    } catch (error) { tell('card-status', error.message); }
  };

  const advancedStorage = element('advanced-storage');
  function listStorage(selected) {
    media.replaceChildren();
    for (const medium of ['shared', ...cloud, ...network, ...mounts, ...mirrors]) {
      if (!advanced.has(medium) || advancedStorage.checked) media.add(new Option(message(medium), medium));
    }
    media.value = selected;
  }
  listStorage('shared');
  advancedStorage.onchange = () => {
    const reset = advanced.has(media.value) && !advancedStorage.checked;
    listStorage(reset ? 'shared' : media.value);
    if (reset) media.onchange();
    else showStorage();
  };
  function showStorage() {
    element('server-row').hidden = !network.has(media.value);
    element('storage-connect').hidden = !network.has(media.value) && !cloud.has(media.value);
    element('storage-connect').textContent = message(cloud.has(media.value) ? 'signIn' : 'connectServer');
    element('offline-row').hidden = !cloud.has(media.value) && !mirrors.has(media.value);
    element('offline-help').hidden = element('offline-row').hidden;
    const customNetwork = network.has(media.value) || mounts.has(media.value);
    element('nas-row').hidden = !advancedStorage.checked || !customNetwork || media.value === 'nas';
    element('route-row').hidden = !advancedStorage.checked || !customNetwork || media.value === 'tailscale';
    element('lan-row').hidden = cloud.has(media.value) || mirrors.has(media.value) || media.value === 'tailscale' ||
      (!element('route-row').hidden && element('via-tailscale').checked);
    tell('storage-help', message(mounts.has(media.value) ? media.value + 'Help' : mirrors.has(media.value) ? 'mirrorHelp' : media.value === 'tailscale' ? 'tailscaleHelp' : cloud.has(media.value) ? 'cloudHelp' : network.has(media.value) ? 'shareHelp' : 'folderHelp'));
    element('disconnect').disabled = !configured?.folder;
    element('sync-now').disabled = !configured?.folder || configured.folder !== folder.value || configured.medium !== media.value;
  }
  media.onchange = () => {
    const saved = configured?.medium === media.value && configured.folder;
    folder.value = saved ? configured.folder : '';
    element('cache-offline').checked = Boolean(saved && configured.cache_offline);
    element('storage-server').value = '';
    element('storage-nas').checked = Boolean(saved && configured.nas);
    element('via-tailscale').checked = Boolean(saved && configured.via_tailscale);
    element('lan-only').checked = saved ? configured.lan_only : !cloud.has(media.value) && media.value !== 'tailscale';
    showStorage();
  };
  element('via-tailscale').onchange = showStorage;
  folder.oninput = showStorage;
  const status = element('sync-status');
  element('storage-connect').onclick = event => withStatus(event.currentTarget, status, async () => {
    const result = await api('card-connect', {medium: media.value, address: element('storage-server').value});
    return message({provider:'providerOpened', help:'helpOpened', network:'networkOpened'}[result.opened]);
  });
  element('storage-browse').onclick = event => withStatus(event.currentTarget, status, async () => {
    const result = await api('card-folder', {});
    if (result.folder) folder.value = result.folder;
    showStorage();
    return '';
  });
  async function sync() {
    tell('sync-status', message('syncWorking'));
    const result = await api('card-sync', {});
    await refreshProfiles();
    return result.error || message('syncFinished');
  }
  element('storage-form').onsubmit = async event => {
    event.preventDefault();
    await withStatus(event.submitter, status, async () => {
      configured = await api('card-sync', {folder:folder.value, medium:media.value, lan_only:element('lan-only').checked,
        cache_offline:!element('offline-row').hidden && element('cache-offline').checked,
        nas:!element('nas-row').hidden && element('storage-nas').checked,
        via_tailscale:!element('route-row').hidden && element('via-tailscale').checked});
      listStorage(configured.medium);
      folder.value = configured.folder;
      showStorage();
      return await sync();
    });
  };
  element('sync-now').onclick = event => withStatus(event.currentTarget, status, sync);
  element('disconnect').onclick = async () => {
    try {
      configured = await api('card-sync', {folder:''});
      folder.value = '';
      tell('sync-status', message('folderDisconnected'));
      showStorage();
      await refreshProfiles();
    } catch (error) { status.textContent = error.message; }
  };
  function updateAccounts() {
    const known = new Map(roster.flatMap(p => (p.accounts || []).map(a => [a.id,a])));
    const list = element('account-list');
    const selected = list.value;
    element('account-settings').hidden = known.size === 0;
    list.replaceChildren(...[...known.values()].map(a => new Option(accountLabel(a), a.id)));
    if (known.has(selected)) list.value = selected;
    const edit = element('account-alias');
    list.onchange = () => { edit.value = known.get(list.value)?.name || ''; };
    if (!element('account-form').contains(document.activeElement)) list.onchange();
  }
  document.addEventListener('lite-roster', updateAccounts);
  element('account-form').onsubmit = async event => {
    event.preventDefault();
    await withStatus(event.submitter, element('account-status'), async () => {
      await api('account-label', {id:element('account-list').value, name:element('account-alias').value});
      await refreshProfiles();
      return message('labelSaved');
    });
  };
  updateAccounts();
  showStorage();
  api('card-sync').then(result => {
    configured = result;
    if (!folder.value && !element('storage-form').contains(document.activeElement)) {
      folder.value = result.folder || '';
      advancedStorage.checked = advanced.has(result.medium) || result.nas || result.via_tailscale;
      element('storage-nas').checked = Boolean(result.nas);
      element('cache-offline').checked = Boolean(result.cache_offline);
      element('via-tailscale').checked = Boolean(result.via_tailscale);
      listStorage(result.medium || 'shared');
      element('lan-only').checked = result.folder ? result.lan_only : true;
    }
    showStorage();
  }).catch(error => {status.textContent = error.message;});
})();
