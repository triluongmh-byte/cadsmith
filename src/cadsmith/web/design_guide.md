# How to design here

## Plan before you write geometry

Put a short plan in the docstring at the top of model.py and keep it current: what the thing is and does, overall
envelope, the list of physical parts, each joint and what drives it, the bought hardware, and your assumptions. Fill gaps
in a vague request with sensible, popular choices instead of asking. Numbers the user gave are requirements: the finished model
measures them within 1 mm on every axis, and you do not trade one away because it seems unneeded. If one truly cannot be
met, for instance real hardware does not fit the stated envelope, build the closest honest version and say so.

## Object or machine

A single object (stand, hook, tray, vase) is one solid unless splitting clearly helps printing.

Anything with motors, electronics, moving joints, or described as a robot, mechanism or device is an assembly of real
parts. It is never one sculpted solid that merely resembles the thing. Build it in this order:

1. The frame or chassis that carries the load and locates everything.
2. Bought devices placed on it: servos, boards, batteries, each in a pocket or on standoffs, with screw access.
3. Joints: for each one, name the axis, the actuator or pin, and what supports the other side.
4. Moving links, each its own part, attached to the shaft or pin in the assembled pose.
5. Covers last. A cover must not be what holds a device in place.

Leave room inside for wires and connectors, and an opening to run them.

## Wiring

When the thing has electronics, the user buys modules, wires them and flashes firmware: no custom circuit boards.
Declare the modules and every wire in model.py so the build checks them:

    DEVICES = {"mcu": "esp32_devkit", "servo_hip": "SG90", "battery": "pack_2s_18650",
               "buck": {"kind": "lm2596", "vout": 5.0}}
    WIRES = [("battery.+", "buck.IN+"), ("battery.-", "buck.IN-"),
             ("buck.OUT+", "servo_hip.V+", "mcu.VIN"), ("buck.OUT-", "servo_hip.GND", "mcu.GND"),
             ("mcu.GPIO13", "servo_hip.signal")]

- Name a device after its part when it has one (servo_hip above is also a BOUGHT part), so the model and wiring agree.
- A wire is two or more "device.pin" names; list a power or ground bus as one tuple.
- Kinds: esp32_devkit, arduino_nano, arduino_uno, pi_pico, SG90, MG90S, MG996R, pca9685, ssd1306, mpu6050, hc_sr04,
  potentiometer, button, switch, cell_18650, pack_2s_18650, aa_4, usb_power_bank, lm2596, mp1584, ams1117_3v3.
  For anything else, pick the nearest kind and say so.
- Power servos from a battery through a buck converter, never from the board's 3V3 or 5V pin, and join every ground.
- More than about six servos: use a pca9685 on I2C instead of one board pin each.
- The checks report wrong pins, wrong voltages, overloaded supplies and 5 V signals into 3.3 V pins. Fix every error.
- In your final message, give the user the pin map as a short table, since they wire it by hand from that.

## Firmware

Each build makes a PlatformIO project for the wired board: include/pins.h holds a PIN_ or CH_ macro for every wired
pin, named after the device (PIN_SERVO_HIP, PIN_SONAR_TRIG, PIN_I2C_SDA, CH_SERVO_HIP for a PCA9685 channel). Until
the project has ./firmware/main.cpp, the build ships a starter that only tests the wiring.

When the user says what the thing should do (walk, sweep, follow a hand), write ./firmware/main.cpp for the Arduino
framework: #include "pins.h", use only its macros for pins, and use the libraries the starter uses (ESP32Servo on
ESP32, Servo on AVR and Pico, Adafruit_PWMServoDriver, Adafruit_SSD1306, Adafruit_MPU6050). Keep it one file, with
named constants for angles and timings at the top. Move servos gradually rather than jumping, and never drive
them past the angles the mechanism allows. When WIRES changes, check main.cpp still uses macros that exist.

A machine should look like one. Show every servo with its servo_horn, the board, the battery, and real fasteners as parts
(screws through clearance holes into nuts) at each place the structure is bolted together. Make frames open: windows or
truss cut-outs in the walls so the inside reads, and a top plate with a screen window or service opening rather than a
blank lid. Walking robots default to two actuated joints per leg, hip and knee, with femur and tibia as separate printed
parts and rounded hubs at the joints. Use fewer only when asked or when the envelope cannot hold them, and say which.

## Parts

- parts() returns one entry per physical piece, each in its assembled pose. Four legs are four entries, placed by a loop.
- Give parts plain names: frame, lid, leg_front_left, servo_front_left.
- Write one function per part, built around its own origin, then move it into place. Dimensions live in Params; a mating
  dimension is one value used by both sides, with the clearance applied in one place.
- List bought items in BOUGHT, a set of part names. They are shown in grey, fit-checked and never printed.
- Everything in one part must be connected. Nothing may float.
- Each printed part needs a flat face for the bed. Split a part when that removes supports or makes it assemble.

## Bought hardware

Use cadsmith.cadlib.components for servos, boards, 18650 cells, screws and nuts. Its dimensions are nominal, so say the
user should measure theirs before a tight print. Cut a servo's seat with servo_cutout using the same placement as the
servo. Other devices: use the size you are confident of, build a plain envelope, and state it as an assumption.

## Fits, which are checked

- Two printed parts need 0.3 mm of clearance everywhere unless a Fit says otherwise. Parts resting on each other need
  Fit("a", "b", seat="z") with the axis they rest along. A rotating pin in a hole needs 0.3 mm or more on the radius.
- A bought part may touch a printed part but may not overlap it. Two bought parts are not checked against each other.
- A screw through a printed part needs a clearance hole. A self-tapping screw overlaps its pilot hole, so do not add
  that screw as a part. Show screws where they go through clearance holes into nuts.

## Printing

- Walls 1.6 mm or more where structural, never under 0.8 mm. Ribs instead of thick blocks.
- Overhangs up to 45 degrees print without support. Chamfer bottom edges instead of filleting them; a horizontal hole
  over 8 mm wants a teardrop top. Supports are acceptable only where no reorientation or split avoids them.
- Fasteners: M2 and M3 are the defaults; tables in cadsmith.cadlib.tables give holes, nuts and inserts.

## Make it look designed

- Silhouette first. A named animal or object must be recognised from the top view alone, even when it is a machine
  inside. Its defining features, such as a crab's wide shell and big claws, are large deliberate forms and separate
  parts where they can be, never small notches on a plate.
- A plain extrusion of a 2D outline reads as a placeholder. Give visible forms real shape: taper limbs, round joint ends
  about the pivot, add ribs and pockets that follow the load.
- One radius language: pick one corner radius per part with design_radius_for and apply soften_edges to visible outer
  edges. Leave mating faces sharp.
- Keep proportions deliberate, features on a shared grid and pattern, symmetry exact (build one side, mirror the other).
- Lightening pockets and truss cut-outs on frames look right and save filament.

## CadQuery that does not break

Add fillets last and to selected edges; if one fails, reduce the radius rather than dropping the shape. Avoid exactly
tangent or coincident faces between bodies you union: overlap them by 0.5 mm. Use `.rotate((0,0,0),(0,0,1),deg)` then
`.translate(...)`, in that order. A file that does not run costs a whole round, so keep it simple and correct.
