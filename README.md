# Remote Display Switcher

Switch a Windows PC between desktop monitors and a TV from any browser on your local network. The app runs in the system tray and restores saved display layouts, making it useful when your TV is connected to your PC with a long HDMI cable.

The Windows application is named **PC Display Switch**. Its current web interface and tray menu use German labels; the instructions below include those labels so you can find the right controls.

## Features

- Control your displays from a phone, tablet, or another computer on the same network.
- Save separate layouts for your desktop monitors and TV, including resolution, refresh rate, orientation, and position.
- Match monitors by hardware ID, so changing Windows display numbers does not select the wrong screen.
- Verify that the target monitors are active before disabling the previous displays, then check that the final layout stays correct across consecutive snapshots.
- Retry failed profile switches and fall back to an extended desktop if the target layout cannot be restored.
- Save profile backups and write rotating diagnostic logs.
- Access the web interface through `switch.local`, the PC's IP address, or localhost.

## Requirements

- Windows 10 or Windows 11 with the intended monitors and TV connected to the PC.
- [MultiMonitorTool](https://www.nirsoft.net/utils/multi_monitor_tool.html), downloaded separately from NirSoft. Use the current 64-bit version on a 64-bit PC.
- Python 3.10 or newer when running from source.
- A shared local network for remote browser access.

MultiMonitorTool is an external dependency and is not included in this repository or bundled into the application.

## Run from source

Clone the repository and install the runtime dependencies using PowerShell:

```powershell
git clone https://github.com/CengoleFHV/Remote-Display-Switcher.git
cd Remote-Display-Switcher
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install pystray Pillow zeroconf
```

Download and extract MultiMonitorTool, then place `MultiMonitorTool.exe` in the repository root, next to `display_remote_tray.py`. You can also put it in a `MultiMonitorTool` or `tools` subfolder.

Start the application:

```powershell
.\.venv\Scripts\python.exe display_remote_tray.py
```

Allow the application's network access on your private network if Windows Firewall prompts you. The app listens on TCP port **5050**.

Open the web interface:

| Location | Address |
| --- | --- |
| On the PC itself | `http://127.0.0.1:5050` |
| On your local network, using mDNS | `http://switch.local:5050` |
| On your local network, using the PC's IP | `http://<pc-ip-address>:5050` |

If `switch.local` does not resolve on your device, use the PC's local IP address. The tray menu can open the local, hostname, and IP-based addresses.

## Set up your display profiles

### 1. Save the desktop layout

1. Open Windows display settings and configure the layout you want at your desk: enable both desktop monitors, choose the primary monitor, set their positions, resolutions, and refresh rates, and disable the TV.
2. Right-click the **PC Display Switch** tray icon.
3. Select **Aktuelles Layout als PC-Profil speichern** (save the current layout as the PC profile).

The app requires a saved PC profile before switching to the TV, so it has a configuration to restore when you return.

### 2. Save the TV layout

1. In Windows display settings, configure your desired TV layout, such as the TV enabled as the only display with the desktop monitors disabled.
2. Right-click the tray icon and select **Aktuelles Layout als TV-Profil speichern** (save the current layout as the TV profile).
3. Use **PC-Monitore aktivieren** to return to your saved desktop layout.

With both profiles saved, the app uses explicit monitor IDs and verifies the layout in both directions.

Without a TV profile, **TV aktivieren** requests Windows' standard external-display mode. That mode does not verify a specific TV layout and may select a different display in a setup with several monitors. Save a TV profile for predictable switching.

### Controls

| Action | Web or tray label |
| --- | --- |
| Activate the saved TV layout | **TV aktivieren** |
| Restore the saved desktop layout | **PC-Monitore aktivieren** |
| Extend the desktop across displays | **Alle Anzeigen erweitern** |
| Request Windows duplicate mode | **Alle Anzeigen duplizieren** |

The web buttons are disabled while a request is running. Profile switches report success only after the saved layout passes verification. The extend and duplicate actions request Windows' global display modes.

### Profile storage and backups

Profiles and logs are stored outside the application folder:

```text
%LOCALAPPDATA%\PCDisplaySwitch\profiles\
```

| File | Purpose |
| --- | --- |
| `desk.cfg` | Saved desktop layout |
| `tv.cfg` | Saved TV layout |
| `desk.backup.cfg`, `tv.backup.cfg` | Previous profiles, retained when you save again |
| `display-switch.log` | Rotating command and verification log |

Save the corresponding profile again after changing your monitor arrangement or display settings. If you accidentally overwrite the desktop profile, choose **Vorheriges PC-Profil wiederherstellen** (restore the previous PC profile), then **PC-Monitore aktivieren**.

## Build the Windows executable

After installing the runtime dependencies, install PyInstaller and run the build script:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\build_windows.bat
```

You can also build directly:

```powershell
.\.venv\Scripts\pyinstaller.exe --clean --noconfirm PCDisplaySwitch.spec
```

The executable is created at `dist\PCDisplaySwitch.exe`. Place the separately downloaded `MultiMonitorTool.exe` next to it, or in a `MultiMonitorTool` or `tools` subfolder within `dist`.

Exit a running copy before rebuilding its executable. After changing the source, rebuild and restart the app: an existing executable keeps using its embedded code. The web page displays the running application version.

Build output, downloaded executables, local profiles, logs, and virtual environments are excluded from Git. The PyInstaller specification and application icons are included so the executable can be rebuilt from source.

## Configuration

The following optional environment variables override the default paths:

| Variable | Purpose |
| --- | --- |
| `PC_DISPLAY_SWITCH_PROFILE_DIR` | Directory for display profiles and logs |
| `PC_DISPLAY_SWITCH_MULTIMONITOR_TOOL` | Path to `MultiMonitorTool.exe` |

For example, set a custom tool path before starting the app:

```powershell
$env:PC_DISPLAY_SWITCH_MULTIMONITOR_TOOL = 'C:\Tools\MultiMonitorTool\MultiMonitorTool.exe'
.\.venv\Scripts\python.exe display_remote_tray.py
```

The port, mDNS hostname, and convenience token are defined by `PORT`, `HOSTNAME`, and `TOKEN` in `display_remote_tray.py`.

## Troubleshooting

- **MultiMonitorTool not found:** put the executable next to the source script or built app, use a supported subfolder, or set the tool-path environment variable.
- **Profile missing or monitor ID unavailable:** make sure Windows recognizes the intended displays, configure the correct layout, and save the profile again.
- **Switch fails or leaves all displays extended:** check the error shown on the web page and the latest entries in `display-switch.log`. The log records the commands, retries, and differences from the saved layout.
- **Windows reports the right layout, but the TV is blank:** confirm the TV is on and using the correct HDMI input. Verification checks Windows' display configuration; it cannot confirm the visible picture on the TV.
- **Another switch is already running:** wait for the current operation to finish before trying again.

Real display switching must be tested on Windows with the connected hardware. Check both directions, including after standby or a restart.

## Network access

Use the web interface on a trusted private network. The server listens on all network interfaces, and the token included in the page is a convenience check rather than authentication. Restrict port 5050 with Windows Firewall and do not expose it directly to the internet.

## Development

Run the controller tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

These tests simulate the display commands and verify profile parsing, monitor selection, retries, stable-layout checks, backup handling, and protection against overlapping commands. They do not change the connected displays.

## License

Copyright (c) 2026 **Cengizhan Aydin**.

This project is licensed under the [MIT License](LICENSE). You may use, modify, and redistribute the project, including commercially, provided you retain the copyright and license notice.

MultiMonitorTool is a separate NirSoft utility with its own [license terms](https://www.nirsoft.net/utils/multi_monitor_tool.html#license). It is downloaded separately, is not covered by this project's MIT license, and is not bundled into builds.
