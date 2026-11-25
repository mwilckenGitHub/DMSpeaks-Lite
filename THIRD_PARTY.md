# License scope and third-party material

DMSpeaks contributors license their original software and documentation under the [MIT License](LICENSE). You may use, modify, and redistribute that software, including commercially. When you distribute copies or substantial portions of it, include the copyright and permission notices in the license.

## World of Warcraft and Spoken

Thank you to **rustykey (Rusty Key)** and the Spoken contributors for the dialogue player and original NPC voice recordings on which DMSpeaks Lite builds. [Spoken on GitHub](https://github.com/rusty-key/spoken-wow) · [Spoken on CurseForge](https://www.curseforge.com/wow/addons/spoken)

Spoken’s own code bears the 2026 Rusty Key copyright. The upstream license and its scope statement are included for reference. [Included license](source/third_party/Spoken-LICENSE.txt) · [Upstream revision](https://github.com/rusty-key/spoken-wow/blob/bf156bfd1655afdb5c4ae25d71b444b6aec50ce6/LICENSE)

The Windows Beta 12 name audio update contains companion code and installation files. It uses game and voice-pack files already installed on the player’s computer.

World of Warcraft is a trademark of Blizzard Entertainment. Blizzard retains its rights in its quest dialogue and game artwork. Spoken’s MIT license covers the code its contributors wrote. Spoken separately identifies the sources of its artwork and the provider terms relevant to its generated voice packs. Each rights holder’s terms govern reuse of their material. [Spoken’s license](https://github.com/rusty-key/spoken-wow/blob/master/LICENSE) · [Spoken’s third-party notice](https://github.com/rusty-key/spoken-wow/blob/master/THIRD_PARTY.md)

## Source checkout and dependencies

The checked-in Python, Lua, JavaScript and installer sources include DMSpeaks integration with Spoken. The public source package contains DMSpeaks' own checksum and compatibility tables for identifying installed files. The project-drawn waveform icons can be regenerated with the included drawing script. The source package uses digital silence for its shared-storage probe.

Spoken performances, prepared NPC voice references, the game dialogue corpus, game portraits, and personalized player recordings are distributed separately where their governing terms permit. Anyone obtaining those assets must follow their publishers’ terms. The MIT notice here grants rights to the DMSpeaks contributors’ work. [Spoken media and code notices](https://github.com/rusty-key/spoken-wow/blob/master/THIRD_PARTY.md)

Python libraries, FFmpeg, PocketSphinx, the statistical model, pronunciation dictionary, NSIS and 7-Zip retain their own licenses. The source build refers to external installations of these components. If you distribute a runtime, include the notices and satisfy the terms of the specific builds and models you ship. [PocketSphinx and model information](source/third_party/pocketsphinx.md)

## Audio made with DMSpeaks

A saved recording can include quest dialogue, a voice reference, and speech generated through a provider. When sharing recordings, consider the rights in the material you used and the provider terms applicable to your generation. Cutting a clip, changing its pace, or encoding it as Ogg preserves the rights that already apply to its source material.
