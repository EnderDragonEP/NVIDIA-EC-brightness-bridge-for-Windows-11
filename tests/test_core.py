import unittest

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
    def __init__(self):
        self.Methods_ = _Collection({"Method": _MethodDefinition()})
        self.received = None

    def ExecMethod_(self, name, parameters, flags):
        self.received = {
            key: value.Value
            for key, value in parameters.Properties_._items.items()
        }
        return type(
            "Output",
            (),
            {"Properties_": _Collection({"Result": _Value(42)})},
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


if __name__ == "__main__":
    unittest.main()
