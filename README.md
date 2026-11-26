# DMSpeaks Lite for WoW

DMSpeaks Lite prepares recordings of your character’s name for NPC dialogue in World of Warcraft Forever. The Windows companion fetches recordings. The add-on plays them in the game.

DMSpeaks Lite builds on **Spoken by rustykey (Rusty Key)**. Spoken provides the WoW dialogue player and the original NPC voice recordings that Lite adapts to speak a character’s name, race, or class in context. Thank you to rustykey and the Spoken contributors. [Spoken on GitHub](https://github.com/rusty-key/spoken-wow) · [Spoken on CurseForge](https://www.curseforge.com/wow/addons/spoken)

GPT-6 Codex assisted with development, packaging, and testing.

## Install on Windows

**Beta 12.8** is the current Windows x64 beta. Run Setup over your existing installation to update. Setup can reuse verified downloads, and updates with matching voice packs skip the full installed-file scan. Maintenance Repair checks every installed file. [Beta 12.8 release notes](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/tag/v0.1.0-beta.12.8)

1. Close WoW. Make sure **Spoken** and **Spoken Quests** are installed in World of Warcraft Forever. [Get Spoken](https://www.curseforge.com/wow/addons/spoken)
2. Download and run **DMSpeaks Lite Setup**. If Windows shows **Windows protected your PC**, choose **More info → Run anyway** if Windows offers it and you opened Setup from this release. [Download Windows Setup](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/v0.1.0-beta.12.8/DMSpeaks-Lite-Setup-Windows-x64.exe)
3. Review the license and credits, then select your WoW Forever folder if prompted.
4. On **Choose voice packs**, check **All voice packs** or select **Alliance quests**, **Horde quests**, **Shared quests**, or **Gossip** individually. Use **+** and **−** to browse the folders. Each selected pack includes both of its folders. Setup downloads the common files and the selected packs.
5. Choose your shortcuts, select **Install**, then select **Finish** to open Lite.

During downloads, the line above the progress bar shows the current archive and how much has downloaded. **Cancel** is available while Setup prepares the ZIPs and checks the installed voices.

To add packs later, run Setup again. Clearing a box skips that pack download and keeps any copy already installed. In WoW, open **DMSpeaks Lite → Installed content** to see and enable the installed packs.

## DMSpeaks Lite WoW Audio Repository

Setup downloads the two required ZIPs and the English voice packs you choose. For an offline install, download those ZIPs and leave them zipped beside the Setup executable.

- **Required files** [DMSpeaks-WoW.zip](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/wow-audio-repository/DMSpeaks-WoW.zip) · [DMSpeaks-Lite-Name-Data.zip](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/wow-audio-repository/DMSpeaks-Lite-Name-Data.zip)
- **Optional voice packs** [Alliance quests](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/wow-audio-repository/DMSpeaks-Lite-Alliance-Audio.zip) · [Horde quests](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/wow-audio-repository/DMSpeaks-Lite-Horde-Audio.zip) · [Shared quests](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/wow-audio-repository/DMSpeaks-Lite-Shared-Audio.zip) · [Gossip](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/download/wow-audio-repository/DMSpeaks-Lite-Gossip-Audio.zip)

Lite connects when Spoken provides the playback functions it uses, regardless of version number. It verifies the installed English recording and caption before selecting a matching plan. [Spoken add-on and voice packs](https://www.curseforge.com/wow/addons/spoken#description)

Spoken documents the source of its generated audio, game text, and artwork, including the terms that apply to those materials. [Spoken’s third-party notice](https://github.com/rusty-key/spoken-wow/blob/master/THIRD_PARTY.md)

## Developer source

Build the standalone Python functions or run the companion tests on a clean checkout. The repository includes the Windows companion, browser UI, WoW integration, installer recipes and examples. This source version uses project-drawn icons. A playable installation also requires matching, separately obtained NPC audio and name-catalog data. [Build and verification guide](BUILDING.md) · [Optional example corpora](examples)

## Name audio update for earlier Beta 12 installations

[Download the Windows x64 name audio update](https://raw.githubusercontent.com/mwilckenGitHub/DMSpeaks-Lite/main/downloads/DMSpeaks-Lite-Name-Audio-Update-Windows-x64.zip)

This update requires an existing **Beta 12 Northshire Fetch** or **Beta 12 Fetch Decoder** installation. It includes the earlier Northshire decoder fix.

During a hosted name fetch, Lite can send more of the NPC’s sentence around the character’s name. It keeps the leading word and name together when the dialogue calls for it. Before saving the recording, Lite checks for audible traces of “Adventurer.” **Start Fetch** can retry a rejected take automatically. For new recordings, Lite saves the start and stop positions of the cut so they stay available when you back up the recording.

1. Pause any active fetch, wait for it to pause, and close Lite.
2. Extract the downloaded ZIP and run **Update DMSpeaks Lite.cmd**.
3. Reopen Lite and use **Start Fetch** for new recordings. To replace a recording you already have, open **Review saved recordings** and choose **Replace this recording**.

The download’s SHA-256 is `529860e968c2f2eee6fa621689cd4ecd4a692e11aaafea5f51d17eff4d260bf7`.

The [earlier decoder-only update](https://raw.githubusercontent.com/mwilckenGitHub/DMSpeaks-Lite/main/downloads/DMSpeaks-Lite-Northshire-Decoder-Update-Windows-x64.zip) is also available.

## Fetch your first character’s name

1. Log in to your character and type `/reload` in WoW chat. In Lite, choose **Refresh character**.
2. Under **Fetch name audio**, choose a speech provider. Scroll to **Speech settings**, enter the provider’s connection details, and select **Save Fetch Settings**, then **Test Saved Connection**. The provider’s own dashboard is where you manage any required credentials and billing.
3. Choose **Starting area** and select **Start Fetch**. Reload WoW once Lite says the audio is ready.

## Report a problem

For missing dialogue, a wrong word, or a pop, keep the quest or NPC window open in WoW and type `/dmspeaksreport` or right-click the DMSpeaks minimap icon. Open the link the add-on gives you and describe what you heard. For a problem with Setup or fetching, include the version and build from **Help → About DMSpeaks Lite** and the steps that reproduce it. [GitHub Issues](https://github.com/mwilckenGitHub/DMSpeaks-Lite/issues)

## Support DMSpeaks Lite

[Donate via PayPal](https://www.paypal.com/paypalme/2ManySecrets)

## License

The official Lite distribution is for fetching player-name audio for use in World of Warcraft. Other uses of the official package require prior written permission. [End-User License Agreement](EULA.md)

Source code released under the MIT License and third-party material remain subject to their respective terms. [MIT License](LICENSE) · [Third-party material](THIRD_PARTY.md)
