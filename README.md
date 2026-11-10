# cadsmith

Describe a part in plain words, get a printable 3D model.

cadsmith turns a short text description into parametric CAD (CadQuery), checks that the parts
will actually print and fit together, and lays them out on Bambu Lab plates.

## Goals

- **Text to CAD:** a short description becomes a parametric `.py` model with STEP, STL and 3MF output.
- **Printable by default:** wall thickness, overhangs and print orientation are measured, not guessed.
- **Parts that fit:** every pair of mating parts gets a stated clearance, and it is checked.
- **Ready to print:** parts are arranged on named Bambu plates in one 3MF.

## Status

Early setup. Nothing to install yet.
