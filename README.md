# DMSpeaks Lite for WoW

DMSpeaks Lite prepares player-name audio for the DMSpeaks add-on. Lite runs in the background while it fetches audio. You use the add-on to configure the provider and request the clips.

## Downloads

Installers will be posted on the [Releases page](https://github.com/mwilckenGitHub/DMSpeaks-Lite/releases). The first public installer is pending.

Use the asset for your operating system and processor. A browser download works on Windows, Linux, and macOS. To script the download, copy the asset URL from its release.

On Windows, open PowerShell and save the installer:

```powershell
$url = 'PASTE_THE_WINDOWS_ASSET_URL_HERE'
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile "$env:TEMP\DMSpeaks-Lite-Setup.exe"
```

On Linux or macOS, open Terminal and save the chosen asset:

```sh
url='PASTE_THE_ASSET_URL_HERE'
filename='PASTE_THE_ASSET_FILENAME_HERE'
curl --fail --location --output "$filename" "$url"
```

## Fetch name audio in WoW

1. Run the Lite installer or follow the setup instructions for your platform.
2. In WoW, click the **DMSpeaks minimap icon**. **Lite Setup** opens the setup guide.
3. Choose **Fetch Settings**, save your speech provider and credentials, then save the dialogue coverage you want.
4. Click **Queue Audio Fetch**. Use **Refresh Progress** to see which clips are ready. The buttons reload the WoW UI when safe.
