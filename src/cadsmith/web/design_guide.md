# How to design here

## Plan before you write geometry

Put a short plan in the docstring at the top of model.py and keep it current: what the thing is and does, overall
envelope, the list of physical parts, each joint and what drives it, the bought hardware, and your assumptions. Fill gaps
in a vague request with sensible, popular choices instead of asking. Numbers the user gave are requirements. If they
cannot be met, for instance real hardware does not fit the stated envelope, build the closest honest version and say so.

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
