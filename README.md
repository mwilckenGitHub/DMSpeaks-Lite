# DMSpeaks Lite for WoW

DMSpeaks Lite prepares recordings of your character’s name for NPC dialogue in World of Warcraft Forever. The Windows companion fetches recordings. The add-on plays them in the game.

DMSpeaks Lite builds on **Spoken by rustykey (Rusty Key)**. Spoken provides the WoW dialogue player and the original NPC voice recordings that Lite adapts to speak a character’s name, race, or class in context. Thank you to rustykey and the Spoken contributors. [Spoken on GitHub](https://github.com/rusty-key/spoken-wow) · [Spoken on CurseForge](https://www.curseforge.com/wow/addons/spoken)

## Developer source

Build the standalone Python functions or run the companion tests on a clean checkout. The repository includes the Windows companion, browser UI, WoW integration, installer recipes and examples. This source version uses project-drawn icons. A playable installation also requires matching, separately obtained NPC audio and name-catalog data. [Build and verification guide](BUILDING.md) · [Optional example corpora](examples)

## Windows Beta 12 name audio update

[Download the Windows x64 name audio update](https://raw.githubusercontent.com/mwilckenGitHub/DMSpeaks-Lite/main/downloads/DMSpeaks-Lite-Name-Audio-Update-Windows-x64.zip)

This update requires an existing **Beta 12 Northshire Fetch** or **Beta 12 Fetch Decoder** installation. It includes the earlier Northshire decoder fix.

During a hosted name fetch, Lite can send more of the NPC’s sentence around the character’s name. It keeps the leading word and name together when the dialogue calls for it. Before saving the recording, Lite checks for audible traces of “Adventurer.” **Start Fetch** can retry a rejected take automatically. For new recordings, Lite saves the start and stop positions of the cut so they stay available when you back up the recording.

1. Pause any active fetch, wait for it to pause, and close Lite.
2. Extract the downloaded ZIP and run **Update DMSpeaks Lite.cmd**.
3. Reopen Lite and use **Start Fetch** for new recordings. To replace a recording you already have, open **Review saved recordings** and choose **Replace this recording**.

The download’s SHA-256 is `529860e968c2f2eee6fa621689cd4ecd4a692e11aaafea5f51d17eff4d260bf7`.

The [earlier decoder-only update](https://raw.githubusercontent.com/mwilckenGitHub/DMSpeaks-Lite/main/downloads/DMSpeaks-Lite-Northshire-Decoder-Update-Windows-x64.zip) is also available.

## First-time installation

This download updates an installed copy of Lite. A full installer for new players is pending. You can get the original Spoken add-on and voice packs from [Spoken’s publisher](https://www.curseforge.com/wow/addons/spoken).

## Sync a new character

On first login, choose **Reload UI** in the add-on prompt, or type `/reload` in WoW chat. In the Windows companion, select **Refresh character**, choose your coverage, and start the fetch. When Lite reports your new recordings are ready, reload WoW to use them.

## License

The official Lite distribution is for fetching player-name audio for use in World of Warcraft. Other uses of the official package require prior written permission. [End-User License Agreement](EULA.md)

Source code released under the MIT License and third-party material remain subject to their respective terms. [MIT License](LICENSE) · [Third-party material](THIRD_PARTY.md)
