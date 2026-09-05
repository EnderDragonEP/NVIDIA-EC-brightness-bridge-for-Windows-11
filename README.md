# NVIDIA EC Brightness Bridge

![icon](.assets/banner.png)

![Version](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fraw.githubusercontent.com%2FEnderDragonEP%2FNVIDIA-EC-brightness-bridge-for-Windows-11%2Fmain%2Fversion_info.txt&search=FileVersion%27%2C%20u%27%28%5B0-9.%5D%2B%29%27&replace=%241&label=version&color=blue)
![Last updated](https://img.shields.io/github/last-commit/EnderDragonEP/NVIDIA-EC-brightness-bridge-for-Windows-11)
![Python](https://img.shields.io/badge/python-3.12%2B-blue)

Some hybrid-GPU laptops lose brightness control after a hardware MUX switch to the NVIDIA GPU. Windows still moves its slider but the backlight ignores it.

This app fixes that issue once and for all! It listens to Windows brightness events and forwards each adjustment to NVIDIA's `NvWmiBrightness` EC interface, which the build-in display usually listen to.

It a lite weight app that sits in the notification area and needs no complex configuration.

## Features

- 💡 **Adjust works again** – The slider and brightness keys actually does what it should.
- 🎚️ **Scroll to dim** – Hover over the tray icon and scroll to adjusts the brightness in 5% increments.
- 🔄 **Stays in sync** – Brightness slider always stays in sync with the panel brightness.
- 🔗 **Start with Windows** – Automatically starts with a optional toggle.
- 💾 **Remembers your brightness** – Restore the last level when the app starts.

## Will it work on my laptop?

This is a narrow fix for a specific failure, not a general brightness utility. You'll need the following requirements:

- Windows 11
- An active `root\wmi:NvWmiBrightness` instance from the NVIDIA driver
- `NvGetSetBrightnessSource(0, 0)` reporting source `2`, meaning EC control
- Administrator privileges
- Brightness keys or slider that still raise `WmiMonitorBrightnessEvent`

If a check fails, the app will show an error tooltip message.

> Tested on an ASUS ROG Strix G18 G814JI with an RTX 4070 Laptop GPU and a BOE NE180QDM-NZ2 panel. Other systems will work only if the driver and firmware expose the same active EC interface.

## Install

1. Download `NvidiaEcBrightnessBridge.exe` from the [releases page](https://github.com/EnderDragonEP/NVIDIA-EC-brightness-bridge-for-Windows-11/releases), or build it yourself.
2. Run it and approve the UAC prompt.
3. Find the icon in the notification area.
4. Adjust brightness the way you normally would.
5. Profit!

It's a fully portable, standalone application. There is no installer.

The executable is not code-signed, so SmartScreen will warn about it. Some antivirus software may also flag it. Build from source if you would rather not trust the downloaded binary.

## Tray menu

Context menu items are:

| Item | What it does |
| --- | --- |
| `Version x.y.z` | Opens the releases page |
| `Status: ...` | Current state of the bridge |
| `Synchronize now` | Re-synchronize the brightness level |
| `Reconnect display interface` | Rebuilds the connection after a display change |
| `Open log` | Opens the log file |
| `Start with Windows` | Adds the app to the startup items |
| `Save brightness for startup` | Restores the last brightness level on startup |
| `Exit` | Exits the application |

Double-click the icon to synchronize. Hover and scroll to adjust brightness in 5% increments.

## Where it keeps things

| Path | Contents |
| --- | --- |
| `C:\ProgramData\NvidiaEcBrightnessBridge\` | Log and settings |
| `C:\Program Files\NvidiaEcBrightnessBridge\` | Protected copy, only if **Start with Windows** is on |

## Build

Install 64-bit Python 3.12 or later, then run:

```powershell
.\build.ps1
```

The executable appears at `dist\NvidiaEcBrightnessBridge.exe`. The script runs the unit tests before packaging.

## Uninstall

Uncheck **Start with Windows**, click **Exit**, then delete the executable in `C:\Program Files\NvidiaEcBrightnessBridge\`. There is no uninstaller.

Logs and settings stay under `C:\ProgramData\NvidiaEcBrightnessBridge`.

## Documentation

Detailed documentation: [DOCUMENTATION.md](DOCUMENTATION.md)

## License

[MIT](LICENSE). Dependency information is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Icon and banner made with [draw.io](https://github.com/jgraph/drawio).
