# Documentation

Detailed behavior, design decisions, and technical notes for NVIDIA EC Brightness Bridge. For installation and everyday use, see the [README](README.md).

## Contents

- [How it works](#how-it-works)
- [Compatibility in detail](#compatibility-in-detail)
- [Tray wheel control](#tray-wheel-control)
- [Start with Windows](#start-with-windows)
- [Save brightness for startup](#save-brightness-for-startup)
- [Logs and diagnostics](#logs-and-diagnostics)
- [Run from source](#run-from-source)
- [Limitations](#limitations)
- [Security notes](#security-notes)
- [Technical references](#technical-references)

## How it works

Windows exposes panel brightness through WMI. When you move the slider or press a brightness key, the display driver raises a `WmiMonitorBrightnessEvent`. On an affected laptop, nothing downstream of that event reaches the backlight.

NVIDIA's driver publishes a separate firmware interface, `root\wmi:NvWmiBrightness`, that talks to the embedded controller. That path still works. The bridge simply connects the two.

A dedicated worker thread does the work:

1. Connects to `root\wmi` and locates the active `NvWmiBrightness` instance.
2. Confirms `NvGetSetBrightnessSource(0, 0)` reports source `2` (EC control), and refuses to continue if it does not.
3. Reads the firmware's maximum raw level, used to scale percentages.
4. Subscribes to `WmiMonitorBrightnessEvent` **before** its first synchronization, so no event is missed during startup.
5. Applies each requested percentage, then reads the level back for the log.

Short bursts of events are coalesced over a 75 ms window. Dragging the slider stays responsive without issuing a firmware write for every intermediate value.

Every 30 seconds the worker re-checks that EC is still the active brightness source. If that changes, it tears down the connection and rebuilds it.

All COM objects stay on the worker thread that created them. The thread initializes COM as multi-threaded, releases every reference on disconnect, and runs a collection pass before uninitializing.

### Percentage scaling

Percentages are scaled to the maximum raw level the firmware reports:

```text
raw = (maximum * percent + 50) / 100
```

The result is clamped to the range 1 to `maximum`. The minimum applied level is 1%, matching the tested firmware's behavior — it does not turn the backlight off.

After each write the app reads the level back and logs both the requested and reported values, which makes firmware rounding visible in the log.

## Compatibility in detail

All of the following must hold:

| Requirement | Why |
| --- | --- |
| Windows 11 | The app targets the Windows 11 tray and WMI behavior. |
| Active `root\wmi:NvWmiBrightness` instance | This is the interface the app writes to. It comes from the NVIDIA driver. |
| `NvGetSetBrightnessSource(0, 0)` returns `2` | Source `2` means the EC drives the backlight. Other sources mean a different path is in control. |
| Administrator rights | The NVIDIA WMI provider denied these method calls from a normal user process on the tested system. |
| Working `WmiMonitorBrightnessEvent` | Without these events the app has nothing to mirror. Tray scrolling still works. |

The app deliberately refuses to write when the active source is not EC. An unsupported machine stays in a waiting state rather than sending values down an unknown backlight path. The tray tooltip and log name the specific check that failed.

Verified on an ASUS ROG Strix G18 G814JI with an RTX 4070 Laptop GPU and a BOE NE180QDM-NZ2 panel. Other systems work only if their driver and firmware expose the same active EC interface.

## Tray wheel control

Hover over the tray icon and scroll to change brightness directly.

- One complete wheel notch is 5%. Up brightens, down dims.
- Partial deltas from high-resolution wheels and precision touchpads accumulate until they add up to a full notch, so slow scrolling is not discarded.
- Each successful EC change is pushed back to Windows, so the system slider stays in agreement.

The implementation installs a low-level mouse hook only while the pointer is inside the tray icon's rectangle, and removes it as soon as the pointer leaves. The hook consumes a wheel event only when it uses it for brightness. Every other mouse message passes through untouched.

The icon rectangle is re-read at most once per second while hovering, so the hit test follows the icon if the notification area rearranges itself.

If Windows refuses to install the hook, the app logs the failure, disables wheel control, and keeps mirroring brightness normally.

## Start with Windows

Checking this item registers a logon task for the current user at highest privilege, so there is no UAC prompt at every sign-in.

An elevated scheduled task must not point at a user-writable file. Anyone who could replace that file would gain an automatic elevation. The app therefore installs a protected copy first:

```text
C:\Program Files\NvidiaEcBrightnessBridge\NvidiaEcBrightnessBridge.exe
```

The directory is created with a protected access-control list: SYSTEM and Administrators have full control, and standard users may read and run the file but not replace it. Inherited permissions are blocked. The app refuses to use the location if it turns out to be a reparse point.

The copy is written to a temporary file in the same directory, size-checked, and then moved into place, so an interrupted copy cannot leave a truncated executable behind.

### Keeping the copy current

The task always launches the same fixed path. A copy left behind by an older build would keep starting that older build at every sign-in.

To prevent that, each start checks the installed copy and refreshes it when it is out of date:

- If the installed copy declares an **older** version than the running executable, it is replaced.
- If it declares a **newer** version, it is left alone. Running an old download does not downgrade your startup copy.
- If the versions match, size and modification time are compared. This catches a rebuild at the same version number.

Nothing is installed by this check. If no protected copy exists, it does nothing at all.

Upgrading is therefore just a matter of running the new executable once.

### Turning it off

Unchecking the item removes the current user's task. If no other user still has a bridge startup task, the unused protected copy is removed too. If that copy happens to be the executable currently running, it is left in place.

## Save brightness for startup

Checking this item records each successfully applied brightness value to:

```text
C:\ProgramData\NvidiaEcBrightnessBridge\settings.ini
```

The most recent value is applied once, when the next session connects to the EC interface. If the option is off, or the file holds no valid value from 1 through 100, the app falls back to the current Windows brightness.

Writes happen on the worker thread and never block the tray UI. The file is written to a temporary file, flushed, and moved into place, so an interrupted write cannot corrupt the stored value.

Unchecking the option stops both future updates and startup restoration.

This setting controls brightness restoration only. Use **Start with Windows** separately if the app should launch at sign-in.

## Logs and diagnostics

```text
C:\ProgramData\NvidiaEcBrightnessBridge\brightness-bridge.log
```

The log rotates at 1 MB and keeps three previous files. No manual cleanup is needed.

The app creates this directory with a protected access-control list because it runs elevated. The signed-in user can read the logs. Only Administrators and SYSTEM can modify them. The app refuses to use the path if it is a reparse point, and re-applies the protected list on every start.

**Open log** in the tray menu opens the file through Explorer rather than starting the handler directly. That matters: a text editor launched straight from this process would inherit its administrator token, turning an ordinary Save-As dialog into an elevated file browser. Handing the request to the desktop shell means the editor starts with your own token instead.

### Tray statuses

| Status | Meaning |
| --- | --- |
| `Connected: n%` | Events are being mirrored to the EC. |
| `Connecting` / `Reconnecting` | The connection is being built or rebuilt. |
| `Waiting: No active NvWmiBrightness instance was found` | The current driver, firmware, or GPU mode does not expose the required provider. |
| `Waiting: Brightness source is ... not EC (2)` | The interface exists, but this workaround does not apply in the current display mode. |

A waiting state is retried every three seconds.

### Recovery behavior

The app rebuilds its WMI connection after a display topology change (`WM_DISPLAYCHANGE`) and after resume from sleep. **Reconnect display interface** in the tray menu forces the same rebuild by hand.

If Explorer restarts, the tray icon is re-added automatically.

## Run from source

```powershell
python -m pip install -r requirements.txt
python .\nvidia_ec_brightness_bridge.py
```

The script relaunches itself with administrator permission.

**Start with Windows** is unavailable when running from source. It requires a single frozen executable to copy into the protected directory.

To run the tests:

```powershell
python -m unittest discover -s tests -v
```

`build.ps1` runs a syntax check and the full test suite before it packages anything.

## Limitations

This is a narrow compatibility bridge, not a universal brightness fix. It cannot:

- create the NVIDIA WMI interface if the driver does not provide it,
- repair a broken embedded controller,
- make Windows emit brightness events that are not being raised,
- or drive a display controlled through a different vendor's protocol.

NVIDIA's WMI calls are synchronous. A firmware or provider call that hangs cannot be safely cancelled from a Python thread. Ordinary disconnects and errors are retried.

## Security notes

The app runs elevated, so it is deliberately careful about anything it writes or launches:

- Both data directories are created with protected access-control lists and re-checked on every start. Reparse points are refused.
- The startup copy is installed under `C:\Program Files`, where a standard user cannot replace it.
- Files and links opened from the tray menu are handed to the desktop shell, so no handler inherits the administrator token.
- Explorer is launched by absolute path, so a planted executable in the working directory cannot be run in its place.
- Only two window messages are exempted from the process's UIPI filter: the tray icon callback and Explorer's restart broadcast.

This project uses the PyInstaller one-file format. PyInstaller documents an added temporary-extraction risk for elevated one-file applications. Protecting the scheduled-task copy stops a normal user from replacing the outer executable, but it does not remove that extraction risk. For high-security environments, prefer a one-folder build installed in an administrator-protected directory.

Released executables are not code-signed, so SmartScreen will warn about them. Build from source if you would rather not rely on a downloaded binary.

## Technical references

- [Microsoft: WmiMonitorBrightnessEvent class](https://learn.microsoft.com/en-us/windows/win32/wmicoreprov/wmimonitorbrightnessevent)
- [Microsoft: WmiSetBrightness method](https://learn.microsoft.com/en-us/windows/win32/wmicoreprov/wmisetbrightness-method-in-class-wmimonitorbrightnessmethods)
- [Microsoft: SWbemEventSource.NextEvent](https://learn.microsoft.com/en-us/windows/win32/wmisdk/swbemeventsource-nextevent)
- [Microsoft: Task Scheduler Principal.RunLevel](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-runlevel)
- [Microsoft: ChangeWindowMessageFilterEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-changewindowmessagefilterex)
- [Microsoft: TrackPopupMenu](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-trackpopupmenu)
- [Microsoft: Constructing WMI input parameter objects](https://learn.microsoft.com/en-us/windows/win32/wmisdk/constructing-inparameters-objects-and-parsing-outparameters-objects)
- [Linux kernel: NVIDIA WMI EC backlight protocol](https://github.com/torvalds/linux/blob/master/include/linux/platform_data/x86/nvidia-wmi-ec-backlight.h)
- [PyInstaller: how one-file programs work](https://pyinstaller.org/en/stable/operating-mode.html#how-the-one-file-program-works)
