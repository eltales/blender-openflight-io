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

---

## Supported Blender Version

Blender **5.0.1** and later (Python 3, bpy API 4.x / 5.x).
Not compatible with Blender 2.x / 3.x due to API changes.

---

## Installation

1. Download `io_import_flt.zip` from the [Releases](../../releases) page
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

## Notes

- The exporter writes a **vertex palette** shared across all meshes — identical vertices (same position, normal, UV) are deduplicated automatically.
- Materials are exported as FLT **material palette** entries (op=113). Use Principled BSDF for best round-trip fidelity.
- Texture paths are stored as **filenames only** (no directory). Place textures in the same folder as the `.flt` file.
- The addon has been tested with files from **Presagis Creator** (format revision 15.8 / 16.0).

---

## License

MIT License — see [LICENSE](LICENSE) for details.
