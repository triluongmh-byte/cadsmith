import pytest

from cadsmith.electronics import KINDS, Wiring, WiringError, check_wiring, make_device
from cadsmith.loader import ModelError, load_model

ROBOT = {"mcu": "esp32_devkit", "s1": "SG90", "s2": "SG90", "bat": "pack_2s_18650",
         "buck": {"kind": "lm2596", "vout": 5.0}}
ROBOT_WIRES = [("bat.+", "buck.IN+"), ("bat.-", "buck.IN-"), ("buck.OUT+", "s1.V+", "s2.V+", "mcu.VIN"),
               ("buck.OUT-", "s1.GND", "s2.GND", "mcu.GND"), ("mcu.GPIO13", "s1.signal"), ("mcu.GPIO14", "s2.signal")]


def check(devices, wires):
    return check_wiring(Wiring.from_spec(devices, wires))


def test_every_kind_builds_and_resolves_its_own_pins():
    for kind in KINDS:
        device = make_device(kind)
        assert all(device.resolve(pin) == pin for pin in device.pins)
        assert all(source in device.pins and out in device.pins for out, source in device.feeds.items())


def test_a_sound_robot_passes_with_a_pin_map():
    report = check(ROBOT, ROBOT_WIRES)
    assert report.errors == []
    assert {pin["pin"]: pin["use"] for pin in report.pins} == {"mcu.GPIO13": "pwm", "mcu.GPIO14": "pwm"}
    buck = next(entry for entry in report.power if entry["supply"] == "buck.OUT+")
    assert buck["draw_ma"] == 2 * 650 + 250


def test_buck_input_current_follows_the_voltage_drop():
    report = check(ROBOT, ROBOT_WIRES)
    battery = next(entry for entry in report.power if entry["supply"] == "bat.+")
    assert battery["draw_ma"] == pytest.approx(1550 * 5.0 / (7.4 * 0.85) + 5, abs=1)


def test_aliases_and_case_are_accepted():
    wiring = Wiring.from_spec({"mcu": "esp32_devkit", "nano": "arduino_nano"}, [("mcu.d13", "nano.13")])
    assert wiring.wires == [("mcu.GPIO13", "nano.D13")]


def test_unknown_pin_names_the_real_ones():
    with pytest.raises(WiringError, match="GPIO13"):
        Wiring.from_spec({"mcu": "esp32_devkit"}, [("mcu.GPIO99", "mcu.GPIO13")])


def test_unknown_kind():
    with pytest.raises(WiringError, match="unknown device kind"):
        Wiring.from_spec({"x": "esp8266_magic"}, [])


def test_servo_on_a_pin_without_pwm_gets_suggestions():
    report = check({"nano": "arduino_nano", "s": "SG90", "pack": "aa_4"},
                   [("pack.+", "s.V+"), ("pack.-", "s.GND", "nano.GND"), ("nano.D2", "s.signal")])
    assert any("nano.D2 cannot" in error and "D3" in error for error in report.errors)


def test_overvoltage_burns_the_part():
    report = check({"mcu": "arduino_nano", "s": "MG996R", "bat": "pack_2s_18650"},
                   [("bat.+", "s.V+"), ("bat.-", "s.GND", "mcu.GND"), ("mcu.D9", "s.signal")])
    assert any("s.V+ takes at most 7.2 V" in error for error in report.errors)


def test_fresh_aa_cells_are_a_warning_not_an_error():
    report = check({"mcu": "arduino_nano", "s": "SG90", "pack": "aa_4"},
                   [("pack.+", "s.V+"), ("pack.-", "s.GND", "mcu.GND"), ("mcu.D9", "s.signal")])
    assert report.errors == []
    assert any("6.4 V when full" in warning for warning in report.warnings)


def test_five_volt_echo_into_an_esp32_needs_a_divider():
    report = check({"mcu": "esp32_devkit", "sonar": "hc_sr04"},
                   [("mcu.VIN", "sonar.VCC"), ("mcu.GND", "sonar.GND"), ("mcu.GPIO25", "sonar.TRIG"),
                    ("mcu.GPIO26", "sonar.ECHO")])
    assert any("voltage divider" in error for error in report.errors)


def test_the_same_echo_is_fine_on_a_five_volt_board():
    report = check({"mcu": "arduino_nano", "sonar": "hc_sr04"},
                   [("mcu.5V", "sonar.VCC"), ("mcu.GND", "sonar.GND"), ("mcu.D7", "sonar.TRIG"),
                    ("mcu.D8", "sonar.ECHO")])
    assert report.errors == []


def test_input_only_pin_cannot_drive_a_servo():
    report = check(ROBOT, ROBOT_WIRES[:-1] + [("mcu.GPIO34", "s2.signal")])
    assert any("mcu.GPIO34 cannot" in error for error in report.errors)


def test_servos_on_usb_power_overload_it():
    report = check({"mcu": "esp32_devkit", "s1": "SG90", "s2": "SG90"},
                   [("mcu.VIN", "s1.V+", "s2.V+"), ("mcu.GND", "s1.GND", "s2.GND"),
                    ("mcu.GPIO13", "s1.signal"), ("mcu.GPIO14", "s2.signal")])
    assert any("on USB" in warning and "mA" in warning for warning in report.warnings)


def test_short_circuit():
    report = check({"bat": "cell_18650", "s": "SG90"}, [("bat.+", "bat.-")])
    assert any("short circuit" in error for error in report.errors)


def test_mixed_voltages_on_one_net():
    report = check({"a": "cell_18650", "b": "usb_power_bank"}, [("a.+", "b.+"), ("a.-", "b.-")])
    assert any("different voltages" in error for error in report.errors)


def test_missing_ground_and_split_grounds():
    report = check({"mcu": "esp32_devkit", "s": "SG90", "bat": "aa_4"},
                   [("bat.+", "s.V+"), ("bat.-", "s.GND"), ("mcu.GPIO13", "s.signal")])
    assert any("mcu has no ground wire" in error for error in report.errors)


def test_i2c_address_clash():
    report = check({"mcu": "esp32_devkit", "a": "ssd1306", "b": "ssd1306"},
                   [("mcu.3V3", "a.VCC", "b.VCC"), ("mcu.GND", "a.GND", "b.GND"),
                    ("mcu.GPIO21", "a.SDA", "b.SDA"), ("mcu.GPIO22", "a.SCL", "b.SCL")])
    assert any("0x3C" in error for error in report.errors)


def test_unpowered_regulator():
    report = check({"reg": "ams1117_3v3", "oled": "ssd1306"},
                   [("reg.VOUT", "oled.VCC"), ("reg.GND", "oled.GND")])
    assert any("unpowered" in error for error in report.errors)


def test_strapping_pin_is_a_warning():
    report = check(ROBOT, ROBOT_WIRES[:-1] + [("mcu.GPIO12", "s2.signal")])
    assert report.errors == []
    assert any("mcu.GPIO12 is a boot strapping pin" in warning for warning in report.warnings)


def test_model_file_declares_wiring(tmp_path):
    path = tmp_path / "bot.py"
    path.write_text('import cadquery as cq\n'
                    'def parts():\n'
                    '    return {"frame": cq.Workplane().box(40, 40, 5), "s1": cq.Workplane().box(23, 12, 23)}\n'
                    'BOUGHT = {"s1"}\n'
                    f'DEVICES = {{"mcu": "esp32_devkit", "s1": "SG90", "bat": "aa_4"}}\n'
                    'WIRES = [("bat.+", "s1.V+"), ("bat.-", "s1.GND", "mcu.GND"), ("mcu.GPIO13", "s1.signal")]\n')
    model = load_model(path)
    assert check_wiring(model.wiring).errors == []


def test_model_file_with_a_bad_pin_fails_to_load(tmp_path):
    path = tmp_path / "bot.py"
    path.write_text('import cadquery as cq\nresult = cq.Workplane().box(10, 10, 10)\n'
                    'DEVICES = {"mcu": "esp32_devkit"}\nWIRES = [("mcu.GPIO99", "mcu.GND")]\n')
    with pytest.raises(ModelError, match="WIRES"):
        load_model(path)


def test_firmware_pins_follow_the_wiring():
    from cadsmith.electronics.firmware import firmware_files
    devices = dict(ROBOT, oled="ssd1306", sonar="hc_sr04")
    wires = ROBOT_WIRES + [("mcu.3V3", "oled.VCC"), ("mcu.GND", "oled.GND", "sonar.GND"), ("mcu.21", "oled.SDA"),
                           ("mcu.22", "oled.SCL"), ("buck.OUT+", "sonar.VCC"), ("mcu.GPIO25", "sonar.TRIG"),
                           ("mcu.GPIO27", "sonar.ECHO")]
    wiring = Wiring.from_spec(devices, wires)
    files = firmware_files(wiring, check_wiring(wiring), "dog bot")
    defines = {line.split()[1]: line.split()[2] for line in files["include/pins.h"].splitlines()
               if line.startswith("#define")}
    assert defines == {"PIN_S1": "13", "PIN_S2": "14", "PIN_SONAR_TRIG": "25", "PIN_SONAR_ECHO": "27",
                       "PIN_I2C_SDA": "21", "PIN_I2C_SCL": "22"}
    assert "board = esp32dev" in files["platformio.ini"] and "ESP32Servo" in files["platformio.ini"]
    main = files["src/main.cpp"]
    assert "s1.attach(PIN_S1)" in main and "Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL)" in main


def test_firmware_uses_the_projects_own_sketch_and_avr_servo():
    from cadsmith.electronics.firmware import firmware_files
    wiring = Wiring.from_spec({"nano": "arduino_nano", "s": "SG90", "pack": "aa_4"},
                              [("pack.+", "s.V+"), ("pack.-", "s.GND", "nano.GND"), ("nano.D9", "s.signal")])
    report = check_wiring(wiring)
    starter = firmware_files(wiring, report, "x")
    assert "#define PIN_S 9 " in starter["include/pins.h"] and "#include <Servo.h>" in starter["src/main.cpp"]
    assert "arduino-libraries/Servo" in starter["platformio.ini"]
    assert firmware_files(wiring, report, "x", sketch="// mine\n")["src/main.cpp"] == "// mine\n"


def test_no_board_means_no_firmware():
    from cadsmith.electronics.firmware import firmware_files
    wiring = Wiring.from_spec({"bat": "aa_4", "s": "SG90"}, [("bat.+", "s.V+"), ("bat.-", "s.GND")])
    assert firmware_files(wiring, check_wiring(wiring), "x") == {}
