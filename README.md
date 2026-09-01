# NVIDIA EC Brightness Bridge

![icon](.assets/banner.png)

NVIDIA EC Brightness Bridge is a standalone Windows 11 system-tray application for a specific hybrid-GPU laptop failure: after switching the internal display to the NVIDIA GPU with a hardware MUX, Windows still moves its brightness slider but the panel backlight does not change.

The application listens for Windows brightness events and mirrors each requested percentage to NVIDIA's firmware-provided `NvWmiBrightness` EC interface. It does not replace a graphics driver, patch Windows, modify Windhawk, or change the laptop's brightness-source setting.

The underlying bridge was verified on an ASUS ROG Strix G18 G814JI with an NVIDIA GeForce RTX 4070 Laptop GPU and BOE NE180QDM-NZ2 panel. Other systems will work only if their NVIDIA driver and firmware expose the same active EC interface.

## Compatibility requirements

- Windows 11.
- An active `root\wmi:NvWmiBrightness` instance.
- `NvGetSetBrightnessSource(0, 0)` must report source `2`, which means EC control.
- Administrator permission. The NVIDIA WMI provider denied the required method calls from a normal user process on the tested system.
- The Windows brightness slider or brightness keys must still produce `WmiMonitorBrightnessEvent` events.

The application deliberately refuses to write when the active source is not EC. Unsupported computers remain in a waiting state instead of sending values to an unknown backlight path.

## Build

Install 64-bit Python 3.12 or later, open PowerShell in this directory, and run:

```powershell
.\build.ps1
```

The standalone executable is created at:

```text
dist\NvidiaEcBrightnessBridge.exe
```

Only this executable is needed to run the application. It has no PowerShell installer or uninstaller. Windows SmartScreen might warn about it because a locally built executable is not code-signed; review the source and build it yourself rather than using repackaged copies from untrusted sources.

The build script runs the included unit tests before packaging. You can also run them directly with `python -m unittest discover -s tests -v`.

## Use

Run `NvidiaEcBrightnessBridge.exe` and approve the UAC prompt. Find NVIDIA EC Brightness Bridge in the notification area; Windows may initially place it in the tray overflow menu. Adjust the normal Windows brightness slider or use the laptop brightness keys, and the physical backlight should follow.

Right-click the tray icon to see the current status, synchronize immediately, reconnect after a display-stack change, open the log, enable or disable `Start with Windows`, save the brightness for startup, or exit. Double-clicking the icon also synchronizes the current Windows value.

The worker subscribes before its initial synchronization, coalesces short bursts of slider events, checks that EC remains the active source, and rebuilds its WMI connection after display topology changes or resume from sleep. Every COM object remains on the dedicated worker thread that created it.

## Start with Windows

Check `Start with Windows` in the tray menu to create a highest-privilege logon task for the current user. This avoids a UAC prompt at every sign-in. Because an elevated scheduled task must not point to a user-writable download, the app first copies the standalone executable to:

```text
C:\Program Files\NvidiaEcBrightnessBridge\NvidiaEcBrightnessBridge.exe
```

The copied directory is protected so standard users can read and run the file but cannot replace it. Unchecking `Start with Windows` removes the current user's task. When no other user has a bridge startup task, the app removes an unused protected copy; if that copy is the currently running executable, it is safely left in place.

## Save brightness for startup

Check `Save brightness for startup` in the tray menu to remember each successfully applied brightness value. The most recent value is stored at `C:\ProgramData\NvidiaEcBrightnessBridge\settings.ini` and applied once when the next app session connects to the NVIDIA EC interface. If the option is disabled or the INI file has no valid value from 1 through 100, the app uses the current Windows brightness value as before.

The saved value is updated on the bridge's worker thread and never blocks the tray UI. Unchecking the option stops future updates and startup restoration. This setting controls brightness restoration only; use `Start with Windows` separately if the app should launch automatically at sign-in.

## Remove the application

Uncheck `Start with Windows`, choose `Exit`, and delete the standalone executable wherever you saved it. No uninstaller is required. Diagnostic logs and brightness settings are intentionally retained under `C:\ProgramData\NvidiaEcBrightnessBridge`; an administrator can delete that directory if they are no longer needed.

## Logs

Logs are stored at:

```text
C:\ProgramData\NvidiaEcBrightnessBridge\brightness-bridge.log
```

The application uses one append-only log file without automatic rotation or a size limit. Delete or truncate the file manually when the application is not running if it becomes too large.

The application creates this directory with a protected access-control list because it runs elevated. The signed-in user can read the logs, while only Administrators and SYSTEM can modify them.

Common tray statuses:

- `Connected`: slider events are being mirrored to the EC.
- `Connecting` or `Reconnecting`: the display stack changed or the WMI connection is being rebuilt.
- `Waiting: No active NvWmiBrightness instance was found`: the current driver, firmware, or GPU mode does not expose the required provider.
- `Waiting: Brightness source is ... not EC (2)`: the interface exists, but this workaround is not valid in the current display mode.

## Run from source

For development, install the runtime dependency and launch the script:

```powershell
python -m pip install -r requirements.txt
python .\nvidia_ec_brightness_bridge.py
```

The script relaunches itself with administrator permission. The `Start with Windows` menu item is available only in the built executable.

## Limitations and security note

This is a narrow compatibility bridge, not a universal brightness fix. It cannot create the NVIDIA WMI interface, repair a broken EC, make Windows emit missing brightness events, or support a display controlled through a different vendor protocol. NVIDIA WMI calls are synchronous; a firmware or provider call that hangs cannot be safely cancelled from a Python thread, although normal disconnects and errors are retried.

The minimum applied level is 1% to match the tested firmware behavior. The application scales percentages to the maximum raw level reported by the NVIDIA provider and reads the level back after each write for diagnostics.

This project uses the requested PyInstaller one-file format. PyInstaller documents that an elevated one-file application has additional temporary-extraction risk. Protecting the scheduled-task copy prevents a normal user from replacing the outer executable, but it cannot remove that one-file extraction risk. For high-security environments, prefer a one-folder build installed in an administrator-protected directory.

## Technical references

- [Microsoft: WmiMonitorBrightnessEvent class](https://learn.microsoft.com/en-us/windows/win32/wmicoreprov/wmimonitorbrightnessevent)
- [Microsoft: SWbemEventSource.NextEvent](https://learn.microsoft.com/en-us/windows/win32/wmisdk/swbemeventsource-nextevent)
- [Microsoft: Task Scheduler Principal.RunLevel](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-runlevel)
- [Microsoft: ChangeWindowMessageFilterEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changewindowmessagefilterex)
- [Microsoft: TrackPopupMenu](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenu)
- [Microsoft: Constructing WMI input parameter objects](https://learn.microsoft.com/en-us/windows/win32/wmisdk/constructing-inparameters-objects-and-parsing-outparameters-objects)
- [Linux kernel: NVIDIA WMI EC backlight protocol](https://github.com/torvalds/linux/blob/master/include/linux/platform_data/x86/nvidia-wmi-ec-backlight.h)
- [PyInstaller: how one-file programs work](https://pyinstaller.org/en/stable/operating-mode.html#how-the-one-file-program-works)

## License

The project is available under the [MIT License](LICENSE). See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for build and runtime dependency information.
