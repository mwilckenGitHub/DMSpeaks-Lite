'use strict';

const desktopHelp = (() => {
  const dialog = element('desktop-help');
  const contents = element('help-content');
  const topics = [
    ['deskStart', ['deskStartOne', 'deskStartTwo', 'deskStartThree'], ['deskOpenProfiles', 'characters']],
    ['deskProviders', [
      ['h4', 'deskFishFreeTitle'], 'deskFishFreeKey',
      ['a', 'deskFishFreeLink', 'https://fish.audio/app/developers/get-started/'],
      'deskFishFreeConnect', 'deskFishFreeAvailability',
      ['a', 'deskFishFreeDetails', 'https://fish.audio/developers/#pricing'],
      ['h4', 'deskOtherProviders'], 'deskProviderOne', 'deskProviderTwo', 'deskProviderThree',
    ], ['deskOpenSpeech', 'connection-settings']],
    ['deskLocalSpeech', [
      'deskLocalIntro', ['h4', 'deskLocalHost'], 'deskLocalSpark', 'deskLocalGpu',
      ['h4', 'deskLocalServer'], 'deskLocalServe', 'deskLocalApi',
      ['a', 'deskLocalRecipe', 'https://github.com/vllm-project/vllm-omni/blob/main/recipes/fishaudio/Drama-3.md'],
      ['a', 'deskLocalHardwareRecipe', 'https://github.com/Ranjit246/fish-audio-drama3-tts/blob/2e62bd07f2f6f1d2a26cdaf632006c6846141b71/upstream/VALIDATION-0002.md'],
      ['h4', 'deskLocalSsh'], 'deskLocalAlias', ['pre', 'deskLocalAliasExample'],
      'deskLocalLogin', ['pre', 'deskLocalCommands'], 'deskLocalHealth',
      ['h4', 'deskLocalLite'], 'deskLocalFields', 'deskLocalTest', 'deskLocalManaged',
    ], ['deskOpenSpeech', 'connection-settings']],
    ['deskFetch', ['deskCoverageOne', 'deskCoverageTwo', 'deskCoverageThree'], ['deskOpenFetch', 'name-fetch']],
    ['deskBackup', ['deskBackupOne', 'deskBackupTwo', 'deskBackupThree']],
    ['deskStorage', ['deskStorageOne', 'deskStorageTwo', 'deskStorageThree'], ['deskOpenStorage', 'card-storage']],
    ['deskTrouble', ['deskTroubleOne', 'deskTroubleTwo', 'deskTroubleThree'], ['deskOpenFetch', 'name-fetch']],
    ['deskKeyboard', ['deskKeyboardOne', 'deskKeyboardTwo']],
    ['deskAbout', ['deskAboutText']],
  ];
  const buttons = new Map();
  let navigateToPanel;
  function showTopic(topic, focus) {
    const [title, paragraphs, action] = topic;
    const heading = node('h3', '', message(title));
    heading.id = 'help-topic-heading';
    heading.tabIndex = -1;
    const blocks = paragraphs.map(entry => {
      if (typeof entry === 'string') return node('p', '', message(entry));
      const [tag, key, url] = entry;
      const block = node(tag, '', message(key));
      if (tag === 'a') {
        block.href = url;
        block.target = '_blank';
        block.rel = 'noopener noreferrer';
      }
      return block;
    });
    contents.replaceChildren(heading, ...blocks);
    if (action) {
      const link = node('button', 'help-action', message(action[0]));
      link.type = 'button';
      link.addEventListener('click', () => {
        dialog.close();
        navigateToPanel(action[1]);
      });
      contents.append(link);
    }
    if (title === 'deskAbout') {
      for (const [tag, key] of [['version','deskVersion'], ['build','deskBuild']]) {
        const value = document.querySelector('meta[name="lite-' + tag + '"]').content;
        if (value && !value.startsWith('__')) contents.append(node('p', 'build-info', message(key, {[tag]: value})));
      }
    }
    for (const [key, button] of buttons) {
      if (key === title) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
    if (focus) heading.focus();
  }
  for (const topic of topics) {
    const button = node('button', '', message(topic[0]));
    button.type = 'button';
    button.setAttribute('aria-controls', contents.id);
    button.addEventListener('click', () => showTopic(topic, true));
    element('help-topics').append(button);
    buttons.set(topic[0], button);
  }
  element('help-close').addEventListener('click', () => dialog.close());
  return function openHelp(title, navigate) {
    navigateToPanel = navigate;
    const topic = topics.find(([key]) => key === title) || topics[0];
    showTopic(topic, false);
    dialog.showModal();
    if (title === 'deskLocalSpeech') contents.querySelector('h3').focus();
    else buttons.get(topic[0]).focus();
  };
})();
