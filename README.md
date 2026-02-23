# OpenFlight (.flt) Importer/Exporter for Blender

Blender addon for importing and exporting files in the **OpenFlight** binary format (`.flt`), used in real-time 3D visualization and simulation software (e.g. Presagis Creator, OpenSceneGraph).

Created by **Aleksander Pininski** — https://github.com/eltales
Based on the OpenFlight format specification and the original [blight-1.2](https://sourceforge.net/projects/blight/) by Greg MacDonald.

---

## Features

### Import
- Full scene hierarchy: Header, Group, Object, Face, LOD, Switch, DOF, ExternalReference
- Vertices: all 4 types (Color, Color+Normal, Color+UV, Color+Normal+UV)
- Mesh nodes (op=84) with Local Vertex Pool (op=85) and Mesh Primitives (op=86)
- Texture palette with UV mapping
- Material palette → Principled BSDF (diffuse, roughness, alpha, emission)
- Packed color and color palette lookup on faces
- Transform matrices (op=49)
- LongID (op=33), Comment (op=31)
- External references (op=63) with optional recursive import
- **Light Points (op=130)** imported as Blender POINT / SPOT / AREA lights:
  - 3D position read from the vertex list child record (`IDX_LP → PUSH → VERT_LIST → POP`)
  - Light type (omni/uni/bidi), color and intensity from Appearance Palette (op=128)
  - `intensityFront` mapped to Blender energy: `energy = intensityFront × 1000 W`
  - Radius set to **0** (FLT `actualSize` is a screen-space sprite size, not a physical radius)
  - Raw op=128 bytes preserved in a scene custom property for lossless re-export

### Export
- Scene hierarchy: Group (Empty objects) → Object (Mesh) → Face → VertexList
- Full vertex palette (VERTEX_CNUV, op=70) with normals and UV
- Material palette (op=113) — extracted from Principled BSDF:
  - Base Color → diffuse / ambient
  - Roughness → shininess
  - Alpha → transparency
- Face packed color (ABGR) preserved on round-trip
- Face transparency encoding (`transparency = (1 - alpha) × 65535`)
- Correct PUSH/POP nesting around VertexList records
- Texture palette with filename references
- LongID for object names longer than 8 characters
- **Blender Light objects** exported as FLT Indexed Light Points (op=130):
  - POINT → omni, SPOT → unidirectional, AREA → bidirectional
  - Round-trip: raw op=128 bytes reused if the light was imported from FLT
  - Synthesis: new op=128 Appearance Palette record generated from Blender light properties
  - `energy ÷ 1000` → `intensityFront` (inverse of import scale)

---

## Supported Blender Version

Blender **5.0.1** and later (Python 3, bpy API 4.x / 5.x).

---

## Installation

1. Download the ZIP: on the repository page click **Code → Download ZIP**
2. In Blender: **Edit → Preferences → Add-ons → Install…**
3. Select the downloaded `.zip` file
4. Enable **Import-Export: OpenFlight FLT format**

---

## Usage

### Import
**File → Import → OpenFlight (.flt)**

| Option | Default | Description |
|--------|---------|-------------|
| Import Textures | ✓ | Load and assign textures from the palette |
| Import Materials | ✓ | Create Principled BSDF from material palette |
| Import External Refs | ✓ | Recursively import referenced `.flt` files |
| Scale | 1.0 | Scale factor for coordinates |

### Export
**File → Export → OpenFlight (.flt)**

| Option | Default | Description |
|--------|---------|-------------|
| Selected Only | ✗ | Export only selected objects |
| Scale | 1.0 | Should match the scale used during import |

---

## Light Point mapping

FLT light points use a purely visual rasterizer model — `intensityFront` (0–1) is a dimensionless multiplier with no physical unit. The import maps it to Blender Watts using a practical heuristic calibrated for metric scenes:

```
Blender energy [W] = intensityFront × 1 000
```

| FLT intensityFront | Blender energy | Notes |
|--------------------|----------------|-------|
| 1.0 | 1 000 W | Full brightness |
| 0.5 | 500 W | Half brightness |
| 0.0 | 1 W | Disabled in FLT — minimum so the object is selectable |

For a street lamp at 6 m height illuminating the ground at ~20 lux (urban standard):
`P = 20 × 4π × 6² ≈ 9 000 W` — scale up in Blender if needed.

Export reverses the formula: `intensityFront = energy ÷ 1 000`, clamped to [0, 1].
Round-tripped lights (imported then re-exported) use the original raw palette bytes, so `intensityFront` is preserved exactly.

---

## Notes

- The exporter writes a **vertex palette** shared across all meshes — identical vertices (same position, normal, UV) are deduplicated automatically.
- Materials are exported as FLT **material palette** entries (op=113). Use Principled BSDF for best round-trip fidelity.
- Texture paths are stored as **filenames only** (no directory). Place textures in the same folder as the `.flt` file.
- The addon has been tested with files from **Presagis Creator** (format revision 15.8 / 16.0).

---

## Changelog

### v2.4.0
- Light Points (op=130) imported as Blender POINT/SPOT/AREA lights
- Correct 3D position from vertex list (`IDX_LP → PUSH → VERT_LIST → POP`)
- Appearance Palette (op=128) field offsets corrected (actualSize@332, hLobeAngle@364, vLobeAngle@368, rolloffExponent@372)
- Light energy mapped: `intensityFront × 1000 W` on import, `÷ 1000` on export
- Radius = 0 on import (FLT actualSize is a sprite display size, not Blender physical radius)
- Blender Light objects exported as FLT Indexed Light Points with synthesized or round-tripped op=128

### v2.3.0
- Full Light Point round-trip: op=128 raw bytes stored as base64 in scene custom property
- LightPoint custom properties preserved: `flt_lp_app_idx`, `flt_lp_anim_idx`, `flt_lp_draw_order`, `flt_lp_flags`

### v2.2.0 — v2.0.0
- Initial public release: full import/export of geometry, materials, textures, hierarchy

---

## License

MIT License — see [LICENSE](LICENSE) for details.
