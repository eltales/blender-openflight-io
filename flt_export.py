"""
FLT Exporter - converts a Blender scene to a binary OpenFlight (.flt) file.
Format: big-endian. Each record: int16 opcode + uint16 length (includes 4-byte header).
"""

import os
import math
import struct
import base64
import traceback

import bpy
import bmesh
import mathutils


# ── Opcodes ───────────────────────────────────────────────────────────────────
OP_HEADER          = 1
OP_GROUP           = 2
OP_OBJECT          = 4
OP_FACE            = 5
OP_PUSH            = 10
OP_POP             = 11
OP_COLOR_PALETTE   = 32
OP_LONG_ID         = 33
OP_TEXTURE_PALETTE = 64
OP_VERTEX_PALETTE  = 67
OP_VERTEX_CNUV     = 70
OP_VERTEX_LIST     = 72
OP_MATERIAL        = 113
OP_LIGHTPT_APP_PAL = 128
OP_INDEXED_LP      = 130

# ── Face misc_flags ────────────────────────────────────────────────────────────
FACE_NOCOLOR     = 0x40000000   # color comes from material palette
FACE_NO_ALT_COLOR = 0x20000000  # no alternate color (standard FLT convention)
FACE_PACKEDCOLOR = 0x10000000   # color comes from packedColor field

# ── Vertex flags ───────────────────────────────────────────────────────────────
VERT_NO_COLOR = 0x2000          # vertex has no individual color, use face color

# ── Vertex palette geometry ────────────────────────────────────────────────────
# VERTEX_CNUV record: 4 (header) + 56 (data) + 4 (spare) = 64 bytes
VERT_RECORD_SIZE = 64
# VP record header: 4 (op+len) + 4 (total_vp uint32) = 8 bytes
VP_HEADER_SIZE   = 8

# byte_offset of i-th vertex from vp_record_pos (= start of VP record):
# vertex[0] is at offset VP_HEADER_SIZE = 8
# vertex[i] is at offset VP_HEADER_SIZE + i * VERT_RECORD_SIZE
# Confirmed from original file: VertexList offsets = [8, 72, 136, 200]
def vert_byte_offset(i):
    return VP_HEADER_SIZE + i * VERT_RECORD_SIZE


# ── FltWriter: low-level binary writer ───────────────────────────────────────

class FltWriter:
    """Writes big-endian binary data to a file-like object."""

    def __init__(self, fileobj):
        self._f = fileobj

    # ── Record header ─────────────────────────────────────────────────────

    def rec(self, opcode, length):
        """Write a 4-byte record header (opcode int16 + length uint16)."""
        self._f.write(struct.pack('>hH', opcode, length))

    # ── Primitive types ───────────────────────────────────────────────────

    def uint(self, v):
        self._f.write(struct.pack('>I', v & 0xFFFFFFFF))

    def int_(self, v):
        self._f.write(struct.pack('>i', v))

    def ushort(self, v):
        self._f.write(struct.pack('>H', v & 0xFFFF))

    def short(self, v):
        self._f.write(struct.pack('>h', v))

    def uchar(self, v):
        self._f.write(struct.pack('>B', v & 0xFF))

    def char(self, v):
        self._f.write(struct.pack('>b', v))

    def float_(self, v):
        self._f.write(struct.pack('>f', v))

    def double(self, v):
        self._f.write(struct.pack('>d', v))

    def zeros(self, n):
        """Write n zero bytes (padding)."""
        self._f.write(b'\x00' * n)

    def string(self, s, length):
        """Write string as exactly `length` bytes, null-padded / truncated."""
        encoded = s.encode('latin-1', errors='replace')[:length]
        self._f.write(encoded.ljust(length, b'\x00'))

    def raw(self, data):
        self._f.write(data)

    def tell(self):
        return self._f.tell()


# ── Entry point ───────────────────────────────────────────────────────────────

def save(context, filepath, export_selected=False, scale=1.0, **kwargs):
    """Export selected/all objects to a FLT file."""
    print(f"FLT Export: {filepath}")
    try:
        if export_selected:
            objects = [o for o in context.selected_objects]
        else:
            objects = list(context.scene.objects)

        exporter = FltExporter(filepath, objects, scale=scale)
        exporter.export()
    except Exception:
        traceback.print_exc()
        return {'CANCELLED'}
    return {'FINISHED'}


# ── FltExporter ───────────────────────────────────────────────────────────────

class FltExporter:

    def __init__(self, filepath, objects, scale=1.0):
        self.filepath = filepath
        self.objects  = objects
        self.scale    = scale

        # Vertex palette: list of (x, y, z, nx, ny, nz, u, v)
        self.vp       = []
        # Map from vertex key tuple to palette index
        self._vp_map  = {}

        # Texture palette: list of str paths (index = FLT tex index)
        self.tex_paths = []
        # Map from abs path to FLT texture index
        self._tex_map  = {}

        # Material palette: list of dicts with FLT material data
        self.mat_list  = []
        # Map from Blender material name to FLT material index
        self._mat_index_map = {}

        # Light Point Appearance Palette (op=128) raw record bytes to write
        self._lp_palette_raw_list = []
        # Map from light object name → app_idx in _lp_palette_raw_list
        self._lp_appidx_map = {}
        # Map from light object name → vertex palette index (for 3D position)
        self._lp_vertex_map = {}

    # ── Public ────────────────────────────────────────────────────────────

    def export(self):
        self._build_palettes()
        with open(self.filepath, 'wb') as f:
            w = FltWriter(f)
            self._write_flt_header(w)
            self._write_color_palette(w)
            self._write_material_palette(w)
            self._write_lp_app_palette(w)
            self._write_texture_palette(w)
            self._write_vertex_palette(w)
            self._write_scene(w)

    # ── Pass 1: build palettes ─────────────────────────────────────────────

    def _build_palettes(self):
        """Collect all vertices, textures, materials and LP palette data."""
        for obj in self.objects:
            if obj.type == 'MESH':
                self._collect_from_mesh(obj)
        # Collect vertex palette entries for light point 3D positions.
        # Each LIGHT object gets one vertex (position only, dummy normal/UV).
        for obj in self.objects:
            if obj.type == 'LIGHT' or obj.get('flt_type') == 'LIGHTPOINT':
                s = 1.0 / self.scale
                loc = obj.matrix_world.translation
                key = (
                    round(loc.x * s, 6), round(loc.y * s, 6), round(loc.z * s, 6),
                    0.0, 0.0, 1.0,  # dummy normal (Z-up)
                    0.0, 0.0,       # no UV
                )
                if key not in self._vp_map:
                    self._vp_map[key] = len(self.vp)
                    self.vp.append(key)
                self._lp_vertex_map[obj.name] = self._vp_map[key]
        self._collect_lp_palette()

    def _collect_from_mesh(self, obj):
        # Apply modifiers via evaluated depsgraph
        depsgraph = bpy.context.evaluated_depsgraph_get()
        obj_eval  = obj.evaluated_get(depsgraph)
        mesh      = obj_eval.to_mesh()
        if mesh is None:
            return

        uv_layer = mesh.uv_layers.active
        s = 1.0 / self.scale  # invert import scale

        # World matrix for transforming coordinates
        mx   = obj.matrix_world
        mx_n = mx.to_3x3().inverted_safe().transposed()  # for normals

        for poly in mesh.polygons:
            for li in poly.loop_indices:
                loop = mesh.loops[li]
                vi   = loop.vertex_index
                v    = mesh.vertices[vi]

                co  = mx @ v.co
                nor = (mx_n @ v.normal).normalized()
                uv  = uv_layer.data[li].uv if uv_layer else mathutils.Vector((0.0, 0.0))

                key = (
                    round(co.x * s, 6), round(co.y * s, 6), round(co.z * s, 6),
                    round(nor.x, 5),    round(nor.y, 5),    round(nor.z, 5),
                    round(uv.x, 5),     round(uv.y, 5),
                )
                if key not in self._vp_map:
                    self._vp_map[key] = len(self.vp)
                    self.vp.append(key)

        # Collect materials and textures from material slots
        for mat in obj.data.materials:
            if mat is None:
                continue
            # Material palette entry (one FLT material per Blender material)
            if mat.name not in self._mat_index_map:
                self._mat_index_map[mat.name] = len(self.mat_list)
                self.mat_list.append(self._extract_mat_data(mat))
            # Texture palette
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == 'TEX_IMAGE' and node.image:
                        path = bpy.path.abspath(node.image.filepath)
                        if path and path not in self._tex_map:
                            self._tex_map[path] = len(self.tex_paths)
                            self.tex_paths.append(path)

        obj_eval.to_mesh_clear()

    # ── LP Appearance Palette collection ──────────────────────────────────

    def _collect_lp_palette(self):
        """Populate _lp_palette_raw_list and _lp_appidx_map.

        Two strategies:
        1. Round-trip: scene has 'flt_lp_app_palette' (list of base64 raw records
           stored during import).  Use those bytes directly; each light object
           already has its correct flt_lp_app_idx.
        2. Synthesis: no scene property exists.  Build an op=128 record from
           every LIGHT object that will be exported as a light point, assigning
           sequential palette indices.
        """
        scene_palette = bpy.context.scene.get('flt_lp_app_palette', [])
        if scene_palette:
            # Strategy 1: use raw bytes from import
            for entry_b64 in scene_palette:
                try:
                    self._lp_palette_raw_list.append(base64.b64decode(entry_b64))
                except Exception:
                    pass
            # appIdx per object comes from flt_lp_app_idx custom property
        else:
            # Strategy 2: synthesize from LIGHT objects
            for obj in self.objects:
                if obj.type == 'LIGHT' or obj.get('flt_type') == 'LIGHTPOINT':
                    if obj.type == 'LIGHT' and obj.name not in self._lp_appidx_map:
                        idx = len(self._lp_palette_raw_list)
                        self._lp_palette_raw_list.append(
                            self._synth_lp_app_record(obj, idx)
                        )
                        self._lp_appidx_map[obj.name] = idx

    def _synth_lp_app_record(self, obj, palette_index):
        """Synthesize a complete 412-byte op=128 record from a Blender Light.

        Payload layout (408 bytes, big-endian):
          0    int32    index
          4    char[256] name
          260  int16    state (0=enabled)
          262  int16    lp_type (0=omni, 1=uni, 2=bidi)
          264  float    intensityFront
          268  float    intensityBack (0)
          272  float    minDefocus (0)
          276  float    maxDefocus (0)
          280  int16    fadingMode (0)
          282  int16    fogPunch (0)
          284  float    dirAmbIntensity (0)
          288  float    significance (1)
          292  int32    visibilityRange (0)
          296  float    fadeRangeRatio (1)
          300..315  floats: fadeIn/Out/LOD1/LOD2 (zeros)
          316  int16    primaryColor (127=white)
          318  int16    altColor (127)
          320  uint16   flags (0)
          322  int16    reserved (2 bytes padding)
          324  float    minPixelSize (1)
          328  float    maxPixelSize (100)
          332  float    actualSize
          336..361  (tp_*/fog_* fields, zeros)
          362  int16    direction (0)
          364  float    hLobeAngle
          368  float    vLobeAngle
          372  float    rolloffExponent
          376..407  (anim/misc fields, zeros)
        """
        light = obj.data
        lp_type = {'POINT': 0, 'SPOT': 1, 'AREA': 2, 'SUN': 0}.get(light.type, 0)

        intensity  = light.energy / 1000.0   # inverse of import scale (import: × 1000)
        # Blender radius is 0 for imported FLT lights (set intentionally).
        # FLT actualSize is the sprite display size, so default to 0.3 m when
        # the Blender radius is zero or unset.
        radius_bl   = getattr(light, 'radius', 0.0) or getattr(light, 'shadow_soft_size', 0.0)
        actual_size = (radius_bl if radius_bl > 0.0 else 0.3) / self.scale

        h_lobe = 45.0
        v_lobe = 45.0
        rolloff = 1.0
        if light.type == 'SPOT':
            h_lobe = v_lobe = math.degrees(light.spot_size)
            blend  = max(0.001, getattr(light, 'spot_blend', 0.15))
            rolloff = 1.0 / blend

        # Build 408-byte payload using struct.pack_into on a zero-filled bytearray
        payload = bytearray(408)
        name_enc = (obj.name[:255] + '\x00').encode('latin-1', 'replace')
        struct.pack_into('>i',  payload,   0, palette_index)
        payload[4:4 + min(len(name_enc), 256)] = name_enc[:256]
        struct.pack_into('>h',  payload, 260, 0)              # state = enabled
        struct.pack_into('>h',  payload, 262, lp_type)
        struct.pack_into('>f',  payload, 264, intensity)
        struct.pack_into('>f',  payload, 288, 1.0)            # significance
        struct.pack_into('>f',  payload, 296, 1.0)            # fadeRangeRatio
        struct.pack_into('>h',  payload, 316, 127)            # primaryColor = white
        struct.pack_into('>h',  payload, 318, 127)            # altColor
        struct.pack_into('>H',  payload, 320, 0)              # flags
        # offset 322-323: reserved/padding (leave as zero)
        struct.pack_into('>f',  payload, 324, 1.0)            # minPixelSize
        struct.pack_into('>f',  payload, 328, 100.0)          # maxPixelSize
        struct.pack_into('>f',  payload, 332, actual_size)
        struct.pack_into('>h',  payload, 362, 0)              # direction
        struct.pack_into('>f',  payload, 364, h_lobe)
        struct.pack_into('>f',  payload, 368, v_lobe)
        struct.pack_into('>f',  payload, 372, rolloff)

        return struct.pack('>hH', OP_LIGHTPT_APP_PAL, 412) + bytes(payload)

    # ── Material extraction from Blender Principled BSDF ──────────────────

    def _extract_mat_data(self, mat):
        """Extract FLT material parameters from a Blender Principled BSDF material.

        Inverses of import logic in flt_import.py:
          import:  roughness = max(0.25, sqrt(1 - shininess/128))
          export:  shininess = (1 - roughness^2) * 128
        """
        data = {
            'name':      mat.name[:12],
            'ambient':   (0.1, 0.1, 0.1),
            'diffuse':   (0.8, 0.8, 0.8),
            'specular':  (0.2, 0.2, 0.2),
            'emissive':  (0.0, 0.0, 0.0),
            'shininess': 10.0,
            'alpha':     1.0,
        }
        if mat.use_nodes:
            for node in mat.node_tree.nodes:
                if node.type == 'BSDF_PRINCIPLED':
                    r, g, b, _ = node.inputs['Base Color'].default_value
                    data['diffuse'] = (r, g, b)
                    data['ambient'] = (r * 0.2, g * 0.2, b * 0.2)
                    roughness = node.inputs['Roughness'].default_value
                    data['shininess'] = max(0.0, (1.0 - roughness * roughness) * 128.0)
                    data['alpha'] = node.inputs['Alpha'].default_value
                    break
            # Optional: emission from Emission node (if wired)
            for node in mat.node_tree.nodes:
                if node.type == 'EMISSION':
                    er, eg, eb, _ = node.inputs['Color'].default_value
                    strength = node.inputs['Strength'].default_value
                    data['emissive'] = (
                        min(1.0, er * strength),
                        min(1.0, eg * strength),
                        min(1.0, eb * strength),
                    )
                    break
        return data

    # ── Helper: pack RGBA floats to ABGR uint32 ───────────────────────────

    @staticmethod
    def _pack_abgr(r, g, b, a):
        """Pack float RGBA (0..1) to uint32 in ABGR byte order (FLT packedColor)."""
        ri = max(0, min(255, round(r * 255)))
        gi = max(0, min(255, round(g * 255)))
        bi = max(0, min(255, round(b * 255)))
        ai = max(0, min(255, round(a * 255)))
        return (ai << 24) | (bi << 16) | (gi << 8) | ri

    # ── Write: FLT Header (op=1, 316 bytes) ───────────────────────────────

    def _write_flt_header(self, w):
        w.rec(OP_HEADER, 316)
        w.string('db', 8)          # database origin ID
        w.uint(1600)               # formatRevision = v16.0
        w.uint(0)                  # editRevision
        w.string('', 32)           # dateTime
        w.ushort(1)                # nextGroupNodeID
        w.ushort(1)                # nextLODNodeID
        w.ushort(1)                # nextObjectNodeID
        w.ushort(1)                # nextFaceNodeID
        w.ushort(1)                # unitMultiplier
        w.uchar(0)                 # coordUnits = meters
        # pad to 312 data bytes (316 total - 4 header)
        # written so far: 8+4+4+32+2+2+2+2+2+1 = 59 bytes
        w.zeros(312 - 59)

    # ── Write: Color Palette (op=32, 4228 bytes) ──────────────────────────

    def _write_color_palette(self, w):
        w.rec(OP_COLOR_PALETTE, 4228)
        w.zeros(128)               # reserved
        # 1024 entries x 4 bytes = 4096 bytes; white = 0xFFFFFFFF (ABGR)
        white = struct.pack('>I', 0xFFFFFFFF)
        for _ in range(1024):
            w.raw(white)

    # ── Write: Material Palette (op=113, 84 bytes each) ───────────────────

    def _write_material_palette(self, w):
        """Write one OP_MATERIAL (op=113) record per collected Blender material.

        Record layout (84 bytes total):
          4  header (op + length)
          4  materialIndex  (uint32)
         12  name           (char[12])
          4  flags          (uint32, bit31 = in use)
         12  ambient        (3 x float32)
         12  diffuse        (3 x float32)
         12  specular       (3 x float32)
         12  emissive       (3 x float32)
          4  shininess      (float32)
          4  alpha          (float32)
          4  spare          (zeros)
        """
        for idx, mat_data in enumerate(self.mat_list):
            w.rec(OP_MATERIAL, 84)
            w.uint(idx)
            w.string(mat_data['name'], 12)
            w.uint(0x80000000)             # flags: bit 31 = in use
            ar, ag, ab = mat_data['ambient']
            w.float_(ar); w.float_(ag); w.float_(ab)
            dr, dg, db = mat_data['diffuse']
            w.float_(dr); w.float_(dg); w.float_(db)
            sr, sg, sb = mat_data['specular']
            w.float_(sr); w.float_(sg); w.float_(sb)
            er, eg, eb = mat_data['emissive']
            w.float_(er); w.float_(eg); w.float_(eb)
            w.float_(mat_data['shininess'])
            w.float_(mat_data['alpha'])
            w.zeros(4)                     # spare

    # ── Write: Light Point Appearance Palette (op=128) ────────────────────

    def _write_lp_app_palette(self, w):
        """Write Light Point Appearance Palette records (op=128).

        _lp_palette_raw_list is built by _collect_lp_palette():
        - Round-trip: raw bytes from scene['flt_lp_app_palette'] (set during import).
        - Synthesis:  records synthesized from Blender LIGHT objects.
        """
        for raw in self._lp_palette_raw_list:
            w.raw(raw)

    # ── Write: Texture Palette (op=64, 216 bytes each) ────────────────────

    def _write_texture_palette(self, w):
        flt_dir = os.path.dirname(os.path.abspath(self.filepath))
        for idx, path in enumerate(self.tex_paths):
            # Write a path relative to the output FLT file so that
            # re-importing from any location can still resolve textures in
            # subdirectories (e.g. ./Textures/name.rgb).
            # Fall back to basename-only if relpath crosses drive roots (Windows).
            try:
                rel = os.path.relpath(path, flt_dir).replace(os.sep, '/')
                if not rel.startswith('.') and not rel.startswith('/'):
                    rel = './' + rel
            except ValueError:
                rel = os.path.basename(path)
            w.rec(OP_TEXTURE_PALETTE, 216)
            w.string(rel, 200)
            w.int_(idx)
            w.zeros(8)             # reserved x,y location

    # ── Write: Vertex Palette ─────────────────────────────────────────────

    def _write_vertex_palette(self, w):
        n = len(self.vp)
        total_vp = VP_HEADER_SIZE + n * VERT_RECORD_SIZE
        w.rec(OP_VERTEX_PALETTE, 8)
        w.uint(total_vp)

        for (x, y, z, nx, ny, nz, u, v) in self.vp:
            w.rec(OP_VERTEX_CNUV, VERT_RECORD_SIZE)
            w.ushort(0)                 # colorNameIndex
            w.ushort(VERT_NO_COLOR)     # flags: 0x2000 = no per-vertex color
            w.double(x)
            w.double(y)
            w.double(z)
            w.float_(nx)
            w.float_(ny)
            w.float_(nz)
            w.float_(u)
            w.float_(v)
            w.uint(0xFFFFFFFF)          # packedColor (ignored when VERT_NO_COLOR)
            w.uint(127)                 # colorIndex
            w.zeros(4)                  # spare (required for length=64)

    # ── Write: Scene hierarchy ─────────────────────────────────────────────

    def _write_scene(self, w):
        """Write all top-level objects (those without a parent in the export set)."""
        top_level = [o for o in self.objects if o.parent is None or
                     o.parent not in self.objects]
        for obj in top_level:
            self._write_node(w, obj)

    def _write_node(self, w, obj):
        """Recursively write one object and its children."""
        children = [o for o in self.objects if o.parent == obj]

        if obj.type == 'MESH':
            self._write_object_node(w, obj, children)
        elif obj.type == 'LIGHT' or obj.get('flt_type') == 'LIGHTPOINT':
            self._write_light_point_node(w, obj, children)
        else:
            self._write_group_node(w, obj, children)

    def _write_group_node(self, w, obj, children):
        name = obj.name[:8]

        # Group record (op=2, length=44)
        # Data layout: name(8)+priority(2)+reserved(2)+flags(4)+
        #              sID1(2)+sID2(2)+significance(2)+
        #              layerCode(1)+reserved(1)+reserved(4)+
        #              loopCount(4)+loopDur(4)+lastFrameDur(4) = 40 bytes
        w.rec(OP_GROUP, 44)
        w.string(name, 8)
        w.short(0)     # relativePriority
        w.ushort(0)    # reserved
        w.uint(0)      # flags
        w.short(0)     # specialID1
        w.short(0)     # specialID2
        w.short(0)     # significance
        w.uchar(0)     # layerCode
        w.uchar(0)     # reserved
        w.uint(0)      # reserved
        w.uint(0)      # loopCount
        w.float_(0.0)  # loopDuration
        w.float_(0.0)  # lastFrameDuration

        # Always write LongID — FLT convention: every node gets a LongID
        # so that the full name is preserved on round-trip regardless of length.
        self._write_long_id(w, obj.name)

        # Write the light's 3D position and any children inside a PUSH/POP block.
        # FLT importer expects: IDX_LP → PUSH → VERT_LIST [1 offset] → POP
        vi = self._lp_vertex_map.get(obj.name)
        if vi is not None or children:
            w.rec(OP_PUSH, 4)
            if vi is not None:
                # VERT_LIST: header(4) + 1 × int32 byte-offset(4) = 8 bytes total
                w.rec(OP_VERTEX_LIST, 8)
                w.int_(vert_byte_offset(vi))
            for child in children:
                self._write_node(w, child)
            w.rec(OP_POP, 4)

    def _write_object_node(self, w, obj, children):
        name = obj.name[:8]

        # Object record (op=4, length=28)
        w.rec(OP_OBJECT, 28)
        w.string(name, 8)
        w.uint(0)      # flags
        w.short(0)     # relativePriority
        w.ushort(0)    # transparency
        w.short(0)     # specialEffectID1
        w.short(0)     # specialEffectID2
        w.short(0)     # significance
        w.ushort(0)    # reserved

        # Always write LongID — FLT convention: every node gets a LongID
        self._write_long_id(w, obj.name)

        w.rec(OP_PUSH, 4)
        self._write_mesh_faces(w, obj)
        if children:
            for child in children:
                self._write_node(w, child)
        w.rec(OP_POP, 4)

    def _write_mesh_faces(self, w, obj):
        """Write Face + PUSH + VertexList + POP for each polygon of a mesh object."""
        depsgraph = bpy.context.evaluated_depsgraph_get()
        obj_eval  = obj.evaluated_get(depsgraph)
        mesh      = obj_eval.to_mesh()
        if mesh is None:
            return

        uv_layer = mesh.uv_layers.active
        s = 1.0 / self.scale

        mx   = obj.matrix_world
        mx_n = mx.to_3x3().inverted_safe().transposed()

        for poly in mesh.polygons:
            # ── Resolve material, texture and face color ──────────────────
            tex_idx = -1
            mat_idx = -1
            r, g, b, a = 1.0, 1.0, 1.0, 1.0

            if poly.material_index < len(obj.data.materials):
                mat = obj.data.materials[poly.material_index]
                if mat is not None:
                    mat_idx = self._mat_index_map.get(mat.name, -1)
                    if mat.use_nodes:
                        for node in mat.node_tree.nodes:
                            if node.type == 'BSDF_PRINCIPLED':
                                r, g, b, _ = node.inputs['Base Color'].default_value
                                a = node.inputs['Alpha'].default_value
                                break
                        for node in mat.node_tree.nodes:
                            if node.type == 'TEX_IMAGE' and node.image:
                                path = bpy.path.abspath(node.image.filepath)
                                tex_idx = self._tex_map.get(path, -1)
                                break

            # ── Collect vertex palette indices ────────────────────────────
            vp_indices = []
            for li in poly.loop_indices:
                loop = mesh.loops[li]
                vi   = loop.vertex_index
                v    = mesh.vertices[vi]
                co   = mx @ v.co
                nor  = (mx_n @ v.normal).normalized()
                uv   = uv_layer.data[li].uv if uv_layer else mathutils.Vector((0.0, 0.0))
                key  = (
                    round(co.x * s, 6), round(co.y * s, 6), round(co.z * s, 6),
                    round(nor.x, 5),    round(nor.y, 5),    round(nor.z, 5),
                    round(uv.x, 5),     round(uv.y, 5),
                )
                vp_indices.append(self._vp_map.get(key, 0))

            n_verts = len(vp_indices)
            if n_verts < 3:
                continue

            # ── Write: Face → PUSH → VertexList → POP ────────────────────
            self._write_face(w, tex_idx, mat_idx, r, g, b, a)

            w.rec(OP_PUSH, 4)

            vl_len = 4 + n_verts * 4
            w.rec(OP_VERTEX_LIST, vl_len)
            for vi in vp_indices:
                w.int_(vert_byte_offset(vi))

            w.rec(OP_POP, 4)

        obj_eval.to_mesh_clear()

    def _write_face(self, w, tex_idx, mat_idx, r, g, b, a):
        """Write a Face record (op=5, length=76).

        Color encoding:
          FACE_PACKEDCOLOR (0x10000000) - use packedColor field (ABGR uint32).
          This preserves the face/material base color on reimport even when
          there is no material palette entry on the receiving end.
          mat_idx also written so that a matching material palette entry
          provides full BSDF data (diffuse, shininess, etc.) on reimport.

        Transparency encoding:
          transparency = round((1 - alpha) * 65535)
          Parser reverses this: alpha = 1 - transparency / 65535
        """
        transparency = max(0, min(65535, round((1.0 - a) * 65535)))
        packed = self._pack_abgr(r, g, b, a)

        w.rec(OP_FACE, 76)
        w.string('', 8)             # face ID
        w.int_(0)                   # irColorCode
        w.short(0)                  # relativePriority
        w.char(0)                   # drawType = solid
        w.uchar(0)                  # textureWhite
        w.ushort(127)               # colorNameIndex = white
        w.ushort(127)               # altColorNameIndex
        w.uchar(0)                  # reserved0
        w.uchar(0)                  # billboardFlags
        w.short(-1)                 # detailTextureIndex
        w.short(tex_idx)            # textureIndex
        w.short(mat_idx)            # materialIndex (-1 = none)
        w.ushort(0)                 # surfaceMaterialCode
        w.ushort(0)                 # featureID
        w.uint(0)                   # irMaterialCode
        w.ushort(transparency)      # transparency
        w.uchar(0)                  # LODGenerationControl
        w.uchar(0)                  # lineStyleIndex
        w.uint(FACE_PACKEDCOLOR | FACE_NO_ALT_COLOR)  # miscFlags
        w.uchar(0)                  # lightMode
        w.uchar(0)                  # reserved1
        w.ushort(0)                 # reserved2
        w.uint(0)                   # reserved3
        w.uint(packed)              # packedColor (ABGR)
        w.uint(packed)              # packedColorAlternate
        w.short(-1)                 # textureMappingIndex
        w.ushort(0)                 # reserved4
        w.uint(127)                 # primaryColorIndex
        w.uint(127)                 # alternateColorIndex

    def _write_light_point_node(self, w, obj, children):
        """Write an Indexed Light Point record (op=130, 28 bytes).

        Record layout:
          4  header (op=130 + length=28)
          8  name
          2  appIdx    (short)  — appearance palette index
          2  animIdx   (short)  — animation palette index
          2  drawOrder (short)  — -1 = default
          2  flags     (ushort) — 0xFFFF = all flags set (standard default)
          8  reserved  (zeros)
        """
        name       = obj.name[:8]
        # For imported lights: flt_lp_app_idx is stored as a custom property.
        # For native Blender lights: we synthesized a palette entry and stored
        # its index in _lp_appidx_map during _collect_lp_palette().
        if obj.get('flt_lp_app_idx') is not None:
            app_idx = int(obj['flt_lp_app_idx'])
        else:
            app_idx = self._lp_appidx_map.get(obj.name, 0)
        anim_idx   = int(obj.get('flt_lp_anim_idx',   0))
        draw_order = int(obj.get('flt_lp_draw_order', -1))
        lp_flags   = int(obj.get('flt_lp_flags',   0xFFFF))

        w.rec(OP_INDEXED_LP, 28)
        w.string(name, 8)
        w.short(app_idx)
        w.short(anim_idx)
        w.short(draw_order)
        w.ushort(lp_flags & 0xFFFF)
        w.zeros(8)   # reserved

        self._write_long_id(w, obj.name)

        # Write the light's 3D position and any children inside a PUSH/POP block.
        # FLT importer expects: IDX_LP → PUSH → VERT_LIST [1 offset] → POP
        vi = self._lp_vertex_map.get(obj.name)
        if vi is not None or children:
            w.rec(OP_PUSH, 4)
            if vi is not None:
                # VERT_LIST: header(4) + 1 × int32 byte-offset(4) = 8 bytes total
                w.rec(OP_VERTEX_LIST, 8)
                w.int_(vert_byte_offset(vi))
            for child in children:
                self._write_node(w, child)
            w.rec(OP_POP, 4)

    def _write_long_id(self, w, name):
        """Write a LongID record (op=33) for names longer than 8 chars."""
        encoded = name.encode('latin-1', errors='replace') + b'\x00'
        length  = 4 + len(encoded)
        w.rec(OP_LONG_ID, length)
        w.raw(encoded)
