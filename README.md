# NVIDIA EC Brightness Bridge

![icon](.assets/banner.png)

Some hybrid-GPU laptops lose brightness control after a hardware MUX switch to the NVIDIA GPU. Windows still moves its slider but the backlight ignores it.

This app fixes that. It watches Windows brightness events and forwards each one to NVIDIA's `NvWmiBrightness` EC interface, which the panel does listen to.

It a lite weight app that sits in the notification area and needs no configuration.

## Features

- 💡 **Brightness adjust works again** － The slider, the brightness keys, and anything else that moves the Windows level all reach the backlight.
- 🎚️ **Scroll to dim** － Hover over the tray icon and scroll. One notch is 5%. Precision touchpads work too.
- 🔄 **Stays in sync** － Changes made from the tray are pushed back to Windows, so the system slider always matches the panel.
- 🔗 **Start with Windows** － An opt-in logon task, with no UAC prompt at sign-in. It updates itself when you run a newer build.
- 💾 **Remembers your brightness** － Optionally restore the last level when the app starts.

## Will it work on my laptop?

This is a narrow fix for a specific failure, not a general brightness utility. You need all of:

- Windows 11
- An active `root\wmi:NvWmiBrightness` instance from the NVIDIA driver
- `NvGetSetBrightnessSource(0, 0)` reporting source `2`, meaning EC control
- Administrator rights
- Brightness keys or slider that still raise `WmiMonitorBrightnessEvent`

If a check fails, the app waits and the tray tooltip tells you which one.

> Verified on an ASUS ROG Strix G18 G814JI with an RTX 4070 Laptop GPU and a BOE NE180QDM-NZ2 panel. Other systems work only if their driver and firmware expose the same active EC interface.

## Install

1. Download `NvidiaEcBrightnessBridge.exe` from the [releases page](https://github.com/EnderDragonEP/NVIDIA-EC-brightness-bridge-for-Windows-11/releases), or build it yourself.
2. Run it and approve the UAC prompt.
3. Find the icon in the notification area.
4. Adjust brightness the way you normally would.

That single file is the whole application. There is no installer.

The executable is not code-signed, so SmartScreen will warn about it. Build from source if you would rather not trust a downloaded binary.

## Tray menu

Right-click the icon.

| Item | What it does |
| --- | --- |
| `Version x.y.z` | Opens the releases page |
| `Status: ...` | Current state of the bridge |
| `Synchronize now` | Re-reads the Windows level and applies it |
| `Reconnect display interface` | Rebuilds the connection after a display change |
| `Open log` | Opens the log file |
| `Start with Windows` | Adds or removes the logon task |
| `Save brightness for startup` | Remembers the current level |
| `Exit` | Quits |

Double-click the icon to synchronize. Hover and scroll to adjust brightness.

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

Uncheck **Start with Windows**, choose **Exit**, and delete the executable. There is no uninstaller.

Logs and settings stay under `C:\ProgramData\NvidiaEcBrightnessBridge`. An administrator can delete that folder.

## Documentation

[DOCUMENTATION.md](DOCUMENTATION.md) covers how the bridge works, the security design behind the protected directories, every tray status, running from source, and the technical references.

## License

[MIT](LICENSE). Dependency information is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Icon and banner made with [draw.io](https://github.com/jgraph/drawio).
