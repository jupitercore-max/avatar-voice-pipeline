# G-Shock Crocs/Jibbitz mount v2

This replaces the invalid desk-stand-style first draft. It is a one-piece
Crocs shoe mount with a Jibbitz-style retaining button underneath and a compact
snap cradle above.

## Fit target

- Nominal cradle: 44.5 x 50.0 mm
- Retaining-lip opening: 41.9 mm
- Intended cases: DW-5600 (about 42.8 x 48.9 mm) and GA-2100 (about
  45.4 x 48.5 mm)
- Overall envelope: 51.7 x 57.0 x 23.9 mm
- Original watch straps exit through the open centers at the front and rear
- Shoe plug: 12.8 mm lower button, 7.6 mm neck, 20 mm upper load pad

Case dimensions vary across the G-Shock range. This is a prototype for the two
common case families above, not a universal fit for every G-Shock.

## Print recommendation

- PETG or nylon; PLA is acceptable only for a first fit check
- 0.20 mm layers, four perimeters, 30% infill
- Print on a front/rear cradle edge with a brim; use build-plate-only support
  beneath the horizontal shoe peg
- Do not add dense support inside the cradle or under the snap lips
- Test the shoe plug first and stop if insertion requires excessive force

## QA completed

- STL is one connected body
- Watertight volume; 1,987 vertices / 3,970 faces
- Mesh bounds and the two target-case retention clearances are asserted by
  `scripts/build_gshock_croc_mount.py`

Physical Crocs-hole fit, rail flex, and real-watch retention still require
Ray's test print. Those cannot be proven from mesh QA alone.
