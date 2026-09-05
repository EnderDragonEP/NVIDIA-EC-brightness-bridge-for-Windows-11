import unittest
from pathlib import Path
import shutil
import tempfile

import nvidia_ec_brightness_bridge as bridge
import win32con
import win32gui


class _Collection:
    def __init__(self, items):
        self._items = items

    def __iter__(self):
        return iter(self._items.values())

    def Item(self, name):
        return self._items[name]


class _Value:
    def __init__(self, value=None):
        self.Value = value


class _Property(_Value):
    def __init__(self, name, parameter_id):
        super().__init__()
        self.Name = name
        self.Qualifiers_ = _Collection({"ID": _Value(parameter_id)})


class _ParameterInstance:
    def __init__(self, names):
        self.Properties_ = _Collection(
            {name: _Value() for name in names}
        )


class _ParameterSchema:
    def __init__(self, properties):
        self.Properties_ = _Collection(
            {prop.Name: prop for prop in properties}
        )

    def SpawnInstance_(self):
        return _ParameterInstance(self.Properties_._items)


class _MethodDefinition:
    def __init__(self):
        # Deliberately enumerate ID 1 before ID 0.
        self.InParameters = _ParameterSchema(
            [_Property("Level", 1), _Property("inArg", 0)]
        )


class _WmiInstance:
    def __init__(self, outputs=True):
        self.Methods_ = _Collection({"Method": _MethodDefinition()})
        self.received = None
        self._outputs = outputs

    def ExecMethod_(self, name, parameters, flags):
        self.received = {
            key: value.Value
            for key, value in parameters.Properties_._items.items()
        }
        if not self._outputs:
            # A void method such as WmiSetBrightness has no out parameters,
            # so WMI hands back nothing at all.
            return None
        return type(
            "Output",
            (),
            {
                "Properties_": _Collection(
                    {
                        "Result": _Value(42),
                        "ReturnValue": _Value(0),
                    }
                )
            },
        )()


class CoreTests(unittest.TestCase):
    def test_automation_hresult_uses_nested_wmi_code(self):
        error = type(
            "Error",
            (),
            {
                "hresult": -2147352567,
                "excepinfo": (None, None, None, None, 0, -2147209215),
            },
        )()
        self.assertEqual(bridge.underlying_hresult(error), 0x80043001)

    def test_wmi_inputs_follow_id_qualifiers(self):
        instance = _WmiInstance()
        method = bridge.WmiMethod(instance, "Method")
        result = method.invoke(1, 80, want_result=True)
        self.assertEqual(result, 42)
        self.assertEqual(instance.received, {"inArg": 1, "Level": 80})

    def test_wmi_method_can_read_windows_return_value(self):
        instance = _WmiInstance()
        method = bridge.WmiMethod(instance, "Method")
        result = method.invoke(
            0,
            75,
            want_result=True,
            result_property="ReturnValue",
        )
        self.assertEqual(result, 0)
        self.assertEqual(instance.received, {"inArg": 0, "Level": 75})

    def test_windows_slider_update_uses_immediate_timeout(self):
        instance = _WmiInstance(outputs=False)
        device = object.__new__(bridge.NvidiaEcBrightness)
        device._windows_brightness_method = bridge.WmiMethod(
            instance,
            "Method",
        )

        self.assertTrue(device.set_windows_percent(75))
        self.assertEqual(instance.received, {"inArg": 0, "Level": 75})

    def test_void_method_result_request_reports_a_clear_error(self):
        instance = _WmiInstance(outputs=False)
        method = bridge.WmiMethod(instance, "Method")
        with self.assertRaises(bridge.BridgeError) as caught:
            method.invoke(0, 75, want_result=True)
        self.assertIn("no output parameters", str(caught.exception))

    def test_native_menu_separator_uses_a_string(self):
        menu = win32gui.CreatePopupMenu()
        try:
            win32gui.AppendMenu(
                menu,
                win32con.MF_SEPARATOR,
                0,
                "",
            )
        finally:
            win32gui.DestroyMenu(menu)

    def test_brightness_settings_save_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            settings = bridge.BrightnessSettings(path)
            settings.set_enabled(True, 62)
            settings.record_brightness(44)

            reloaded = bridge.BrightnessSettings(path)
            self.assertTrue(reloaded.enabled)
            self.assertEqual(reloaded.brightness, 44)
            self.assertIn("brightness = 44", path.read_text(encoding="utf-8"))

    def test_brightness_settings_do_not_record_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.ini"
            settings = bridge.BrightnessSettings(path)
            settings.set_enabled(False)
            settings.record_brightness(80)

            reloaded = bridge.BrightnessSettings(path)
            self.assertFalse(reloaded.enabled)
            self.assertIsNone(reloaded.brightness)

    def test_wheel_delta_accumulates_partial_notches(self):
        steps, remainder = bridge.accumulate_wheel_delta(0, 60)
        self.assertEqual((steps, remainder), (0, 60))
        steps, remainder = bridge.accumulate_wheel_delta(remainder, 60)
        self.assertEqual((steps, remainder), (1, 0))

    def test_wheel_delta_preserves_negative_remainder(self):
        steps, remainder = bridge.accumulate_wheel_delta(0, -180)
        self.assertEqual((steps, remainder), (-1, -60))
        steps, remainder = bridge.accumulate_wheel_delta(remainder, 60)
        self.assertEqual((steps, remainder), (0, 0))

    def test_startup_copy_is_stale_when_the_size_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.exe"
            target = Path(directory) / "target.exe"
            source.write_bytes(b"newer build")
            target.write_bytes(b"old")
            self.assertFalse(
                bridge.StartupTaskManager._startup_copy_is_current(
                    source,
                    target,
                )
            )

    def test_startup_copy_is_current_after_a_metadata_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.exe"
            target = Path(directory) / "target.exe"
            source.write_bytes(b"same build")
            shutil.copy2(source, target)
            self.assertTrue(
                bridge.StartupTaskManager._startup_copy_is_current(
                    source,
                    target,
                )
            )

    def test_taskbar_created_message_is_registered(self):
        # A typo in the message name would silently register a private message
        # that Explorer never broadcasts, so the icon would stay lost.
        self.assertEqual(
            bridge.TASKBAR_CREATED,
            win32gui.RegisterWindowMessage("TaskbarCreated"),
        )
        self.assertGreaterEqual(bridge.TASKBAR_CREATED, 0xC000)

    def _worker(self, directory):
        settings = bridge.BrightnessSettings(Path(directory) / "settings.ini")
        return bridge.BridgeWorker(lambda: None, settings)

    def test_wheel_notches_scale_by_the_adjustment_step(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self._worker(directory)
            worker.request_adjustment(2)
            worker.request_adjustment(-1)
            self.assertEqual(
                worker._take_adjustment(),
                bridge.WHEEL_ADJUSTMENT_PERCENT,
            )

    def test_pending_adjustment_stays_within_the_percent_range(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = self._worker(directory)
            for _ in range(50):
                worker.request_adjustment(1)
            self.assertEqual(worker._take_adjustment(), 100)


if __name__ == "__main__":
    unittest.main()
