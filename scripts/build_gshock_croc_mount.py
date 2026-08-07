#!/usr/bin/env python3
"""Build the G-Shock Crocs/Jibbitz mount STL from simple parametric solids."""

from pathlib import Path

import numpy as np
import trimesh


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "gshock_jibitz_mount.stl"

CASE_W = 44.5
CASE_L = 50.0
PLATFORM_T = 3.6
WALL_T = 2.4
WALL_H = 11.5
LIP = 1.3


def cylinder(radius: float, height: float, z0: float, radius_top=None):
    if radius_top is None or abs(radius_top - radius) < 1e-9:
        mesh = trimesh.creation.cylinder(radius=radius, height=height, sections=96)
        mesh.apply_translation([0, 0, z0 + height / 2])
    else:
        # Trimesh's cone/frustum is created from z=0..height (unlike its
        # centered cylinder), so translate by z0 only.
        mesh = trimesh.creation.cone(
            radius=radius, radius_top=radius_top, height=height, sections=96
        )
        mesh.apply_translation([0, 0, z0])
    return mesh


def rounded_box(size, radius, center):
    """Axis-aligned rounded XY box, flat on top and bottom."""
    sx, sy, sz = size
    cx, cy, cz = center
    pieces = []
    pieces.append(trimesh.creation.box([sx - 2 * radius, sy, sz]))
    pieces.append(trimesh.creation.box([sx, sy - 2 * radius, sz]))
    for x in (-sx / 2 + radius, sx / 2 - radius):
        for y in (-sy / 2 + radius, sy / 2 - radius):
            c = trimesh.creation.cylinder(radius=radius, height=sz, sections=48)
            c.apply_translation([x, y, 0])
            pieces.append(c)
    shape = trimesh.boolean.union(pieces, engine="manifold")
    shape.apply_translation([cx, cy, cz])
    return shape


def build():
    parts = [
        cylinder(5.5, 0.6, 0.0, radius_top=6.4),
        # Small overlaps make each stacked peg section a genuinely fused body.
        cylinder(6.4, 1.7, 0.5),
        cylinder(3.8, 3.9, 2.1),
        # Deliberate 0.2 mm overlap with the neck. A coplanar seam exported as
        # two watertight shells even though most slicers would fuse it.
        cylinder(8.2, 2.9, 5.8, radius_top=10.0),
        rounded_box(
            [CASE_W + 7.2, CASE_L + 7.0, PLATFORM_T],
            3.0,
            [0, 0, 8.4 + PLATFORM_T / 2],
        ),
    ]

    wall_z = 11.6 + WALL_H / 2
    for sign in (-1, 1):
        parts.append(
            rounded_box(
                [WALL_T, CASE_L, WALL_H],
                1.0,
                [sign * (CASE_W / 2 + WALL_T / 2), 0, wall_z],
            )
        )
        # Inward snap lip. Its underside is a short bridge most FDM slicers can
        # handle; PETG or nylon is preferred for repeated flexing.
        lip_w = WALL_T + LIP
        lip_center_x = CASE_W / 2 + (WALL_T - LIP) / 2
        parts.append(
            rounded_box(
                [lip_w, CASE_L - 5.0, 1.8],
                0.7,
                [sign * lip_center_x, 0, 22.1 + 0.9],
            )
        )

    # Corner stops leave a 28.5 mm center opening at both ends for the G-Shock
    # straps. The first draft used a full-width stop that visually passed QA
    # but would have blocked the straps; keep this clearance explicit.
    end_tab_w = 8.0
    strap_gap = 28.5
    end_tab_x = strap_gap / 2 + end_tab_w / 2
    for sy in (-1, 1):
        for sx in (-1, 1):
            parts.append(
                rounded_box(
                    [end_tab_w, WALL_T, 5.5],
                    0.8,
                    [sx * end_tab_x, sy * (CASE_L / 2 + WALL_T / 2), 11.6 + 2.75],
                )
            )

    mesh = trimesh.boolean.union(parts, engine="manifold")
    mesh.remove_unreferenced_vertices()
    mesh.merge_vertices()
    if not mesh.is_watertight or not mesh.is_volume:
        raise RuntimeError("Generated mesh failed watertight/volume QA")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(OUT)
    return mesh


if __name__ == "__main__":
    result = build()
    print(f"wrote {OUT}")
    print(f"vertices={len(result.vertices)} faces={len(result.faces)}")
    print(f"watertight={result.is_watertight} volume={result.is_volume}")
    print("bounds_mm=" + np.array2string(result.bounds, precision=2))
    print("extents_mm=" + np.array2string(result.extents, precision=2))
