"""Check how bought modules are wired before anything is plugged in.

A model file may declare, next to its parts::

    DEVICES = {"mcu": "esp32_devkit", "servo_hip": "SG90", "battery": "pack_2s_18650",
               "buck": {"kind": "lm2596", "vout": 5.0}}
    WIRES = [
        ("battery.+", "buck.IN+"), ("battery.-", "buck.IN-"),
        ("buck.OUT+", "servo_hip.V+", "mcu.VIN"),        # one wire, or a bus of several pins
        ("buck.OUT-", "servo_hip.GND", "mcu.GND"),
        ("mcu.GPIO13", "servo_hip.signal"),
    ]

Device names may match part names, so the servo in the 3D model is the servo in the wiring. The
check finds wrong pins, shorts, wrong voltages, power budgets that a stalled servo would break,
and 5 V signals going into 3.3 V pins. It also returns the pin map that firmware is written from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cadsmith.electronics.devices import DeviceType, Pin, make_device

USB = "USB"
POWER_ROLES = ("supply", "load", "vbus")
_CAUTIONS = {
    "strap": "is a boot strapping pin; a module pulling it at power-up can stop the board from starting",
    "serial": "is the USB serial line; using it can break uploads and the serial monitor",
}


class WiringError(ValueError):
    """The wiring names a device or pin that does not exist."""


@dataclass
class WiringReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    devices: list[dict] = field(default_factory=list)
    nets: list[dict] = field(default_factory=list)
    power: list[dict] = field(default_factory=list)
    pins: list[dict] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"passed": self.passed, "errors": self.errors, "warnings": self.warnings, "devices": self.devices,
                "nets": self.nets, "power": self.power, "pins": self.pins}


@dataclass
class Wiring:
    """Named devices and the wires between their pins."""

    devices: dict[str, DeviceType]
    wires: list[tuple[str, ...]]

    @classmethod
    def from_spec(cls, devices: dict, wires) -> "Wiring":
        try:
            built = {str(name): make_device(spec) for name, spec in devices.items()}
        except ValueError as error:
            raise WiringError(str(error)) from None
        wiring = cls(built, [])
        for wire in wires:
            ends = (wire,) if isinstance(wire, str) else tuple(wire)
            if len(ends) < 2 or not all(isinstance(end, str) for end in ends):
                raise WiringError(f"a wire joins two or more 'device.pin' names, got {wire!r}")
            wiring.wires.append(tuple(wiring.ref(end) for end in ends))
        return wiring

    def ref(self, end: str) -> str:
        """``device.pin`` with the pin's own spelling; raises on unknown names."""
        name, dot, pin = end.partition(".")
        if not dot:
            raise WiringError(f"{end!r} should be 'device.pin'")
        device = self.devices.get(name)
        if device is None:
            raise WiringError(f"{end!r}: no device called {name!r}; devices are {sorted(self.devices)}")
        found = device.resolve(pin)
        if found is None:
            raise WiringError(f"{end!r}: {device.title} has no pin {pin!r}; its pins are {', '.join(device.pins)}")
        return f"{name}.{found}"

    def pin(self, ref: str) -> Pin:
        name, _, pin = ref.partition(".")
        return self.devices[name].pins[pin]

    def device(self, ref: str) -> DeviceType:
        return self.devices[ref.partition(".")[0]]


def _nets(wiring: Wiring) -> list[list[str]]:
    parent: dict[str, str] = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def join(ends):
        for end in ends[1:]:
            parent[find(end)] = find(ends[0])

    for wire in wiring.wires:
        join(wire)
    for name, device in wiring.devices.items():
        for group in device.internal:
            refs = [f"{name}.{pin}" for pin in group]
            if any(ref in parent for ref in refs):
                join(refs)
    groups: dict[str, list[str]] = {}
    for ref in parent:
        groups.setdefault(find(ref), []).append(ref)
    return [sorted(group) for group in groups.values()]


class _Power:
    """Decides which pins supply each net and how much current each supply must give."""

    def __init__(self, wiring: Wiring, nets: list[list[str]]):
        self.w, self.net_of = wiring, {ref: i for i, net in enumerate(nets) for ref in net}
        self.nets = nets
        self.fed_by = {f"{name}.{out}": f"{name}.{src}" for name, d in wiring.devices.items()
                       for out, src in d.feeds.items()}
        self.feeds: dict[str, list[str]] = {}
        for out, src in self.fed_by.items():
            self.feeds.setdefault(src, []).append(out)
        self.mode: dict[str, str] = {}  # vbus ref -> "load", "regulated" or "usb"
        for _ in range(4):  # a few rounds settle chains such as VIN -> 5V -> 3V3
            for name, device in wiring.devices.items():
                for pin_name, pin in device.pins.items():
                    if pin.role == "vbus":
                        ref = f"{name}.{pin_name}"
                        self.mode[ref] = self._vbus_mode(ref)

    def _vbus_mode(self, ref: str) -> str:
        source = self.fed_by.get(ref)
        if source and self.powered(source):
            return "regulated"
        if ref in self.net_of and any(self.acts_as_supply(other) and other != ref for other in self.net(ref)):
            return "load"
        return "usb"

    def net(self, ref: str) -> list[str]:
        return self.nets[self.net_of[ref]] if ref in self.net_of else [ref]

    def acts_as_supply(self, ref: str) -> bool:
        role = self.w.pin(ref).role
        return role == "supply" or (role == "vbus" and self.mode.get(ref, "usb") != "load")

    def acts_as_load(self, ref: str) -> bool:
        role = self.w.pin(ref).role
        return role == "load" or (role == "vbus" and self.mode.get(ref) == "load")

    def live(self, ref: str) -> bool:
        """A supply pin is live if whatever feeds it is powered; a battery always is."""
        if self.w.pin(ref).role == "vbus":
            return self.mode.get(ref, "usb") != "load"
        source = self.fed_by.get(ref)
        return source is None or self.powered(source)

    def powered(self, ref: str) -> bool:
        if self.w.pin(ref).role == "vbus" and self.mode.get(ref, "usb") != "load":
            return True
        return any(self.acts_as_supply(other) and other != ref and self.live(other) for other in self.net(ref))

    def load_draw(self, ref: str, seen=frozenset()) -> float:
        """Current into a load pin: its own draw plus what it feeds inside the device."""
        if ref in seen:
            return 0.0
        own = self.w.pin(ref).ma
        return own + sum(self._input_current(out, ref, seen | {ref}) for out in self.feeds.get(ref, []))

    def _input_current(self, out: str, source: str, seen) -> float:
        drawn = self.supply_draw(out, seen)
        if not drawn:
            return 0.0
        efficiency = self.w.device(out).efficiency
        if efficiency is None:
            return drawn
        vin = next((self.w.pin(o).volts for o in self.net(source) if o != source and self.acts_as_supply(o)), None)
        vout = self.w.pin(out).volts
        return drawn * vout / (vin * efficiency) if vin and vout else drawn

    def supply_draw(self, ref: str, seen=frozenset()) -> float:
        """Current a supply pin must give: every load on its net, plus anything it feeds."""
        if ref in seen:
            return 0.0
        seen = seen | {ref}
        total = sum(self.load_draw(other, seen) for other in self.net(ref) if other != ref and self.acts_as_load(other))
        if self.w.pin(ref).role == "vbus":
            total += self.w.pin(ref).ma if self.mode.get(ref) == "usb" else 0.0
            total += sum(self._input_current(out, ref, seen) for out in self.feeds.get(ref, []))
        return total


def _suggest(wiring: Wiring, used: set[str], mcu: str, needs: frozenset[str]) -> str:
    device = wiring.devices[mcu]
    free = [pin for pin, p in device.pins.items() if p.role == "gpio" and needs <= p.caps
            and f"{mcu}.{pin}" not in used and not p.notes]
    return f"; free pins that can: {', '.join(free[:8])}" if free else ""


def check_wiring(wiring: Wiring) -> WiringReport:
    report = WiringReport()
    nets = _nets(wiring)
    used = {ref for net in nets for ref in net}
    power = _Power(wiring, nets)
    err, warn = report.errors.append, report.warnings.append
    for name, device in wiring.devices.items():
        roles = {pin.role for pin in device.pins.values()}
        group = ("board" if device.is_mcu else
                 "power" if roles <= {"supply", "gnd", "load", "passive"} and ("supply" in roles or device.internal)
                 else "module")
        report.devices.append({"name": name, "kind": device.kind, "title": device.title, "group": group})

    for net in nets:
        roles = {ref: wiring.pin(ref).role for ref in net}
        supplies = [ref for ref in net if power.acts_as_supply(ref)]
        loads = [ref for ref in net if power.acts_as_load(ref)]
        grounds = [ref for ref in net if roles[ref] == "gnd"]
        signals = [ref for ref in net if roles[ref] in ("gpio", "in", "out")]
        kind = "ground" if grounds else "power" if supplies or loads else "signal"
        entry = {"pins": net, "kind": kind}
        report.nets.append(entry)

        if grounds and (supplies or loads):
            err(f"short circuit: {', '.join(supplies + loads)} wired straight to ground ({', '.join(grounds)})")
            continue
        if grounds:
            if signals:
                warn(f"{', '.join(signals)} wired to ground; a pin held low does nothing useful")
            continue
        if supplies or loads:
            entry["volts"] = _check_power_net(wiring, power, net, supplies, loads, report)
            for ref in signals:
                err(f"{ref} is a signal pin but is wired to power ({', '.join(supplies + loads)})")
            continue
        _check_signal_net(wiring, power, net, used, report)

    # Every device with a wire needs a ground, and all grounds must meet.
    ground_nets = [net for net in nets if any(wiring.pin(ref).role == "gnd" for ref in net)]
    for name, device in wiring.devices.items():
        mine = [f"{name}.{pin}" for pin in device.pins]
        if not any(ref in used for ref in mine):
            warn(f"{name} ({device.title}) is not wired to anything")
            continue
        has_ground = any(p.role == "gnd" for p in device.pins.values())
        if has_ground and not any(ref in used and wiring.pin(ref).role == "gnd" for ref in mine):
            err(f"{name} has no ground wire; nothing works without a shared ground")
        if not device.is_mcu:
            for ref in mine:
                if ref not in used and wiring.pin(ref).role in ("in", "load"):
                    warn(f"{ref} is not connected")
    if len(ground_nets) > 1:
        warn("the grounds are not all joined: " + " | ".join(", ".join(net) for net in ground_nets)
             + "; join them, or signals between them have no reference")

    # I2C address clashes on one bus.
    for net in nets:
        sda = [ref for ref in net if wiring.pin(ref).role == "in" and "sda" in wiring.pin(ref).needs]
        addresses: dict[int, list[str]] = {}
        for ref in sda:
            address = wiring.device(ref).i2c_address
            if address is not None:
                addresses.setdefault(address, []).append(ref.partition(".")[0])
        for address, names in addresses.items():
            if len(names) > 1:
                err(f"{', '.join(names)} share I2C address 0x{address:02X} on one bus; change one's address jumper")

    # Power budget per live supply.
    for name, device in wiring.devices.items():
        for pin_name, pin in device.pins.items():
            ref = f"{name}.{pin_name}"
            if ref not in used and pin.role != "vbus":
                continue
            if not power.acts_as_supply(ref) or (pin.role == "vbus" and ref not in used and not power.feeds.get(ref)):
                continue
            draw = power.supply_draw(ref)
            limit = pin.usb_ma if pin.role == "vbus" and power.mode.get(ref) == "usb" else pin.ma
            source = USB if pin.role == "vbus" and power.mode.get(ref) == "usb" else ref
            report.power.append({"supply": source if source != USB else f"{ref} (USB)", "volts": pin.volts,
                                 "draw_ma": round(draw), "max_ma": limit or None})
            if limit and draw > limit:
                warn(f"{source if source != USB else name + ' on USB'} can give about {limit:g} mA, but everything on "
                     f"it can draw {draw:.0f} mA at worst (servos stalled): use a bigger supply or split the load")

    # The pin map firmware is written from.
    for net in nets:
        for ref in net:
            if wiring.pin(ref).role == "gpio":
                others = [o for o in net if o != ref]
                uses = sorted({need for o in others for need in wiring.pin(o).needs} - {"out"}) or (
                    ["out"] if any("out" in wiring.pin(o).needs for o in others) else ["in"])
                report.pins.append({"pin": ref, "to": others, "use": "/".join(uses)})
    return report


def _check_power_net(wiring, power: _Power, net, supplies, loads, report) -> float | None:
    err, warn = report.errors.append, report.warnings.append
    live = [ref for ref in supplies if power.live(ref)]
    if not live:
        if supplies:
            dead = ", ".join(supplies)
            err(f"{dead} feeds {', '.join(loads) or 'nothing'} but is itself unpowered: wire power to "
                f"{', '.join(power.fed_by[s] for s in supplies if s in power.fed_by) or 'its input'}")
        elif loads:
            err(f"nothing powers {', '.join(loads)}")
        return None
    volts = {wiring.pin(ref).volts for ref in live}
    if len(volts) > 1:
        err("supplies at different voltages are joined: "
            + ", ".join(f"{ref} {wiring.pin(ref).volts:g} V" for ref in live))
        return None
    if len(live) > 1:
        warn(f"{', '.join(live)} are joined and both supply power; feed this from one of them")
    source = wiring.pin(live[0])
    nominal = source.volts
    low = source.vmin if source.vmin is not None else nominal
    high = source.vmax if source.vmax is not None else nominal
    for ref in loads:
        pin = wiring.pin(ref)
        if pin.vmax is not None and nominal > pin.vmax:
            err(f"{ref} takes at most {pin.vmax:g} V but gets {nominal:g} V from {live[0]}: it will burn out")
        elif pin.vmin is not None and nominal < pin.vmin:
            err(f"{ref} needs at least {pin.vmin:g} V but gets {nominal:g} V from {live[0]}")
        elif pin.vmax is not None and high > pin.vmax:
            warn(f"{live[0]} reaches {high:g} V when full, above the {pin.vmax:g} V {ref} takes")
        elif pin.vmin is not None and low < pin.vmin:
            warn(f"{live[0]} sags to {low:g} V when nearly empty, below the {pin.vmin:g} V {ref} needs")
    return nominal


def _check_signal_net(wiring, power: _Power, net, used, report) -> None:
    err, warn = report.errors.append, report.warnings.append
    gpios = [ref for ref in net if wiring.pin(ref).role == "gpio"]
    inputs = [ref for ref in net if wiring.pin(ref).role == "in"]
    outputs = [ref for ref in net if wiring.pin(ref).role == "out"]
    if len(gpios) > 1:
        warn(f"{', '.join(gpios)} are wired together; two pins driving one wire can fight")
    for ref in inputs:
        needs = wiring.pin(ref).needs
        ok = any(needs <= wiring.pin(g).caps for g in gpios) or any(needs <= wiring.pin(o).caps for o in outputs)
        if ok:
            continue
        if not gpios and not outputs:
            err(f"{ref} is wired only to {', '.join(o for o in net if o != ref) or 'nothing'}; nothing drives it")
            continue
        for g in gpios:
            missing = ", ".join(sorted(needs - wiring.pin(g).caps)).replace("out", "output")
            hint = _suggest(wiring, used, g.partition(".")[0], needs)
            err(f"{ref} needs a pin that can do {missing}; {g} cannot{hint}")
    for ref in outputs:
        pin = wiring.pin(ref)
        for g in gpios:
            missing = pin.needs - wiring.pin(g).caps
            if missing:
                hint = _suggest(wiring, used, g.partition(".")[0], pin.needs)
                err(f"{ref} needs a pin that can do {', '.join(sorted(missing))}; {g} cannot{hint}")
            volts = pin.volts
            if pin.volts_from:
                end = f"{ref.partition('.')[0]}.{pin.volts_from}"
                volts = next((wiring.pin(s).volts for s in power.net(end) if power.acts_as_supply(s)), None)
            board = wiring.device(g)
            if volts and board.logic and volts > board.logic + 0.3 and not board.five_volt_tolerant:
                err(f"{ref} puts out {volts:g} V but {g} takes only {board.logic:g} V: add a voltage divider "
                    f"(for example 1k and 2k resistors) or a level shifter")
    for g in gpios:
        notes = wiring.pin(g).notes
        for note, text in _CAUTIONS.items():
            if note in notes:
                warn(f"{g} {text}")
        if "adc2" in notes and any("adc" in wiring.pin(o).needs for o in outputs):
            warn(f"{g} is on ADC2, which cannot read while Wi-Fi is on; an ADC1 pin (GPIO32 to GPIO39) can")
