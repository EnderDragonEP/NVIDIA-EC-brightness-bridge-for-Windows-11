"""NVIDIA EC brightness bridge for Windows 11.

Mirrors Windows brightness events to NVIDIA's privileged NvWmiBrightness
firmware interface and exposes a small native system-tray application.
"""

from __future__ import annotations

import configparser
import ctypes
from ctypes import wintypes
import gc
import logging
import logging.handlers
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

import pythoncom
import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32gui
import win32com.client
import win32security
import winerror
import ntsecuritycon


APP_NAME = "NVIDIA EC Brightness Bridge"
APP_ID = "NvidiaEcBrightnessBridge"
APP_VERSION = "1.0.0"
MUTEX_NAME = rf"Local\{APP_ID}"
RELEASES_URL = (
    "https://github.com/EnderDragonEP/"
    "NVIDIA-EC-brightness-bridge-for-Windows-11/releases"
)

NV_BRIGHTNESS_CLASS = "NvWmiBrightness"
NV_LEVEL_METHOD = "NvGetSetBrightnessLevel"
NV_SOURCE_METHOD = "NvGetSetBrightnessSource"
WINDOWS_BRIGHTNESS_CLASS = "WmiMonitorBrightness"
WINDOWS_BRIGHTNESS_EVENT_CLASS = "WmiMonitorBrightnessEvent"
WINDOWS_BRIGHTNESS_METHODS_CLASS = "WmiMonitorBrightnessMethods"
WINDOWS_SET_BRIGHTNESS_METHOD = "WmiSetBrightness"

MODE_GET = 0
MODE_SET = 1
MODE_GET_MAX = 2
SOURCE_EC = 2
MIN_BRIGHTNESS_PERCENT = 1
WMI_TIMEOUT_HRESULT = 0x80043001
WMI_QUERY_FLAGS = 0x10 | 0x20  # Return immediately | forward-only.
HEALTH_CHECK_INTERVAL_SECONDS = 30.0
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
WMI_EVENT_POLL_MILLISECONDS = 100
MSGFLT_ALLOW = 1
NIM_SETFOCUS = 3
TRAY_ICON_ID = 0
WHEEL_DELTA = 120
WHEEL_ADJUSTMENT_PERCENT = 5
WH_MOUSE_LL = 14
HC_ACTION = 0
PM_NOREMOVE = 0

USER32 = ctypes.WinDLL("user32", use_last_error=True)
USER32.ChangeWindowMessageFilterEx.argtypes = (
    wintypes.HWND,
    wintypes.UINT,
    wintypes.DWORD,
    wintypes.LPVOID,
)
USER32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
USER32.GetShellWindow.argtypes = ()
USER32.GetShellWindow.restype = wintypes.HWND


class Guid(ctypes.Structure):
    _fields_ = (
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", wintypes.BYTE * 8),
    )


class NotifyIconIdentifier(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", Guid),
    )


class LowLevelMouseInfo(ctypes.Structure):
    _fields_ = (
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    )


LOW_LEVEL_MOUSE_CALLBACK = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)

USER32.SetWindowsHookExW.argtypes = (
    ctypes.c_int,
    LOW_LEVEL_MOUSE_CALLBACK,
    wintypes.HINSTANCE,
    wintypes.DWORD,
)
USER32.SetWindowsHookExW.restype = wintypes.HANDLE
USER32.CallNextHookEx.argtypes = (
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
USER32.CallNextHookEx.restype = ctypes.c_ssize_t
USER32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
USER32.UnhookWindowsHookEx.restype = wintypes.BOOL
USER32.GetMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
)
USER32.GetMessageW.restype = ctypes.c_int
USER32.PeekMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
)
USER32.PeekMessageW.restype = wintypes.BOOL
USER32.PostThreadMessageW.argtypes = (
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
)
USER32.PostThreadMessageW.restype = wintypes.BOOL

KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
KERNEL32.GetCurrentThreadId.argtypes = ()
KERNEL32.GetCurrentThreadId.restype = wintypes.DWORD
KERNEL32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
KERNEL32.GetModuleHandleW.restype = wintypes.HMODULE

SHELL32 = ctypes.WinDLL("shell32", use_last_error=True)
SHELL32.Shell_NotifyIconGetRect.argtypes = (
    ctypes.POINTER(NotifyIconIdentifier),
    ctypes.POINTER(wintypes.RECT),
)
SHELL32.Shell_NotifyIconGetRect.restype = ctypes.c_long

WM_TRAYICON = win32con.WM_USER + 20
WM_STATUS_CHANGED = win32con.WM_APP + 1
WM_WHEEL_HOOK_ARM = win32con.WM_APP + 20
WM_WHEEL_HOOK_DISARM = win32con.WM_APP + 21
WM_WHEEL_HOOK_STOP = win32con.WM_APP + 22
# Broadcast by Explorer after it restarts; every tray icon must be re-added.
TASKBAR_CREATED = win32gui.RegisterWindowMessage("TaskbarCreated")

MENU_STATUS = 1001
MENU_SYNC = 1002
MENU_RECONNECT = 1003
MENU_OPEN_LOG = 1004
MENU_STARTUP = 1005
MENU_EXIT = 1006
MENU_SAVE_BRIGHTNESS = 1007
MENU_VERSION = 1008

TASK_NAME_PREFIX = APP_ID
STARTUP_INSTALL_DIRECTORY = APP_ID


def windows_special_folder(csidl: int) -> Path:
    """Resolve a system-selected folder without trusting environment variables."""
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(
        None,
        csidl,
        None,
        0,
        buffer,
    )
    if result != 0 or not buffer.value:
        raise OSError(result, f"Windows could not locate special folder {csidl}")
    return Path(buffer.value)


def common_application_data_dir() -> Path:
    # CSIDL_COMMON_APPDATA = 0x23.
    return windows_special_folder(0x23)


def program_files_dir() -> Path:
    # CSIDL_PROGRAM_FILES = 0x26.
    return windows_special_folder(0x26)


def windows_dir() -> Path:
    # CSIDL_WINDOWS = 0x24.
    return windows_special_folder(0x24)


def _protected_directory(path: Path, reader_sid: Any) -> Path:
    """Create a non-reparse-point directory with a protected DACL."""
    system_sid = win32security.CreateWellKnownSid(
        win32security.WinLocalSystemSid
    )
    administrators_sid = win32security.CreateWellKnownSid(
        win32security.WinBuiltinAdministratorsSid
    )
    inheritance = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        inheritance,
        ntsecuritycon.FILE_ALL_ACCESS,
        system_sid,
    )
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        inheritance,
        ntsecuritycon.FILE_ALL_ACCESS,
        administrators_sid,
    )
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        inheritance,
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE,
        reader_sid,
    )

    if not path.exists():
        descriptor = win32security.SECURITY_DESCRIPTOR()
        descriptor.SetSecurityDescriptorDacl(True, dacl, False)
        descriptor.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED,
            win32security.SE_DACL_PROTECTED,
        )
        security_attributes = win32security.SECURITY_ATTRIBUTES()
        security_attributes.SECURITY_DESCRIPTOR = descriptor
        try:
            win32file.CreateDirectory(str(path), security_attributes)
        except pywintypes.error as error:
            if error.winerror != winerror.ERROR_ALREADY_EXISTS:
                raise

    attributes = win32file.GetFileAttributes(str(path))
    if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
        raise BridgeError(f"Refusing to use reparse-point log path: {path}")
    if not path.is_dir():
        raise BridgeError(f"Log path is not a directory: {path}")

    security_information = (
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        security_information,
        None,
        None,
        dacl,
        None,
    )
    return path


def _protected_data_directory() -> Path:
    process_token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32security.TOKEN_QUERY,
    )
    user_sid = win32security.GetTokenInformation(
        process_token,
        win32security.TokenUser,
    )[0]
    return _protected_directory(
        common_application_data_dir() / APP_ID,
        user_sid,
    )


LOG_PATH: Path | None = None
SETTINGS_PATH: Path | None = None


def configure_logging() -> None:
    global LOG_PATH, SETTINGS_PATH
    data_directory = _protected_data_directory()
    LOG_PATH = data_directory / "brightness-bridge.log"
    SETTINGS_PATH = data_directory / "settings.ini"
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)
    win32gui.set_logger(root_logger)


class BrightnessSettings:
    SECTION = "startup"
    ENABLED_KEY = "restore_brightness"
    VALUE_KEY = "brightness"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._enabled = False
        self._brightness: int | None = None
        self._load()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def brightness(self) -> int | None:
        with self._lock:
            return self._brightness

    def set_enabled(self, enabled: bool, brightness: int | None = None) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            if brightness is not None:
                self._brightness = self._validated_brightness(brightness)
            elif enabled:
                self._brightness = None
            self._write_locked()

    def record_brightness(self, brightness: int) -> None:
        with self._lock:
            if not self._enabled:
                return
            brightness = self._validated_brightness(brightness)
            if brightness == self._brightness:
                return
            self._brightness = brightness
            self._write_locked()

    @staticmethod
    def _validated_brightness(brightness: int) -> int:
        brightness = int(brightness)
        if not MIN_BRIGHTNESS_PERCENT <= brightness <= 100:
            raise ValueError(f"Brightness must be between 1 and 100: {brightness}")
        return brightness

    def _load(self) -> None:
        if not self.path.exists():
            return

        parser = configparser.ConfigParser()
        try:
            with self.path.open("r", encoding="utf-8") as settings_file:
                parser.read_file(settings_file)
            enabled = parser.getboolean(
                self.SECTION,
                self.ENABLED_KEY,
                fallback=False,
            )
            raw_brightness = parser.get(
                self.SECTION,
                self.VALUE_KEY,
                fallback=None,
            )
            brightness = (
                self._validated_brightness(int(raw_brightness))
                if raw_brightness is not None
                else None
            )
        except (OSError, ValueError, configparser.Error) as error:
            logging.warning("Could not read brightness settings: %s", error)
            return

        with self._lock:
            self._enabled = enabled
            self._brightness = brightness
        logging.info(
            "Brightness startup restore loaded (enabled=%s, value=%s)",
            enabled,
            brightness,
        )

    def _write_locked(self) -> None:
        parser = configparser.ConfigParser()
        parser[self.SECTION] = {
            self.ENABLED_KEY: "true" if self._enabled else "false",
        }
        if self._brightness is not None:
            parser[self.SECTION][self.VALUE_KEY] = str(self._brightness)

        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as settings_file:
                parser.write(settings_file)
                settings_file.flush()
                os.fsync(settings_file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def show_message(message: str, title: str = APP_NAME, error: bool = False) -> None:
    flags = win32con.MB_OK | (
        win32con.MB_ICONERROR if error else win32con.MB_ICONINFORMATION
    )
    win32api.MessageBox(0, message, title, flags)


def open_as_user(target: Path | str) -> None:
    """Open a file or URL without handing the elevated token to the handler.

    Started directly, the handler would inherit this process's administrator
    token, which turns a text editor's Save-As dialog into an elevated file
    browser and a web browser into an elevated one. Explorer instead forwards
    the request to the already-running desktop shell, so the handler starts
    with the user's own token.
    """
    if not USER32.GetShellWindow():
        raise BridgeError(
            "The Windows shell is not running, so this cannot be opened "
            "without administrator rights"
        )
    # Launch Explorer by absolute path. CreateProcess resolves a bare name
    # against this executable's own directory and the working directory before
    # the system directory, and both are user-writable for a downloaded copy.
    subprocess.Popen(
        [os.fspath(windows_dir() / "explorer.exe"), os.fspath(target)],
        creationflags=subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )


def allow_lower_integrity_window_message(hwnd: int, message: int) -> None:
    """Allow one explicit callback through this window's UIPI filter."""
    if not USER32.ChangeWindowMessageFilterEx(
        hwnd,
        message,
        MSGFLT_ALLOW,
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


def tray_icon_rect(hwnd: int) -> tuple[int, int, int, int]:
    identifier = NotifyIconIdentifier()
    identifier.cbSize = ctypes.sizeof(identifier)
    identifier.hWnd = hwnd
    identifier.uID = TRAY_ICON_ID
    rectangle = wintypes.RECT()
    result = SHELL32.Shell_NotifyIconGetRect(
        ctypes.byref(identifier),
        ctypes.byref(rectangle),
    )
    if result < 0:
        raise OSError(result, "Windows could not locate the tray icon")
    return (
        rectangle.left,
        rectangle.top,
        rectangle.right,
        rectangle.bottom,
    )


def accumulate_wheel_delta(remainder: int, delta: int) -> tuple[int, int]:
    total = int(remainder) + int(delta)
    if total >= 0:
        steps = total // WHEEL_DELTA
    else:
        steps = -((-total) // WHEEL_DELTA)
    return steps, total - steps * WHEEL_DELTA


class TrayWheelListener(threading.Thread):
    """Capture wheel input only while the pointer is over the tray icon."""

    def __init__(self, adjustment_callback: Callable[[int], None]) -> None:
        super().__init__(name="TrayWheelListener", daemon=True)
        self._adjustment_callback = adjustment_callback
        self._ready_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread_id: int | None = None
        self._icon_rect: tuple[int, int, int, int] | None = None
        self._hook_handle: int | None = None
        self._wheel_remainder = 0
        self._disabled = False
        self._hook_callback = LOW_LEVEL_MOUSE_CALLBACK(self._mouse_hook)

    def wait_until_ready(self, timeout: float) -> bool:
        return self._ready_event.wait(timeout)

    def arm(self, rectangle: tuple[int, int, int, int]) -> None:
        with self._state_lock:
            if self._disabled:
                return
            self._icon_rect = rectangle
            thread_id = self._thread_id
        if thread_id is not None:
            USER32.PostThreadMessageW(
                thread_id,
                WM_WHEEL_HOOK_ARM,
                0,
                0,
            )

    def stop(self) -> None:
        with self._state_lock:
            thread_id = self._thread_id
        if thread_id is not None:
            USER32.PostThreadMessageW(
                thread_id,
                WM_WHEEL_HOOK_STOP,
                0,
                0,
            )

    def run(self) -> None:
        message = wintypes.MSG()
        with self._state_lock:
            self._thread_id = KERNEL32.GetCurrentThreadId()
        # Force Windows to create this thread's message queue before arm() can
        # post commands to it.
        USER32.PeekMessageW(
            ctypes.byref(message),
            None,
            0,
            0,
            PM_NOREMOVE,
        )
        self._ready_event.set()

        try:
            while True:
                result = USER32.GetMessageW(
                    ctypes.byref(message),
                    None,
                    0,
                    0,
                )
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0 or message.message == WM_WHEEL_HOOK_STOP:
                    break
                if message.message == WM_WHEEL_HOOK_ARM:
                    self._install_hook()
                elif message.message == WM_WHEEL_HOOK_DISARM:
                    self._uninstall_hook()
        except Exception:
            logging.exception("Tray wheel listener failed")
        finally:
            self._uninstall_hook()
            with self._state_lock:
                self._thread_id = None
            self._ready_event.set()

    def _install_hook(self) -> None:
        if self._hook_handle or self._disabled:
            return
        module = KERNEL32.GetModuleHandleW(None)
        hook_handle = USER32.SetWindowsHookExW(
            WH_MOUSE_LL,
            self._hook_callback,
            module,
            0,
        )
        if not hook_handle:
            error = ctypes.WinError(ctypes.get_last_error())
            with self._state_lock:
                self._disabled = True
            logging.error("Could not enable tray wheel control: %s", error)
            return
        self._hook_handle = hook_handle
        logging.info("Tray wheel control armed")

    def _uninstall_hook(self) -> None:
        hook_handle = self._hook_handle
        if hook_handle:
            if not USER32.UnhookWindowsHookEx(hook_handle):
                logging.warning(
                    "Could not disarm tray wheel control: %s",
                    ctypes.WinError(ctypes.get_last_error()),
                )
            self._hook_handle = None
        with self._state_lock:
            self._icon_rect = None
            self._wheel_remainder = 0

    def _mouse_hook(
        self,
        code: int,
        message: int,
        data_pointer: int,
    ) -> int:
        if code == HC_ACTION:
            mouse = ctypes.cast(
                data_pointer,
                ctypes.POINTER(LowLevelMouseInfo),
            ).contents
            with self._state_lock:
                rectangle = self._icon_rect

            inside = bool(
                rectangle
                and rectangle[0] <= mouse.pt.x < rectangle[2]
                and rectangle[1] <= mouse.pt.y < rectangle[3]
            )
            if not inside and message == win32con.WM_MOUSEMOVE:
                thread_id = self._thread_id
                if thread_id is not None:
                    USER32.PostThreadMessageW(
                        thread_id,
                        WM_WHEEL_HOOK_DISARM,
                        0,
                        0,
                    )
            elif inside and message == win32con.WM_MOUSEWHEEL:
                delta = ctypes.c_short(mouse.mouseData >> 16).value
                with self._state_lock:
                    steps, self._wheel_remainder = accumulate_wheel_delta(
                        self._wheel_remainder,
                        delta,
                    )
                if steps:
                    self._adjustment_callback(steps)
                return 1

        return USER32.CallNextHookEx(
            self._hook_handle,
            code,
            message,
            data_pointer,
        )


def is_administrator() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_elevated() -> bool:
    if getattr(sys, "frozen", False):
        executable = sys.executable
        arguments = sys.argv[1:]
    else:
        executable = sys.executable
        arguments = [str(Path(__file__).resolve()), *sys.argv[1:]]

    parameters = subprocess.list2cmdline(arguments)
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        parameters,
        None,
        win32con.SW_SHOWNORMAL,
    )
    return result > 32


def underlying_hresult(error: pywintypes.com_error) -> int:
    """Extract a WMI HRESULT hidden inside an Automation exception."""
    exception_info = getattr(error, "excepinfo", None)
    if (
        exception_info
        and len(exception_info) > 5
        and isinstance(exception_info[5], int)
        and exception_info[5]
    ):
        return int(exception_info[5]) & 0xFFFFFFFF
    return int(getattr(error, "hresult", 0)) & 0xFFFFFFFF


class BridgeError(RuntimeError):
    pass


class ReconnectRequested(Exception):
    pass


class StartupTaskManager:
    """Manage an opt-in, highest-privilege per-user logon task."""

    TASK_CREATE_OR_UPDATE = 6
    TASK_LOGON_INTERACTIVE_TOKEN = 3
    TASK_RUNLEVEL_HIGHEST = 1
    TASK_TRIGGER_LOGON = 9
    TASK_ACTION_EXEC = 0
    TASK_INSTANCES_IGNORE_NEW = 2
    TASK_NOT_FOUND_HRESULTS = {0x80070002}

    @staticmethod
    def _scheduler_service() -> Any:
        service = win32com.client.Dispatch("Schedule.Service")
        service.Connect()
        return service

    @staticmethod
    def _current_user_sid_string() -> str:
        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32security.TOKEN_QUERY,
        )
        user_sid = win32security.GetTokenInformation(
            process_token,
            win32security.TokenUser,
        )[0]
        return win32security.ConvertSidToStringSid(user_sid)

    @classmethod
    def _task_name(cls) -> str:
        return f"{TASK_NAME_PREFIX}-{cls._current_user_sid_string()}"

    @staticmethod
    def installed_executable() -> Path:
        return (
            program_files_dir()
            / STARTUP_INSTALL_DIRECTORY
            / f"{APP_ID}.exe"
        )

    @staticmethod
    def _file_version(path: Path) -> tuple[int, int, int, int] | None:
        """Read an executable's numeric FileVersion, if it declares one."""
        try:
            information = win32api.GetFileVersionInfo(str(path), "\\")
        except (pywintypes.error, OSError):
            return None
        most = int(information["FileVersionMS"])
        least = int(information["FileVersionLS"])
        return (
            (most >> 16) & 0xFFFF,
            most & 0xFFFF,
            (least >> 16) & 0xFFFF,
            least & 0xFFFF,
        )

    @classmethod
    def _startup_copy_is_current(cls, source: Path, target: Path) -> bool:
        source_version = cls._file_version(source)
        target_version = cls._file_version(target)
        if source_version and target_version:
            if target_version > source_version:
                # Never replace a newer copy with an older executable.
                return True
            if target_version < source_version:
                return False

        # The versions match or are unreadable. copy2 preserves the source
        # timestamp, so an equal size and modification time means the copy
        # was taken from this same build.
        source_stat = source.stat()
        target_stat = target.stat()
        return (
            source_stat.st_size == target_stat.st_size
            and abs(source_stat.st_mtime - target_stat.st_mtime) <= 2
        )

    def refresh_installed_copy(self) -> Path | None:
        """Update an existing startup copy so the logon task runs this build.

        The registered task points at a fixed path, so a copy left behind by
        an older build keeps starting that older build at every sign-in until
        the menu item is toggled off and on again. Nothing is installed here:
        an out-of-date copy is refreshed only when one is already present.
        """
        if not getattr(sys, "frozen", False):
            return None
        target = self.installed_executable()
        if not target.exists():
            return None

        source = Path(sys.executable)
        source_name = os.path.normcase(os.path.abspath(source))
        target_name = os.path.normcase(os.path.abspath(target))
        if source_name == target_name:
            return None
        if self._startup_copy_is_current(source, target):
            return None
        return self._install_protected_copy()

    def is_enabled(self) -> bool:
        service = self._scheduler_service()
        root = service.GetFolder("\\")
        try:
            task = root.GetTask(f"\\{self._task_name()}")
        except pywintypes.com_error as error:
            if underlying_hresult(error) in self.TASK_NOT_FOUND_HRESULTS:
                return False
            raise
        return bool(task.Enabled)

    def enable(self) -> Path:
        installed_executable = self._install_protected_copy()
        service = self._scheduler_service()
        root = service.GetFolder("\\")
        definition = service.NewTask(0)
        definition.RegistrationInfo.Description = (
            "Mirrors Windows brightness changes to NVIDIA EC brightness control."
        )

        user_sid = self._current_user_sid_string()
        definition.Principal.UserId = user_sid
        definition.Principal.LogonType = self.TASK_LOGON_INTERACTIVE_TOKEN
        definition.Principal.RunLevel = self.TASK_RUNLEVEL_HIGHEST

        trigger = definition.Triggers.Create(self.TASK_TRIGGER_LOGON)
        trigger.Id = "CurrentUserLogon"
        trigger.UserId = user_sid
        trigger.Enabled = True

        action = definition.Actions.Create(self.TASK_ACTION_EXEC)
        action.Path = str(installed_executable)
        action.WorkingDirectory = str(installed_executable.parent)

        definition.Settings.Enabled = True
        definition.Settings.AllowDemandStart = True
        definition.Settings.StartWhenAvailable = True
        definition.Settings.DisallowStartIfOnBatteries = False
        definition.Settings.StopIfGoingOnBatteries = False
        definition.Settings.ExecutionTimeLimit = "PT0S"
        definition.Settings.MultipleInstances = self.TASK_INSTANCES_IGNORE_NEW

        root.RegisterTaskDefinition(
            self._task_name(),
            definition,
            self.TASK_CREATE_OR_UPDATE,
            user_sid,
            None,
            self.TASK_LOGON_INTERACTIVE_TOKEN,
        )
        return installed_executable

    def disable(self) -> None:
        service = self._scheduler_service()
        root = service.GetFolder("\\")
        try:
            root.DeleteTask(self._task_name(), 0)
        except pywintypes.com_error as error:
            if underlying_hresult(error) not in self.TASK_NOT_FOUND_HRESULTS:
                raise

        remaining_startup_tasks = any(
            str(task.Name).startswith(f"{TASK_NAME_PREFIX}-")
            for task in root.GetTasks(1)  # Include hidden tasks.
        )
        if remaining_startup_tasks:
            return
        try:
            self._remove_protected_copy()
        except Exception:
            logging.exception("The unused protected startup copy was not removed")

    def _install_protected_copy(self) -> Path:
        if not getattr(sys, "frozen", False):
            raise BridgeError(
                "Start with Windows is available only in the built executable"
            )

        users_sid = win32security.CreateWellKnownSid(
            win32security.WinBuiltinUsersSid
        )
        install_directory = _protected_directory(
            program_files_dir() / STARTUP_INSTALL_DIRECTORY,
            users_sid,
        )
        target = install_directory / f"{APP_ID}.exe"
        source = Path(sys.executable)
        source_name = os.path.normcase(os.path.abspath(source))
        target_name = os.path.normcase(os.path.abspath(target))
        if source_name == target_name:
            return target

        if target.exists():
            attributes = win32file.GetFileAttributes(str(target))
            if attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise BridgeError(
                    f"Refusing to replace reparse-point executable: {target}"
                )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{APP_ID}-",
            suffix=".tmp",
            dir=install_directory,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(source, temporary)
            if temporary.stat().st_size != source.stat().st_size:
                raise BridgeError("The protected startup copy is incomplete")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def _remove_protected_copy(self) -> None:
        target = self.installed_executable()
        install_directory = target.parent
        if not install_directory.exists():
            return

        directory_attributes = win32file.GetFileAttributes(
            str(install_directory)
        )
        if directory_attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise BridgeError(
                f"Refusing to remove reparse-point directory: "
                f"{install_directory}"
            )
        if target.exists():
            target_attributes = win32file.GetFileAttributes(str(target))
            if target_attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
                raise BridgeError(
                    f"Refusing to remove reparse-point executable: {target}"
                )

            running_name = os.path.normcase(
                os.path.abspath(Path(sys.executable))
            )
            target_name = os.path.normcase(os.path.abspath(target))
            if running_name == target_name:
                logging.info(
                    "Leaving the protected startup copy in place because it "
                    "is the running executable"
                )
                return
            target.unlink()

        try:
            install_directory.rmdir()
        except OSError:
            # Leave an unexpected non-empty directory intact.
            logging.warning(
                "Protected startup directory is not empty: %s",
                install_directory,
            )


class WmiMethod:
    """Invoke a WMI method using its parameter ID qualifiers."""

    def __init__(self, instance: Any, method_name: str) -> None:
        self._instance = instance
        self._method_name = method_name
        self._definition = instance.Methods_.Item(method_name)
        if self._definition.InParameters is None:
            raise BridgeError(f"{method_name} has no input parameter schema")

        names_by_id: dict[int, str] = {}
        for prop in self._definition.InParameters.Properties_:
            parameter_id = int(prop.Qualifiers_.Item("ID").Value)
            if parameter_id in names_by_id:
                raise BridgeError(
                    f"{method_name} has duplicate input parameter ID "
                    f"{parameter_id}"
                )
            names_by_id[parameter_id] = str(prop.Name)

        expected_ids = list(range(len(names_by_id)))
        if sorted(names_by_id) != expected_ids:
            raise BridgeError(f"{method_name} has an unexpected input schema")
        self._input_names = tuple(
            names_by_id[parameter_id] for parameter_id in expected_ids
        )

    def invoke(
        self,
        *values: int,
        want_result: bool,
        result_property: str = "Result",
    ) -> int | None:
        if len(values) != len(self._input_names):
            raise ValueError(
                f"{self._method_name} expects {len(self._input_names)} inputs"
            )

        input_parameters = self._definition.InParameters.SpawnInstance_()
        for name, value in zip(self._input_names, values):
            input_parameters.Properties_.Item(name).Value = int(value)

        output_parameters = self._instance.ExecMethod_(
            self._method_name,
            input_parameters,
            0,
        )
        if not want_result:
            return None

        if output_parameters is None:
            raise BridgeError(
                f"{self._method_name} declares no output parameters"
            )
        result = output_parameters.Properties_.Item(result_property).Value
        if result is None:
            raise BridgeError(
                f"{self._method_name} returned no {result_property} value"
            )
        return int(result)


class NvidiaEcBrightness:
    """Live WMI connection to the NVIDIA EC brightness provider."""

    def __init__(self) -> None:
        locator = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        self._services = locator.ConnectServer(".", r"root\wmi")
        self._services.Security_.ImpersonationLevel = 3

        self._device = self._find_active_instance(NV_BRIGHTNESS_CLASS)
        self._level_method = WmiMethod(self._device, NV_LEVEL_METHOD)
        self._source_method = WmiMethod(self._device, NV_SOURCE_METHOD)

        source = self._source_method.invoke(
            MODE_GET,
            0,
            want_result=True,
        )
        assert source is not None
        if source != SOURCE_EC:
            source_names = {1: "GPU", 2: "EC", 3: "DisplayPort AUX"}
            source_name = source_names.get(source, "unknown")
            raise BridgeError(
                f"Brightness source is {source_name} ({source}), not EC (2)"
            )

        maximum_level = self._level_method.invoke(
            MODE_GET_MAX,
            0,
            want_result=True,
        )
        assert maximum_level is not None
        self.maximum_level = maximum_level
        if not 1 <= self.maximum_level <= 1000:
            raise BridgeError(
                f"NVIDIA firmware returned invalid maximum level "
                f"{self.maximum_level}"
            )

        self._event_source = self._services.ExecNotificationQuery(
            f"SELECT * FROM {WINDOWS_BRIGHTNESS_EVENT_CLASS}"
        )
        self._windows_brightness_method: WmiMethod | None = None
        try:
            windows_methods = self._find_active_instance(
                WINDOWS_BRIGHTNESS_METHODS_CLASS
            )
            self._windows_brightness_method = WmiMethod(
                windows_methods,
                WINDOWS_SET_BRIGHTNESS_METHOD,
            )
        except Exception as error:
            logging.warning(
                "Windows brightness slider synchronization is unavailable: %s",
                error,
            )

    def _find_active_instance(self, class_name: str) -> Any:
        instances = self._services.ExecQuery(
            f"SELECT * FROM {class_name}",
            "WQL",
            WMI_QUERY_FLAGS,
        )
        for instance in instances:
            if bool(instance.Properties_.Item("Active").Value):
                return instance
        raise BridgeError(f"No active {class_name} instance was found")

    def get_level(self) -> int:
        result = self._level_method.invoke(
            MODE_GET,
            0,
            want_result=True,
        )
        assert result is not None
        return result

    def check_health(self) -> None:
        source = self._source_method.invoke(
            MODE_GET,
            0,
            want_result=True,
        )
        assert source is not None
        if source != SOURCE_EC:
            raise ReconnectRequested(
                f"Brightness source changed from EC (2) to {source}"
            )

    def set_percent(self, percent: int) -> tuple[int, int]:
        requested_percent = max(
            MIN_BRIGHTNESS_PERCENT,
            min(100, int(percent)),
        )
        raw_level = (self.maximum_level * requested_percent + 50) // 100
        raw_level = max(1, min(self.maximum_level, raw_level))

        self._level_method.invoke(
            MODE_SET,
            raw_level,
            want_result=False,
        )
        reported_level = self.get_level()
        reported_percent = round(reported_level * 100 / self.maximum_level)
        return requested_percent, reported_percent

    def get_windows_percent(self) -> int:
        monitor = self._find_active_instance(WINDOWS_BRIGHTNESS_CLASS)
        value = monitor.Properties_.Item("CurrentBrightness").Value
        if value is None:
            raise BridgeError("Windows returned no current brightness value")
        return int(value)

    def set_windows_percent(self, percent: int) -> bool:
        method = self._windows_brightness_method
        if method is None:
            return False

        percent = max(MIN_BRIGHTNESS_PERCENT, min(100, int(percent)))
        try:
            # WmiSetBrightness is declared void: it has no output parameters,
            # so WMI reports failure by raising rather than by a return code.
            method.invoke(0, percent, want_result=False)
        except Exception:
            logging.exception(
                "Windows brightness slider synchronization failed and was "
                "disabled for this connection"
            )
            self._windows_brightness_method = None
            return False
        return True

    def next_brightness_event(self, timeout_ms: int) -> Any | None:
        try:
            return self._event_source.NextEvent(timeout_ms)
        except pywintypes.com_error as error:
            if underlying_hresult(error) == WMI_TIMEOUT_HRESULT:
                return None
            raise

    def close(self) -> None:
        self._event_source = None
        self._windows_brightness_method = None
        self._source_method = None
        self._level_method = None
        self._device = None
        self._services = None

    @staticmethod
    def brightness_from_event(event: Any) -> int | None:
        active = event.Properties_.Item("Active").Value
        if not bool(active):
            return None
        brightness = event.Properties_.Item("Brightness").Value
        if brightness is None:
            return None
        return int(brightness)


class BridgeWorker(threading.Thread):
    def __init__(
        self,
        status_callback: Callable[[], None],
        settings: BrightnessSettings,
    ) -> None:
        super().__init__(name="BrightnessBridge", daemon=True)
        self._status_callback = status_callback
        self._stop_event = threading.Event()
        self._sync_event = threading.Event()
        self._reconnect_event = threading.Event()
        self._adjustment_event = threading.Event()
        self._adjustment_lock = threading.Lock()
        self._pending_adjustment = 0
        self._status_lock = threading.Lock()
        self._status = "Starting"
        self._last_applied_percent: int | None = None
        self._settings = settings
        self._startup_restore_pending = True

    @property
    def status(self) -> str:
        with self._status_lock:
            return self._status

    @property
    def last_applied_percent(self) -> int | None:
        with self._status_lock:
            return self._last_applied_percent

    def _set_status(self, status: str) -> None:
        with self._status_lock:
            if status == self._status:
                return
            self._status = status
        self._status_callback()

    def stop(self) -> None:
        self._stop_event.set()
        self._sync_event.set()
        self._reconnect_event.set()
        self._adjustment_event.set()

    def request_sync(self) -> None:
        self._sync_event.set()

    def request_reconnect(self) -> None:
        logging.info("Display-stack reconnect requested")
        self._reconnect_event.set()

    def request_adjustment(self, steps: int) -> None:
        """Queue a brightness change expressed in whole wheel notches."""
        steps = int(steps)
        if not steps:
            return
        percent_delta = steps * WHEEL_ADJUSTMENT_PERCENT
        with self._adjustment_lock:
            self._pending_adjustment = max(
                -100,
                min(100, self._pending_adjustment + percent_delta),
            )
        self._adjustment_event.set()

    def _take_adjustment(self) -> int:
        with self._adjustment_lock:
            adjustment = self._pending_adjustment
            self._pending_adjustment = 0
            self._adjustment_event.clear()
        return adjustment

    def run(self) -> None:
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            while not self._stop_event.is_set():
                try:
                    self._run_connected()
                except ReconnectRequested:
                    self._set_status("Reconnecting")
                    continue
                except Exception as error:
                    logging.exception("Brightness bridge connection failed")
                    self._set_status(f"Waiting: {self._short_error(error)}")

                if self._stop_event.wait(3):
                    break
        finally:
            self._set_status("Stopped")
            gc.collect()
            pythoncom.CoUninitialize()

    @staticmethod
    def _short_error(error: Exception) -> str:
        message = str(error).replace("\r", " ").replace("\n", " ").strip()
        return (message or error.__class__.__name__)[:80]

    def _run_connected(self) -> None:
        self._set_status("Connecting")
        bridge = NvidiaEcBrightness()
        try:
            logging.info(
                "Connected to NVIDIA EC brightness provider (maximum=%d)",
                bridge.maximum_level,
            )
            self._reconnect_event.clear()
            self._sync_event.clear()

            saved_brightness = self._settings.brightness
            if (
                self._startup_restore_pending
                and self._settings.enabled
                and saved_brightness is not None
            ):
                restored = self._apply(
                    bridge,
                    saved_brightness,
                    "saved startup",
                    force=True,
                )
                if (
                    restored is not None
                    and bridge.set_windows_percent(restored)
                ):
                    logging.info(
                        "Windows brightness slider synchronized to %d%%",
                        restored,
                    )
                self._startup_restore_pending = False
            else:
                self._startup_restore_pending = False
                self._synchronize(bridge, "startup")
            next_health_check = (
                time.monotonic() + HEALTH_CHECK_INTERVAL_SECONDS
            )

            while not self._stop_event.is_set():
                if self._reconnect_event.is_set():
                    self._reconnect_event.clear()
                    raise ReconnectRequested

                if self._sync_event.is_set():
                    self._sync_event.clear()
                    self._synchronize(bridge, "manual")

                if self._adjustment_event.is_set():
                    adjustment = self._take_adjustment()
                    if adjustment:
                        current = self.last_applied_percent
                        if current is None:
                            current = round(
                                bridge.get_level()
                                * 100
                                / bridge.maximum_level
                            )
                        applied = self._apply(
                            bridge,
                            current + adjustment,
                            "tray wheel",
                        )
                        if (
                            applied is not None
                            and bridge.set_windows_percent(applied)
                        ):
                            logging.info(
                                "Windows brightness slider synchronized to %d%%",
                                applied,
                            )

                event = bridge.next_brightness_event(
                    WMI_EVENT_POLL_MILLISECONDS
                )
                if event is not None:
                    requested = bridge.brightness_from_event(event)
                    if requested is not None:
                        # Collapse a short burst of slider events without making
                        # dragging feel delayed.
                        deadline = time.monotonic() + 0.075
                        while time.monotonic() < deadline:
                            remaining_ms = max(
                                1,
                                int((deadline - time.monotonic()) * 1000),
                            )
                            next_event = bridge.next_brightness_event(
                                remaining_ms
                            )
                            if next_event is None:
                                break
                            next_requested = bridge.brightness_from_event(
                                next_event
                            )
                            if next_requested is not None:
                                requested = next_requested

                        self._apply(bridge, requested, "event")

                now = time.monotonic()
                if now >= next_health_check:
                    bridge.check_health()
                    next_health_check = now + HEALTH_CHECK_INTERVAL_SECONDS
        finally:
            bridge.close()

    def _synchronize(self, bridge: NvidiaEcBrightness, reason: str) -> None:
        requested = bridge.get_windows_percent()
        self._apply(bridge, requested, reason, force=True)

    def _apply(
        self,
        bridge: NvidiaEcBrightness,
        requested: int,
        reason: str,
        force: bool = False,
    ) -> int | None:
        requested = max(MIN_BRIGHTNESS_PERCENT, min(100, int(requested)))
        if not force and requested == self._last_applied_percent:
            return None

        applied, reported = bridge.set_percent(requested)
        with self._status_lock:
            self._last_applied_percent = applied
        try:
            self._settings.record_brightness(applied)
        except OSError:
            logging.exception("Could not save the applied brightness")
        self._set_status(f"Connected: {reported}%")
        logging.info(
            "Brightness %s: requested %d%%; EC reports %d%%",
            reason,
            applied,
            reported,
        )
        return applied


class TrayApplication:
    def __init__(self, settings: BrightnessSettings) -> None:
        self._hwnd: int | None = None
        self._icon_handle: int | None = None
        self._owns_icon_handle = False
        self._icon_added = False
        self._exiting = False
        self._settings = settings
        self._worker = BridgeWorker(self._post_status_changed, settings)
        self._wheel_listener = TrayWheelListener(
            self._worker.request_adjustment
        )
        self._cached_icon_rect: tuple[int, int, int, int] | None = None
        self._icon_rect_refreshed_at = 0.0
        self._startup_manager = StartupTaskManager()
        self._startup_enabled = False

    def run(self) -> None:
        pythoncom.CoInitialize()
        try:
            self._create_window()
            try:
                self._startup_enabled = self._startup_manager.is_enabled()
            except Exception:
                logging.exception("Could not read the startup-task state")
            try:
                refreshed = self._startup_manager.refresh_installed_copy()
                if refreshed is not None:
                    logging.info(
                        "Protected startup copy updated to %s: %s",
                        APP_VERSION,
                        refreshed,
                    )
            except Exception:
                logging.exception("Could not update the protected startup copy")
            self._add_icon()
            self._wheel_listener.start()
            if not self._wheel_listener.wait_until_ready(2):
                raise BridgeError("The tray wheel listener did not start")
            self._worker.start()
            win32gui.PumpMessages()
        finally:
            self._wheel_listener.stop()
            if self._wheel_listener.is_alive():
                self._wheel_listener.join(timeout=2)
            self._worker.stop()
            if self._worker.is_alive():
                self._worker.join(timeout=4)
            pythoncom.CoUninitialize()

    def _create_window(self) -> None:
        instance = win32api.GetModuleHandle(None)
        class_name = f"{APP_ID}.TrayWindow"
        message_map = {
            WM_TRAYICON: self._on_tray_message,
            WM_STATUS_CHANGED: self._on_status_changed,
            TASKBAR_CREATED: self._on_taskbar_created,
            win32con.WM_DISPLAYCHANGE: self._on_display_change,
            win32con.WM_POWERBROADCAST: self._on_power_broadcast,
            win32con.WM_DESTROY: self._on_destroy,
        }

        window_class = win32gui.WNDCLASS()
        window_class.hInstance = instance
        window_class.lpszClassName = class_name
        window_class.lpfnWndProc = message_map
        win32gui.RegisterClass(window_class)

        self._hwnd = win32gui.CreateWindow(
            class_name,
            APP_NAME,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            instance,
            None,
        )
        allow_lower_integrity_window_message(self._hwnd, WM_TRAYICON)
        # Explorer runs at a lower integrity level than this elevated process,
        # so its restart broadcast needs the same explicit exemption.
        allow_lower_integrity_window_message(self._hwnd, TASKBAR_CREATED)
        logging.info("Tray callback allowed through the window UIPI filter")

    def _add_icon(self) -> None:
        if self._hwnd is None:
            return
        self._release_icon_handle()
        self._icon_handle = self._load_tray_icon()
        win32gui.Shell_NotifyIcon(
            win32gui.NIM_ADD,
            self._notify_data(),
        )
        self._icon_added = True

    def _load_tray_icon(self) -> int:
        if getattr(sys, "frozen", False):
            try:
                large_icons, small_icons = win32gui.ExtractIconEx(
                    sys.executable, 0, 1
                )
                icons = [*small_icons, *large_icons]
                if icons:
                    selected_icon = icons[0]
                    for icon in icons[1:]:
                        win32gui.DestroyIcon(icon)
                    self._owns_icon_handle = True
                    return selected_icon
            except win32gui.error:
                logging.exception("Could not load the embedded application icon")

        self._owns_icon_handle = False
        return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

    def _release_icon_handle(self) -> None:
        if self._owns_icon_handle and self._icon_handle:
            win32gui.DestroyIcon(self._icon_handle)
        self._icon_handle = None
        self._owns_icon_handle = False

    def _notify_data(self) -> tuple[Any, ...]:
        assert self._hwnd is not None
        tooltip = f"{APP_NAME} - {self._worker.status}"[:127]
        return (
            self._hwnd,
            TRAY_ICON_ID,
            win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
            WM_TRAYICON,
            self._icon_handle,
            tooltip,
        )

    def _post_status_changed(self) -> None:
        hwnd = self._hwnd
        if hwnd and win32gui.IsWindow(hwnd):
            win32gui.PostMessage(hwnd, WM_STATUS_CHANGED, 0, 0)

    def _on_status_changed(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if self._icon_added:
            win32gui.Shell_NotifyIcon(win32gui.NIM_MODIFY, self._notify_data())
        return 0

    def _on_tray_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        event_message = lparam & 0xFFFF
        if event_message == win32con.WM_MOUSEMOVE:
            try:
                self._arm_wheel_control(hwnd)
            except OSError:
                logging.exception("Could not locate the tray icon for scrolling")
            return 1

        try:
            if event_message in (
                win32con.WM_RBUTTONUP,
                win32con.WM_CONTEXTMENU,
            ):
                self._show_menu()
            elif event_message == win32con.WM_LBUTTONDBLCLK:
                self._worker.request_sync()
        except Exception as error:
            logging.exception(
                "Tray callback failed (event=0x%04X)",
                event_message,
            )
            show_message(
                "The tray command could not be opened.\n\n"
                f"{error}\n\n"
                "See the log for more information.",
                error=True,
            )
        return 1

    def _arm_wheel_control(self, hwnd: int) -> None:
        now = time.monotonic()
        if (
            self._cached_icon_rect is None
            or now - self._icon_rect_refreshed_at >= 1.0
        ):
            self._cached_icon_rect = tray_icon_rect(hwnd)
            self._icon_rect_refreshed_at = now
        self._wheel_listener.arm(self._cached_icon_rect)

    def _show_menu(self) -> None:
        if self._hwnd is None:
            return
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                MENU_VERSION,
                f"Version {APP_VERSION}",
            )
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING | win32con.MF_GRAYED,
                MENU_STATUS,
                f"Status: {self._worker.status}",
            )
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                MENU_SYNC,
                "Synchronize now",
            )
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                MENU_RECONNECT,
                "Reconnect display interface",
            )
            win32gui.AppendMenu(
                menu,
                win32con.MF_STRING,
                MENU_OPEN_LOG,
                "Open log",
            )
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            try:
                self._startup_enabled = self._startup_manager.is_enabled()
            except Exception:
                logging.exception("Could not refresh the startup-task state")
            startup_flags = win32con.MF_STRING
            if self._startup_enabled:
                startup_flags |= win32con.MF_CHECKED
            if not getattr(sys, "frozen", False):
                startup_flags |= win32con.MF_GRAYED
            win32gui.AppendMenu(
                menu,
                startup_flags,
                MENU_STARTUP,
                "Start with Windows",
            )
            save_brightness_flags = win32con.MF_STRING
            if self._settings.enabled:
                save_brightness_flags |= win32con.MF_CHECKED
            win32gui.AppendMenu(
                menu,
                save_brightness_flags,
                MENU_SAVE_BRIGHTNESS,
                "Save brightness for startup",
            )
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT, "Exit")

            cursor_x, cursor_y = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self._hwnd)
            command = win32gui.TrackPopupMenu(
                menu,
                win32con.TPM_LEFTALIGN
                | win32con.TPM_RIGHTBUTTON
                | win32con.TPM_NONOTIFY
                | win32con.TPM_RETURNCMD,
                cursor_x,
                cursor_y,
                0,
                self._hwnd,
                None,
            )
            win32gui.PostMessage(self._hwnd, win32con.WM_NULL, 0, 0)
            try:
                win32gui.Shell_NotifyIcon(
                    NIM_SETFOCUS,
                    (self._hwnd, 0),
                )
            except win32gui.error:
                logging.exception("Could not return focus to the notification area")
            if command:
                self._execute_command(command)
        finally:
            win32gui.DestroyMenu(menu)

    def _execute_command(self, command: int) -> None:
        if command == MENU_VERSION:
            self._open_releases_page()
        elif command == MENU_SYNC:
            self._worker.request_sync()
        elif command == MENU_RECONNECT:
            self._worker.request_reconnect()
        elif command == MENU_OPEN_LOG:
            self._open_log()
        elif command == MENU_STARTUP:
            self._toggle_startup()
        elif command == MENU_SAVE_BRIGHTNESS:
            self._toggle_saved_brightness()
        elif command == MENU_EXIT:
            self._begin_exit()

    def _open_releases_page(self) -> None:
        try:
            open_as_user(RELEASES_URL)
        except Exception as error:
            logging.exception("Could not open the releases page")
            show_message(
                "The releases page could not be opened.\n\n"
                f"{error}\n\n"
                f"{RELEASES_URL}",
                error=True,
            )

    def _open_log(self) -> None:
        if LOG_PATH is None or not LOG_PATH.exists():
            show_message("The log file is not available yet.")
            return
        try:
            open_as_user(LOG_PATH)
        except Exception as error:
            logging.exception("Could not open the log")
            show_message(
                "The log could not be opened.\n\n"
                f"{error}\n\n"
                f"{LOG_PATH}",
                error=True,
            )

    def _toggle_startup(self) -> None:
        try:
            if self._startup_enabled:
                self._startup_manager.disable()
                self._startup_enabled = False
                logging.info("Start with Windows disabled")
            else:
                installed_executable = self._startup_manager.enable()
                self._startup_enabled = True
                logging.info(
                    "Start with Windows enabled: %s",
                    installed_executable,
                )
        except Exception as error:
            logging.exception("Could not change the startup-task state")
            show_message(
                "Start with Windows could not be changed.\n\n"
                f"{error}",
                error=True,
            )

    def _toggle_saved_brightness(self) -> None:
        try:
            if self._settings.enabled:
                self._settings.set_enabled(False)
                logging.info("Save brightness for startup disabled")
                return

            brightness = self._worker.last_applied_percent
            self._settings.set_enabled(True, brightness)
            if brightness is None:
                self._worker.request_sync()
            logging.info(
                "Save brightness for startup enabled (value=%s)",
                brightness,
            )
        except Exception as error:
            logging.exception("Could not change brightness startup settings")
            show_message(
                "The brightness startup setting could not be changed.\n\n"
                f"{error}",
                error=True,
            )

    def _on_taskbar_created(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if self._exiting:
            return 0
        self._icon_added = False
        self._cached_icon_rect = None
        self._icon_rect_refreshed_at = 0.0
        try:
            self._add_icon()
        except Exception:
            logging.exception("Could not restore the tray icon")
            return 0
        logging.info("Tray icon restored after an Explorer restart")
        return 0

    def _on_display_change(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        self._worker.request_reconnect()
        return 0

    def _on_power_broadcast(
        self,
        hwnd: int,
        msg: int,
        wparam: int,
        lparam: int,
    ) -> int:
        resume_events = {
            0x0007,  # PBT_APMRESUMESUSPEND
            0x0012,  # PBT_APMRESUMEAUTOMATIC
        }
        if wparam in resume_events:
            self._worker.request_reconnect()
        return 1

    def _begin_exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        self._worker.stop()
        self._worker.join(timeout=4)
        if self._hwnd and win32gui.IsWindow(self._hwnd):
            win32gui.DestroyWindow(self._hwnd)

    def _on_destroy(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if self._icon_added:
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, self._notify_data())
            except win32gui.error:
                pass
            self._icon_added = False
        self._release_icon_handle()
        self._worker.stop()
        win32gui.PostQuitMessage(0)
        return 0


def main() -> int:
    if not is_administrator():
        if relaunch_elevated():
            return 0
        show_message(
            "Administrator permission is required to access the NVIDIA EC "
            "brightness interface.",
            error=True,
        )
        return 1

    # Claim the single-instance mutex before the protected data directory is
    # created, so a duplicate launch cannot rewrite its DACL or append a
    # misleading start entry to a log it does not go on to own.
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        show_message(f"{APP_NAME} is already running.")
        win32api.CloseHandle(mutex)
        return 0

    try:
        try:
            configure_logging()
        except Exception as error:
            show_message(
                "The protected log directory could not be initialized.\n\n"
                f"{error}",
                error=True,
            )
            return 1

        logging.info("%s %s starting", APP_NAME, APP_VERSION)
        try:
            assert SETTINGS_PATH is not None
            TrayApplication(BrightnessSettings(SETTINGS_PATH)).run()
            return 0
        except Exception:
            logging.exception("Fatal application error")
            log_location = (
                str(LOG_PATH) if LOG_PATH is not None else "Unavailable"
            )
            show_message(
                f"{APP_NAME} stopped because of an unexpected error.\n\n"
                f"See the log for details:\n{log_location}",
                error=True,
            )
            return 1
        finally:
            logging.info("%s stopped", APP_NAME)
    finally:
        win32api.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
