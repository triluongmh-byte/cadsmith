"""Electrical pin tables for boards and modules, so wiring can be checked before anything is soldered.

Each kind is a function returning a :class:`DeviceType`; add a board by adding one. Values are
nominal, from common datasheets and the usual clone modules: currents are worst case (a servo's
stall current), so a budget that passes here has headroom.

Pin roles:

- ``gnd``: ground.
- ``supply``: gives power at ``volts`` (swinging between ``vmin`` and ``vmax`` for a battery), up
  to ``ma`` (0: no stated limit).
- ``load``: takes power; accepts ``vmin`` to ``vmax`` and draws up to ``ma``.
- ``vbus``: a board's own power pin. It is a load when something else powers its net, a supply
  from the board's regulator when the pin it ``feeds`` from is powered, and otherwise the USB
  supply, at ``volts`` and ``usb_ma``.
- ``gpio``: a microcontroller pin; ``caps`` says what it can do (``out``, ``pwm``, ``adc``,
  ``sda``, ``scl``) and ``notes`` its cautions (``strap``, ``serial``, ``adc2``).
- ``in``: a module input that needs a pin on its net able to do every one of ``needs``.
- ``out``: a module output at ``volts`` (or the voltage of the net on pin ``volts_from``), giving
  ``caps``; the microcontroller pin it reaches needs ``needs``.
- ``passive``: a terminal with no direction, such as a button or switch contact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Pin:
    role: str
    volts: float | None = None
    vmin: float | None = None
    vmax: float | None = None
    ma: float = 0.0
    caps: frozenset[str] = frozenset()
    needs: frozenset[str] = frozenset()
    notes: frozenset[str] = frozenset()
    volts_from: str | None = None
    usb_ma: float = 500.0


@dataclass(frozen=True)
class DeviceType:
    kind: str
    title: str
    pins: dict[str, Pin]
    #: Logic level of a microcontroller's pins, and whether they take 5 V.
    logic: float | None = None
    five_volt_tolerant: bool = False
    #: A supply or vbus pin -> the pin that powers it inside the device.
    feeds: dict[str, str] = field(default_factory=dict)
    #: Of a switching converter; None for a linear regulator, whose input current equals its output.
    efficiency: float | None = None
    i2c_address: int | None = None
    #: Groups of pins joined inside the device, such as a switch that is on.
    internal: tuple[tuple[str, ...], ...] = ()
    aliases: dict[str, str] = field(default_factory=dict)

    @property
    def is_mcu(self) -> bool:
        return any(pin.role == "gpio" for pin in self.pins.values())

    def resolve(self, name: str) -> str | None:
        """The pin called ``name``, ignoring case and allowing the device's aliases."""
        lower = name.strip().lower()
        for pin in self.pins:
            if pin.lower() == lower:
                return pin
        return self.aliases.get(lower)


def _set(*items: str) -> frozenset[str]:
    return frozenset(items)


GND = Pin("gnd")
IO = _set("out", "pwm", "sda", "scl")


# --- microcontroller boards --------------------------------------------------------------------

def esp32_devkit() -> DeviceType:
    """ESP32-WROOM-32 DevKit, the common 30-pin board."""
    adc1, adc2 = {32, 33, 34, 35, 36, 39}, {2, 4, 12, 13, 14, 15, 25, 26, 27}
    strap, serial = {2, 5, 12, 15}, {1, 3}
    pins: dict[str, Pin] = {}
    aliases: dict[str, str] = {"vp": "GPIO36", "vn": "GPIO39", "tx0": "GPIO1", "rx0": "GPIO3", "rx2": "GPIO16",
                               "tx2": "GPIO17", "5v": "VIN", "3.3v": "3V3"}
    for n in (1, 2, 3, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 21, 22, 23, 25, 26, 27, 32, 33, 34, 35, 36, 39):
        caps = set() if n >= 34 else set(IO)
        notes = set()
        if n in adc1 | adc2:
            caps.add("adc")
        if n in adc2:
            notes.add("adc2")
        if n in strap:
            notes.add("strap")
        if n in serial:
            notes.add("serial")
        name = f"GPIO{n}"
        pins[name] = Pin("gpio", caps=frozenset(caps), notes=frozenset(notes))
        aliases.update({f"d{n}": name, f"io{n}": name, str(n): name})
    pins.update({"3V3": Pin("supply", 3.3, ma=500), "VIN": Pin("vbus", 5.0, 4.5, 12.0, ma=250), "GND": GND})
    return DeviceType("esp32_devkit", "ESP32 DevKit (30 pin)", pins, logic=3.3, feeds={"3V3": "VIN"}, aliases=aliases)


def _atmega(kind: str, title: str, analog: int, vin: tuple[float, float], v33_ma: float) -> DeviceType:
    pwm = {3, 5, 6, 9, 10, 11}
    pins: dict[str, Pin] = {}
    aliases: dict[str, str] = {"tx": "D1", "rx": "D0", "sda": "A4", "scl": "A5"}
    for n in range(14):
        caps = {"out"} | ({"pwm"} if n in pwm else set())
        pins[f"D{n}"] = Pin("gpio", caps=frozenset(caps), notes=_set("serial") if n < 2 else frozenset())
        aliases[str(n)] = f"D{n}"
    for n in range(analog):
        caps = {"adc"} if n >= 6 else {"out", "adc"} | ({"sda"} if n == 4 else {"scl"} if n == 5 else set())
        pins[f"A{n}"] = Pin("gpio", caps=frozenset(caps))
        aliases[str(14 + n)] = f"A{n}"
    pins.update({"VIN": Pin("load", None, vin[0], vin[1], ma=20), "5V": Pin("vbus", 5.0, 4.75, 5.25, ma=20),
                 "3V3": Pin("supply", 3.3, ma=v33_ma), "GND": GND})
    aliases.update({"3.3v": "3V3", "+5v": "5V"})
    return DeviceType(kind, title, pins, logic=5.0, five_volt_tolerant=True, feeds={"5V": "VIN", "3V3": "5V"},
                      aliases=aliases)


def arduino_nano() -> DeviceType:
    return _atmega("arduino_nano", "Arduino Nano", 8, (7.0, 12.0), 50)


def arduino_uno() -> DeviceType:
    return _atmega("arduino_uno", "Arduino Uno", 6, (7.0, 12.0), 150)


def pi_pico() -> DeviceType:
    """Raspberry Pi Pico. Every GPIO can do PWM and I2C; GP26 to GP28 also read analog."""
    pins: dict[str, Pin] = {}
    aliases: dict[str, str] = {"3.3v": "3V3", "3v3(out)": "3V3"}
    for n in list(range(23)) + [26, 27, 28]:
        caps = set(IO) | ({"adc"} if n >= 26 else set())
        pins[f"GP{n}"] = Pin("gpio", caps=frozenset(caps))
        aliases.update({f"gpio{n}": f"GP{n}", str(n): f"GP{n}"})
    pins.update({"VSYS": Pin("vbus", 5.0, 1.8, 5.5, ma=50), "3V3": Pin("supply", 3.3, ma=300), "GND": GND})
    return DeviceType("pi_pico", "Raspberry Pi Pico", pins, logic=3.3, feeds={"3V3": "VSYS"}, efficiency=0.9,
                      aliases=aliases)


# --- modules -------------------------------------------------------------------------------------

_SERVOS = {"SG90": (6.0, 650), "MG90S": (6.0, 700), "MG996R": (7.2, 2500)}


def servo(kind: str = "SG90") -> DeviceType:
    """A hobby servo; its current is the stall current, which several servos can hit at once."""
    vmax, stall = _SERVOS[kind]
    pins = {"signal": Pin("in", needs=_set("out", "pwm")), "V+": Pin("load", None, 4.8, vmax, ma=stall), "GND": GND}
    return DeviceType(kind, f"{kind} servo", pins,
                      aliases={"pwm": "signal", "sig": "signal", "vcc": "V+", "+": "V+", "-": "GND"})


def pca9685() -> DeviceType:
    """16-channel servo driver. Its V+ terminal feeds the servo header rows; wire servo power to it."""
    pins = {"VCC": Pin("load", None, 3.0, 5.5, ma=10), "GND": GND, "SDA": Pin("in", needs=_set("sda")),
            "SCL": Pin("in", needs=_set("scl")), "V+": Pin("passive")}
    pins.update({f"PWM{n}": Pin("out", caps=_set("out", "pwm")) for n in range(16)})
    return DeviceType("pca9685", "PCA9685 servo driver", pins, i2c_address=0x40)


def _i2c_module(kind: str, title: str, ma: float, address: int, extra: dict[str, Pin] | None = None) -> DeviceType:
    pins = {"VCC": Pin("load", None, 3.3, 5.0, ma=ma), "GND": GND, "SDA": Pin("in", needs=_set("sda")),
            "SCL": Pin("in", needs=_set("scl")), **(extra or {})}
    return DeviceType(kind, title, pins, i2c_address=address, aliases={"vin": "VCC"})


def ssd1306() -> DeviceType:
    return _i2c_module("ssd1306", "SSD1306 OLED, I2C", 20, 0x3C)


def mpu6050() -> DeviceType:
    return _i2c_module("mpu6050", "MPU6050 IMU (GY-521)", 5, 0x68, {"INT": Pin("out", 3.3)})


def hc_sr04() -> DeviceType:
    pins = {"VCC": Pin("load", None, 4.5, 5.5, ma=15), "GND": GND, "TRIG": Pin("in", needs=_set("out")),
            "ECHO": Pin("out", 5.0)}
    return DeviceType("hc_sr04", "HC-SR04 ultrasonic sensor", pins)


def potentiometer() -> DeviceType:
    """Ends A and B go to a supply and ground; the wiper's voltage tops out at what A is fed."""
    pins = {"A": Pin("passive"), "B": Pin("passive"), "W": Pin("out", needs=_set("adc"), volts_from="A")}
    return DeviceType("potentiometer", "Potentiometer", pins, aliases={"wiper": "W"})


def button() -> DeviceType:
    """A push button: wire one side to a pin and the other to GND, and use the pin's pull-up."""
    return DeviceType("button", "Push button", {"a": Pin("passive"), "b": Pin("passive")})


def switch() -> DeviceType:
    """A power switch, checked as on."""
    return DeviceType("switch", "Power switch", {"a": Pin("passive"), "b": Pin("passive")}, internal=(("a", "b"),))


def _battery(kind: str, title: str, volts: float, low: float, high: float, ma: float) -> DeviceType:
    return DeviceType(kind, title, {"+": Pin("supply", volts, low, high, ma=ma), "-": GND},
                      aliases={"vcc": "+", "bat+": "+", "gnd": "-", "bat-": "-"})


def cell_18650() -> DeviceType:
    return _battery("cell_18650", "18650 cell, 1S", 3.7, 3.0, 4.2, 5000)


def pack_2s_18650() -> DeviceType:
    return _battery("pack_2s_18650", "Two 18650 cells in series, 2S", 7.4, 6.0, 8.4, 5000)


def aa_4() -> DeviceType:
    return _battery("aa_4", "Four AA cells", 6.0, 4.4, 6.4, 2000)


def usb_power_bank() -> DeviceType:
    return _battery("usb_power_bank", "USB power bank", 5.0, 5.0, 5.0, 2000)


def _buck(kind: str, title: str, vout: float, headroom: float, vmax: float, ma: float) -> DeviceType:
    pins = {"IN+": Pin("load", None, vout + headroom, vmax, ma=5), "IN-": GND, "OUT+": Pin("supply", vout, ma=ma),
            "OUT-": GND}
    # IN- and OUT- are one copper ground on these modules.
    return DeviceType(kind, f"{title}, set to {vout:g} V", pins, feeds={"OUT+": "IN+"}, efficiency=0.85,
                      internal=(("IN-", "OUT-"),), aliases={"vin": "IN+", "vout": "OUT+", "in": "IN+", "out": "OUT+"})


def lm2596(vout: float = 5.0) -> DeviceType:
    return _buck("lm2596", "LM2596 buck converter", vout, 1.5, 40.0, 2000)


def mp1584(vout: float = 5.0) -> DeviceType:
    return _buck("mp1584", "MP1584 buck converter", vout, 1.0, 28.0, 2500)


def ams1117_3v3() -> DeviceType:
    pins = {"VIN": Pin("load", None, 4.5, 15.0, ma=5), "VOUT": Pin("supply", 3.3, ma=800), "GND": GND}
    return DeviceType("ams1117_3v3", "AMS1117 3.3 V regulator", pins, feeds={"VOUT": "VIN"})


KINDS: dict[str, Callable[..., DeviceType]] = {
    "esp32_devkit": esp32_devkit, "arduino_nano": arduino_nano, "arduino_uno": arduino_uno, "pi_pico": pi_pico,
    "SG90": lambda: servo("SG90"), "MG90S": lambda: servo("MG90S"), "MG996R": lambda: servo("MG996R"),
    "pca9685": pca9685, "ssd1306": ssd1306, "mpu6050": mpu6050, "hc_sr04": hc_sr04, "potentiometer": potentiometer,
    "button": button, "switch": switch, "cell_18650": cell_18650, "pack_2s_18650": pack_2s_18650, "aa_4": aa_4,
    "usb_power_bank": usb_power_bank, "lm2596": lm2596, "mp1584": mp1584, "ams1117_3v3": ams1117_3v3,
}


def make_device(spec) -> DeviceType:
    """A device from ``"kind"`` or ``{"kind": ..., **settings}``, such as ``{"kind": "lm2596", "vout": 6}``."""
    if isinstance(spec, str):
        kind, settings = spec, {}
    elif isinstance(spec, dict) and isinstance(spec.get("kind"), str):
        settings = dict(spec)
        kind = settings.pop("kind")
    else:
        raise ValueError(f"a device is a kind name or {{'kind': ...}}, got {spec!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown device kind {kind!r}; known: {sorted(KINDS)}")
    try:
        return KINDS[kind](**settings)
    except TypeError:
        raise ValueError(f"{kind} does not take the settings {sorted(settings)}") from None
