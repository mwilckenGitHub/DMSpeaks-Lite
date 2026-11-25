# DMSpeaks Lite for WoW

DMSpeaks Lite prepares recordings of your character’s name for NPC dialogue in World of Warcraft Forever. The Windows companion fetches recordings. The add-on plays them in the game.

DMSpeaks Lite builds on **Spoken by rustykey (Rusty Key)**. Spoken provides the WoW dialogue player and the original NPC voice recordings that Lite adapts to speak a character’s name, race, or class in context. Thank you to rustykey and the Spoken contributors. [Spoken on GitHub](https://github.com/rusty-key/spoken-wow) · [Spoken on CurseForge](https://www.curseforge.com/wow/addons/spoken)

GPT-6 Codex assisted with development, packaging, and testing.

## First-time installation

For Windows x64 with English Spoken voice packs. Download the Setup executable from the [Beta 12 release](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases/tag/v0.1.0-beta.12).

Install Spoken and the English Spoken voice packs you want in WoW first. Run the downloaded Setup executable and select the same WoW folder. Setup uses the matching Lite Alliance, Horde, Shared, and Gossip playback packs for the Spoken packs it finds there. It also installs the Lite add-on and its common name data.

Setup downloads the matching Lite ZIPs from the Beta 12 release. To install with downloaded copies, keep the ZIPs beside Setup and leave them zipped. In WoW, use **DMSpeaks Lite → Installed content** to enable or disable installed packs. Missing packs have disabled checkboxes.

Lite connects when Spoken provides the playback functions it uses, regardless of version number. It verifies the installed English recording and caption before selecting a matching plan. [Spoken add-on and voice packs](https://www.curseforge.com/wow/addons/spoken#description)

Spoken documents the source of its generated audio, game text, and artwork, including the terms that apply to those materials. [Spoken’s third-party notice](https://github.com/rusty-key/spoken-wow/blob/master/THIRD_PARTY.md)

## Developer source

Build the standalone Python functions or run the companion tests on a clean checkout. The repository includes the Windows companion, browser UI, WoW integration, installer recipes and examples. This source version uses project-drawn icons. A playable installation also requires matching, separately obtained NPC audio and name-catalog data. [Build and verification guide](BUILDING.md) · [Optional example corpora](examples)

## Earlier Beta 12 name audio update

[Download the Windows x64 name audio update](https://raw.githubusercontent.com/mwilckenGitHub/DMSpeaks-Lite/main/downloads/DMSpeaks-Lite-Name-Audio-Update-Windows-x64.zip)

This update requires an existing **Beta 12 Northshire Fetch** or **Beta 12 Fetch Decoder** installation. It includes the earlier Northshire decoder fix.

During a hosted name fetch, Lite can send more of the NPC’s sentence around the character’s name. It keeps the leading word and name together when the dialogue calls for it. Before saving the recording, Lite checks for audible traces of “Adventurer.” **Start Fetch** can retry a rejected take automatically. For new recordings, Lite saves the start and stop positions of the cut so they stay available when you back up the recording.

1. Pause any active fetch, wait for it to pause, and close Lite.
2. Extract the downloaded ZIP and run **Update DMSpeaks Lite.cmd**.
3. Reopen Lite and use **Start Fetch** for new recordings. To replace a recording you already have, open **Review saved recordings** and choose **Replace this recording**.

The download’s SHA-256 is `529860e968c2f2eee6fa621689cd4ecd4a692e11aaafea5f51d17eff4d260bf7`.

The [earlier decoder-only update](https://raw.githubusercontent.com/mwilckenGitHub/DMSpeaks-Lite/main/downloads/DMSpeaks-Lite-Northshire-Decoder-Update-Windows-x64.zip) is also available.

## Sync a new character

On first login, choose **Reload UI** in the add-on prompt, or type `/reload` in WoW chat. In the Windows companion, select **Refresh character**, choose your coverage, and start the fetch. When Lite reports your new recordings are ready, reload WoW to use them.

## License

The official Lite distribution is for fetching player-name audio for use in World of Warcraft. Other uses of the official package require prior written permission. [End-User License Agreement](EULA.md)

Source code released under the MIT License and third-party material remain subject to their respective terms. [MIT License](LICENSE) · [Third-party material](THIRD_PARTY.md)
