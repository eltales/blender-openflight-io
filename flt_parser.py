"""
FLT Parser - reads a binary FLT file into pure Python data structures.
No bpy dependency; all data classes are plain Python objects.
"""

import os
import struct

from .flt_reader import (
    FltReader,
    OP_HEADER, OP_GROUP, OP_OBJECT, OP_FACE,
    OP_PUSH, OP_POP, OP_PUSH_EXTENSION, OP_POP_EXTENSION,
    OP_COMMENT, OP_COLOR_PALETTE, OP_LONG_ID, OP_MATRIX,
    OP_EXTERNAL_REF, OP_TEXTURE_PALETTE, OP_MATERIAL_TABLE, OP_MATERIAL,
    OP_VERTEX_PALETTE, OP_VERTEX_C, OP_VERTEX_CN, OP_VERTEX_CNUV, OP_VERTEX_CUV,
    OP_VERTEX_LIST, OP_LOD, OP_SWITCH, OP_DOF,
    OP_MESH, OP_LOCAL_VERTEX_POOL, OP_MESH_PRIMITIVE,
    OP_ROAD_SEGMENT, OP_ROAD_PATH, OP_ROAD_CONST,
    OP_LIGHT_SOURCE, OP_LIGHT_POINT, OP_INDEXED_LP, OP_LIGHTPT_SYSTEM,
    OP_REPLICATE, OP_INSTANCE_DEF, OP_INSTANCE_REF,
    OP_GENERAL_MATRIX, OP_NONUNIFORM_SCALE, OP_TRANSLATE, OP_ROTATE_PT,
    OP_LINE_STYLE_PAL, OP_TEXTURE_MAP_PAL, OP_SHADER_PAL, OP_NAME_TABLE,
    OP_MULTITEXTURE, OP_UV_LIST,
    OP_LIGHTPT_APP_PAL, OP_LIGHTPT_ANIM_PAL, OP_CLIP_REGION,
    MESHPRIM_TRISTRIP, MESHPRIM_TRIFAN, MESHPRIM_QUADSTRIP, MESHPRIM_POLYINDEX,
    LVPATTR_POSITION, LVPATTR_COLORINDEX, LVPATTR_PACKEDCOLOR,
    LVPATTR_NORMAL, LVPATTR_UV0,
    FACE_NOCOLOR, FACE_PACKEDCOLOR,
    VERT_NO_COLOR, VERT_PACKED_COLOR,
)

# ── FLT version helpers ───────────────────────────────────────────────────────
def _is_v13x(rev): return rev < 1420
def _is_v14x(rev): return 1410 < rev < 1541
def _is_v15x(rev): return 1540 < rev < 1600
def _is_v16x(rev): return 1599 < rev < 1700


# ── Data classes ──────────────────────────────────────────────────────────────

class FltVertex:
    """A single vertex from the vertex palette or a local vertex pool."""
    __slots__ = ('x', 'y', 'z', 'nx', 'ny', 'nz', 'u', 'v',
                 'r', 'g', 'b', 'a', 'byte_offset', 'flags')

    def __init__(self):
        self.x = self.y = self.z = 0.0
        self.nx = self.ny = 0.0
        self.nz = 1.0            # default normal points up
        self.u = self.v = 0.0
        self.r = self.g = self.b = self.a = 1.0
        self.byte_offset = 0    # byte offset from start of vertex palette record
        self.flags = 0


class FltFaceData:
    """Geometry and material data for a single face (polygon)."""
    __slots__ = ('vertex_indices', 'uv_list', 'tex_index', 'mat_index',
                 'color', 'alpha', 'double_sided')

    def __init__(self):
        self.vertex_indices = []     # list[int] - indices into vertex palette
        self.uv_list = []            # list[(u, v)] - one UV per vertex
        self.tex_index = -1          # -1 = no texture
        self.mat_index = -1          # -1 = no material
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.alpha = 1.0
        # DrawType=1 (DrawSolidNoBackfaceCulling) → double-sided geometry.
        # All other draw types render with backface culling (single-sided).
        self.double_sided = False


class FltMeshPrimData:
    """One primitive from a Mesh record (triangle strip, fan, etc.)."""
    __slots__ = ('prim_type', 'indices')

    def __init__(self):
        self.prim_type = MESHPRIM_POLYINDEX
        self.indices = []


class FltNode:
    """Base scene graph node."""

    def __init__(self, name=''):
        self.name = name
        self.long_name = ''       # from LongID record (op=33)
        self.comment = ''         # from Comment record (op=31)
        self.matrix = None        # 4x4 row-major list or None
        self.children = []
        self.node_type = 'UNKNOWN'


class FltGroup(FltNode):
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type = 'GROUP'
        self.priority = 0
        self.flags = 0


class FltObject(FltNode):
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type = 'OBJECT'
        self.flags = 0
        self.faces = []       # list[FltFaceData]


class FltLOD(FltNode):
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type = 'LOD'
        self.switch_in = 0.0
        self.switch_out = 0.0
        self.center = (0.0, 0.0, 0.0)
        self.flags = 0


class FltSwitch(FltNode):
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type = 'SWITCH'
        self.current_mask = 0
        self.num_masks = 0
        self.num_u32_per_mask = 0
        self.masks = []


class FltDOF(FltNode):
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type = 'DOF'
        self.limits = {}      # dict with 'origin' etc.


class FltExtRef(FltNode):
    def __init__(self, name='', path=''):
        super().__init__(name)
        self.node_type = 'EXTREF'
        self.path = path
        self.flags = 0


class FltMeshNode(FltNode):
    """Mesh node (op=84) with local vertex pool and mesh primitives."""
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type = 'MESH'
        self.tex_index = -1
        self.mat_index = -1
        self.alpha = 1.0
        self.color = (1.0, 1.0, 1.0, 1.0)
        self.double_sided = False    # DrawType=1 → no backface culling
        self.lvp_verts = []      # list[FltVertex] from LocalVertexPool
        self.primitives = []     # list[FltMeshPrimData]


class FltLightPoint(FltNode):
    def __init__(self, name=''):
        super().__init__(name)
        self.node_type  = 'LIGHTPOINT'
        self.app_idx    = 0       # appearance palette index (op=128)
        self.anim_idx   = 0       # animation palette index  (op=129)
        self.draw_order = -1      # -1 = default
        self.lp_flags   = 0xFFFF  # flags (0xFFFF = all bits set, common default)
        self.position   = None    # (x, y, z) from vertex list — the 3D location of the light


class FltUnhandled(FltNode):
    """Placeholder for unhandled node types - preserves hierarchy."""
    def __init__(self, name='', opcode=0):
        super().__init__(name)
        self.node_type = 'UNHANDLED'
        self.opcode = opcode


# ── FLT Database ──────────────────────────────────────────────────────────────

class FltDatabase:
    """
    Parses a binary FLT file into a tree of FltNode objects.
    Call parse() after construction.
    """

    def __init__(self, filepath):
        self.filepath = filepath
        self.version = 1600             # default: v16
        self.coord_units = 0            # 0 = meters
        self.color_palette = []         # 1024 x (r,g,b,a) float tuples
        self.vert_palette = []          # list[FltVertex]
        self.vert_offset_map = {}       # dict {byte_offset: palette_index}
        self.tex_palette = {}           # dict {int: str path}
        self.mat_palette = {}           # dict {int: dict}
        self.children = []              # top-level scene nodes
        self.search_dirs = []
        self.lp_app_palette_raw  = []   # list of (length, payload_bytes) for op=128 round-trip
        self.lp_app_palette_list = []   # list of parsed dicts for Blender light import

        basedir = os.path.dirname(os.path.abspath(filepath))
        if basedir:
            self.search_dirs.append(basedir)

    # ── File search ───────────────────────────────────────────────────────────

    def add_search_dir(self, dirpath):
        if dirpath and dirpath not in self.search_dirs:
            self.search_dirs.append(dirpath)

    def find_file(self, ref_path):
        """Search for a file across all known search directories.
        Returns the full resolved path or None if not found."""
        if not ref_path:
            return None
        ref_path = ref_path.replace('\\', os.sep).replace('/', os.sep)
        # Try path as-is
        if os.path.isfile(ref_path):
            return ref_path
        basename = os.path.basename(ref_path)
        for d in self.search_dirs:
            candidate = os.path.join(d, ref_path)
            if os.path.isfile(candidate):
                return candidate
            candidate = os.path.join(d, basename)
            if os.path.isfile(candidate):
                return candidate
        return None

    # ── Color palette helpers ─────────────────────────────────────────────────

    def lookup_color(self, color_index):
        """Resolve a color index into an (r,g,b,a) float tuple.
        Color index encodes: actual_idx * 128 + intensity_step."""
        if not self.color_palette:
            return (1.0, 1.0, 1.0, 1.0)
        actual_idx = color_index // 128
        intensity = (color_index - 128 * actual_idx) / 127.0
        if actual_idx < 0 or actual_idx >= len(self.color_palette):
            return (1.0, 1.0, 1.0, 1.0)
        br, bg, bb, ba = self.color_palette[actual_idx]
        return (br * intensity, bg * intensity, bb * intensity, ba)

    @staticmethod
    def unpack_abgr(packed):
        """Unpack a uint32 packed color in ABGR format to (r,g,b,a) floats."""
        r = (packed >> 0) & 0xff
        g = (packed >> 8) & 0xff
        b = (packed >> 16) & 0xff
        a = (packed >> 24) & 0xff
        return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)

    # ── Main parser ───────────────────────────────────────────────────────────

    def parse(self):
        reader = FltReader(self.filepath)
        try:
            self._parse_records(reader)
        finally:
            reader.close()

    def _parse_records(self, reader):
        """
        Main parsing loop. Builds a tree of FltNodes using a parent stack.
        PUSH (op=10) descends into children of the last added node.
        POP  (op=11) returns to the previous parent.

        Special handling for IDX_LP (op=130):
        In FLT the structure is IDX_LP → (op=124) → LongID → PUSH → VERT_LIST → POP.
        The PUSH/POP block is NOT a real children scope — it only carries the 3D
        position (VERT_LIST).  To avoid the LightPoint node swallowing its
        siblings as children, we do NOT set current_parent = LP node.
        Instead we track the last-added LP in pending_lp_node and use it only for
        LongID, Matrix, and VERT_LIST records that immediately follow.
        """
        parent_stack = []       # stack of parent FltNode references
        current_parent = None   # active parent (None = top level)
        ignore_ext = 0          # depth counter for push/pop extension blocks

        # Byte offset of the vertex palette record (used to compute relative offsets)
        vp_record_pos = None

        # Active mesh node (for LocalVertexPool and MeshPrimitive records)
        current_mesh = None

        # Last-added IDX_LP / OP_LIGHT_POINT node waiting for its VERT_LIST.
        # Cleared when any new scene-graph node is encountered.
        pending_lp_node = None

        def add_child(node):
            if current_parent is not None:
                current_parent.children.append(node)
            else:
                self.children.append(node)

        while reader.begin_record():
            op = reader.opcode

            # Skip everything inside push/pop extension blocks
            if ignore_ext > 0:
                if op == OP_POP_EXTENSION:
                    reader.skip(18)
                    reader.read_ushort()
                    ignore_ext -= 1
                elif op == OP_PUSH_EXTENSION:
                    reader.skip(18)
                    reader.read_ushort()
                    ignore_ext += 1
                continue

            # ── Hierarchy control ─────────────────────────────────────────
            if op == OP_PUSH:
                parent_stack.append(current_parent)
                # current_parent stays as-is; children of the last added
                # node will be set after the next hierarchy node is added
                continue

            if op == OP_POP:
                if parent_stack:
                    current_parent = parent_stack.pop()
                current_mesh = None
                continue

            if op == OP_PUSH_EXTENSION:
                reader.skip(18)
                reader.read_ushort()
                ignore_ext += 1
                continue

            if op == OP_POP_EXTENSION:
                reader.skip(18)
                reader.read_ushort()
                if ignore_ext > 0:
                    ignore_ext -= 1
                continue

            # ── Node attribute records (applied to current_parent) ────────
            if op == OP_LONG_ID:
                name = reader.read_string(reader.length - 4)
                # Apply to pending LP node first (it does not own current_parent)
                target = pending_lp_node if pending_lp_node is not None else current_parent
                if target is not None:
                    target.long_name = name
                    if not target.name:
                        target.name = name
                continue

            if op == OP_COMMENT:
                comment = reader.read_string(reader.length - 4)
                target = pending_lp_node if pending_lp_node is not None else current_parent
                if target is not None:
                    target.comment = comment
                continue

            if op == OP_MATRIX:
                mat = reader.read_matrix4x4()
                target = pending_lp_node if pending_lp_node is not None else current_parent
                if target is not None:
                    target.matrix = mat
                continue

            # ── Palette records ───────────────────────────────────────────
            if op == OP_HEADER:
                self._parse_header(reader)
                continue

            if op == OP_COLOR_PALETTE:
                self._parse_color_palette(reader)
                continue

            if op == OP_TEXTURE_PALETTE:
                self._parse_texture_palette(reader)
                continue

            if op == OP_MATERIAL:
                self._parse_material(reader)
                continue

            if op == OP_MATERIAL_TABLE:
                self._parse_material_table(reader)
                continue

            if op == OP_VERTEX_PALETTE:
                reader.read_uint()          # total byte length of the VP record
                vp_record_pos = reader._pos  # absolute file position of VP record start
                continue

            if op in (OP_VERTEX_C, OP_VERTEX_CN, OP_VERTEX_CUV, OP_VERTEX_CNUV):
                self._parse_vertex(reader, op, vp_record_pos)
                continue

            # ── Scene graph nodes ─────────────────────────────────────────
            if op == OP_GROUP:
                pending_lp_node = None
                node = self._parse_group(reader)
                add_child(node)
                current_parent = node
                continue

            if op == OP_OBJECT:
                pending_lp_node = None
                node = self._parse_object_node(reader)
                add_child(node)
                current_parent = node
                continue

            if op == OP_LOD:
                pending_lp_node = None
                node = self._parse_lod(reader)
                add_child(node)
                current_parent = node
                continue

            if op == OP_SWITCH:
                pending_lp_node = None
                node = self._parse_switch(reader)
                add_child(node)
                current_parent = node
                continue

            if op == OP_DOF:
                pending_lp_node = None
                node = self._parse_dof(reader)
                add_child(node)
                current_parent = node
                continue

            if op == OP_EXTERNAL_REF:
                pending_lp_node = None
                node = self._parse_external_ref(reader)
                add_child(node)
                current_parent = node
                continue

            if op == OP_MESH:
                pending_lp_node = None
                node = self._parse_mesh_node(reader)
                add_child(node)
                current_parent = node
                current_mesh = node
                continue

            if op == OP_LOCAL_VERTEX_POOL:
                if current_mesh:
                    self._parse_local_vertex_pool(reader, current_mesh)
                continue

            if op == OP_MESH_PRIMITIVE:
                if current_mesh:
                    self._parse_mesh_primitive(reader, current_mesh)
                continue

            if op == OP_FACE:
                face = self._parse_face(reader)
                obj = current_parent
                if obj is not None:
                    if isinstance(obj, FltObject):
                        obj.faces.append(face)
                    elif isinstance(obj, FltGroup):
                        if not hasattr(obj, '_implicit_faces'):
                            obj._implicit_faces = []
                        obj._implicit_faces.append(face)
                continue

            if op == OP_VERTEX_LIST:
                self._parse_vertex_list(reader, current_parent, pending_lp_node)
                pending_lp_node = None   # consumed
                continue

            if op in (OP_LIGHT_POINT, OP_INDEXED_LP, OP_LIGHTPT_SYSTEM):
                name = reader.read_string(8)
                node = FltLightPoint(name)
                if op == OP_INDEXED_LP and reader.length >= 28:
                    node.app_idx    = reader.read_short()
                    node.anim_idx   = reader.read_short()
                    node.draw_order = reader.read_short()
                    node.lp_flags   = reader.read_ushort()
                add_child(node)
                # Do NOT change current_parent — IDX_LP's PUSH/POP block only
                # carries the VERT_LIST position, not real scene-graph children.
                # Setting current_parent = LP would cause subsequent sibling
                # nodes (groups etc.) to be incorrectly parented under the LP.
                pending_lp_node = node
                continue

            if op == OP_LIGHTPT_APP_PAL:
                payload_len = reader.length - 4
                raw_payload = reader.file.read(payload_len) if payload_len > 0 else b''
                self.lp_app_palette_raw.append((reader.length, raw_payload))
                self._parse_lp_app_palette_entry(raw_payload)
                continue

            # All other records are silently ignored but hierarchy is preserved
            # via PUSH/POP records that will follow

        # After parsing all records: build the offset-to-index lookup map
        for idx, v in enumerate(self.vert_palette):
            self.vert_offset_map[v.byte_offset] = idx

    # ── Individual record parsers ─────────────────────────────────────────────

    def _parse_header(self, reader):
        reader.read_string(8)                    # database ID string
        self.version = reader.read_uint()        # formatRevision
        reader.read_uint()                       # editRevision
        reader.read_string(32)                   # dateTime string
        for _ in range(4):
            reader.read_ushort()                 # nextGroup/LOD/Object/Face NodeID
        reader.read_ushort()                     # unitMultiplier
        self.coord_units = reader.read_uchar()   # coordUnits (0=meters, 1=km, ...)
        # Remaining header fields are not needed for basic import

    def _parse_color_palette(self, reader):
        reader.skip(128)                 # reserved padding
        self.color_palette = []
        for _ in range(1024):
            # Colors are stored as uint32 in ABGR format
            packed = reader.read_uint()
            r = (packed >> 0) & 0xff
            g = (packed >> 8) & 0xff
            b = (packed >> 16) & 0xff
            a = (packed >> 24) & 0xff
            self.color_palette.append((r / 255.0, g / 255.0, b / 255.0, a / 255.0))

    def _parse_texture_palette(self, reader):
        name = reader.read_string(200)
        index = reader.read_int()
        self.tex_palette[index] = name

    def _parse_material(self, reader):
        index = reader.read_uint()
        name_raw = reader.read_string(12)
        flags = reader.read_uint()
        # Bit 31 = material is in use; skip unused slots
        if not (flags & 0x80000000):
            return
        mat = {
            'name':     name_raw,
            'ambient':  (reader.read_float(), reader.read_float(), reader.read_float()),
            'diffuse':  (reader.read_float(), reader.read_float(), reader.read_float()),
            'specular': (reader.read_float(), reader.read_float(), reader.read_float()),
            'emissive': (reader.read_float(), reader.read_float(), reader.read_float()),
            'shininess': reader.read_float(),
            'alpha':     reader.read_float(),
        }
        self.mat_palette[index] = mat

    def _parse_material_table(self, reader):
        """Parse the old (obsolete) material table (op=66) with 64 fixed slots."""
        for i in range(64):
            mat = {
                'name':     '',
                'ambient':  (reader.read_float(), reader.read_float(), reader.read_float()),
                'diffuse':  (reader.read_float(), reader.read_float(), reader.read_float()),
                'specular': (reader.read_float(), reader.read_float(), reader.read_float()),
                'emissive': (reader.read_float(), reader.read_float(), reader.read_float()),
                'shininess': reader.read_float(),
                'alpha':     reader.read_float(),
            }
            flags = reader.read_uint()
            mat['name'] = reader.read_string(12)
            reader.skip(28 * 4)   # spare[28]
            if flags & 0x80000000:
                self.mat_palette[i] = mat

    def _parse_vertex(self, reader, op, vp_record_pos):
        """Parse one vertex record and append it to the vertex palette."""
        v = FltVertex()
        # byte_offset = position of this record relative to the VP record start
        if vp_record_pos is not None:
            v.byte_offset = reader._pos - vp_record_pos
        else:
            v.byte_offset = reader._pos

        reader.read_ushort()    # colorNameIndex (used only for v13/v14 color lookup)
        flags = reader.read_ushort()
        v.flags = flags

        v.x = reader.read_double()
        v.y = reader.read_double()
        v.z = reader.read_double()

        if op in (OP_VERTEX_CN, OP_VERTEX_CNUV):
            v.nx = reader.read_float()
            v.ny = reader.read_float()
            v.nz = reader.read_float()

        if op in (OP_VERTEX_CUV, OP_VERTEX_CNUV):
            v.u = reader.read_float()
            v.v = reader.read_float()

        packed = reader.read_uint()   # packedColor (ABGR)

        if _is_v14x(self.version) or _is_v13x(self.version):
            color_idx = 0   # old files: use colorNameIndex (already read above)
        else:
            color_idx = reader.read_uint()

        # Resolve vertex color
        if flags & VERT_NO_COLOR:
            v.r, v.g, v.b, v.a = 1.0, 1.0, 1.0, 1.0
        elif flags & VERT_PACKED_COLOR:
            v.r, v.g, v.b, v.a = self.unpack_abgr(packed)
        else:
            v.r, v.g, v.b, v.a = self.lookup_color(color_idx)

        self.vert_palette.append(v)
        # Keep the map up to date so VertexList records (which follow later in
        # the file) can look up offsets immediately during the same parse pass.
        self.vert_offset_map[v.byte_offset] = len(self.vert_palette) - 1

    def _parse_group(self, reader):
        name = reader.read_string(8)
        node = FltGroup(name)
        node.priority = reader.read_short()
        reader.read_ushort()             # reserved
        node.flags = reader.read_uint()
        reader.skip(2 + 2 + 2 + 1 + 1 + 4)  # specialID1, specialID2, significance, layerCode, reserved, reserved
        return node

    def _parse_object_node(self, reader):
        name = reader.read_string(8)
        node = FltObject(name)
        node.flags = reader.read_uint()
        reader.skip(2 + 2 + 2 + 2 + 2 + 2)  # priority, transparency, specialID1/2, significance, reserved
        return node

    def _parse_lod(self, reader):
        name = reader.read_string(8)
        node = FltLOD(name)
        reader.read_uint()                    # reserved
        node.switch_in = reader.read_double()
        node.switch_out = reader.read_double()
        reader.read_ushort()                  # specialEffectID1
        reader.read_ushort()                  # specialEffectID2
        node.flags = reader.read_uint()
        cx = reader.read_double()
        cy = reader.read_double()
        cz = reader.read_double()
        node.center = (cx, cy, cz)
        return node

    def _parse_switch(self, reader):
        name = reader.read_string(8)
        node = FltSwitch(name)
        reader.read_uint()                    # reserved
        node.current_mask = reader.read_uint()
        # Note: doc says numUInt32sPerMask first, but actual files have numMasks first
        node.num_masks = reader.read_uint()
        node.num_u32_per_mask = reader.read_uint()
        total = node.num_masks * node.num_u32_per_mask
        node.masks = [reader.read_uint() for _ in range(total)]
        return node

    def _parse_dof(self, reader):
        name = reader.read_string(8)
        node = FltDOF(name)
        reader.read_uint()                    # reserved
        ox = reader.read_double()
        oy = reader.read_double()
        oz = reader.read_double()
        node.limits['origin'] = (ox, oy, oz)
        return node

    def _parse_external_ref(self, reader):
        path = reader.read_string(200)
        reader.read_uchar()                   # reserved0
        reader.read_uchar()                   # reserved1
        reader.read_ushort()                  # reserved2
        flags = reader.read_uint()

        clean_path = path.replace('\x00', '').strip()
        basename = os.path.splitext(os.path.basename(clean_path))[0]
        node = FltExtRef(basename, clean_path)
        node.flags = flags
        return node

    def _parse_mesh_node(self, reader):
        name = reader.read_string(8)
        node = FltMeshNode(name)
        reader.read_int()       # irColorCode
        reader.read_short()     # relativePriority
        draw_type = reader.read_char()   # drawType
        node.double_sided = (draw_type == 1)
        reader.read_uchar()     # textureWhite
        reader.read_ushort()    # colorNameIndex
        reader.read_ushort()    # alternateColorNameIndex
        reader.read_uchar()     # reserved0
        reader.read_uchar()     # billboardFlags
        reader.read_short()     # detailTexturePatternIndex
        tex_index = reader.read_short()
        mat_index = reader.read_short()
        reader.read_ushort()    # surfaceMaterialCode
        reader.read_ushort()    # featureID
        reader.read_uint()      # irMaterialCode
        transparency = reader.read_ushort()
        reader.read_uchar()     # LODGenerationControl
        reader.read_uchar()     # lineStyleIndex
        misc_flags = reader.read_uint()
        reader.read_uchar()     # lightMode
        reader.read_uchar()     # reserved1
        reader.read_ushort()    # reserved2
        reader.read_uint()      # reserved3
        packed_primary = reader.read_uint()
        reader.read_uint()      # packedColorAlternate
        reader.read_ushort()    # textureMappingIndex
        reader.read_ushort()    # reserved4
        color_index = reader.read_uint()
        reader.read_uint()      # alternateColorIndex
        reader.read_ushort()    # reserved5
        reader.read_ushort()    # shaderIndex

        node.tex_index = tex_index
        node.mat_index = mat_index
        node.alpha = 1.0 - transparency / 65535.0

        if misc_flags & FACE_NOCOLOR:
            node.color = (1.0, 1.0, 1.0, 1.0)
        elif misc_flags & FACE_PACKEDCOLOR:
            node.color = self.unpack_abgr(packed_primary)
        else:
            node.color = self.lookup_color(color_index)

        return node

    def _parse_local_vertex_pool(self, reader, mesh_node):
        num_verts = reader.read_uint()
        attr_mask = reader.read_uint()

        for _ in range(num_verts):
            v = FltVertex()

            if attr_mask & LVPATTR_POSITION:
                v.x = reader.read_double()
                v.y = reader.read_double()
                v.z = reader.read_double()

            if attr_mask & (LVPATTR_COLORINDEX | LVPATTR_PACKEDCOLOR):
                color_val = reader.read_uint()
                if attr_mask & LVPATTR_PACKEDCOLOR:
                    v.r, v.g, v.b, v.a = self.unpack_abgr(color_val)
                else:
                    v.r, v.g, v.b, v.a = self.lookup_color(color_val)

            if attr_mask & LVPATTR_NORMAL:
                v.nx = reader.read_float()
                v.ny = reader.read_float()
                v.nz = reader.read_float()

            if attr_mask & LVPATTR_UV0:
                v.u = reader.read_float()
                v.v = reader.read_float()

            mesh_node.lvp_verts.append(v)

    def _parse_mesh_primitive(self, reader, mesh_node):
        prim = FltMeshPrimData()
        prim.prim_type = reader.read_ushort()
        idx_len = reader.read_ushort()        # bytes per index: 1, 2, or 4
        num_verts = reader.read_uint()

        for _ in range(num_verts):
            if idx_len == 1:
                prim.indices.append(reader.read_uchar())
            elif idx_len == 2:
                prim.indices.append(reader.read_ushort())
            else:
                prim.indices.append(reader.read_uint())

        mesh_node.primitives.append(prim)

    def _parse_face(self, reader):
        face = FltFaceData()
        reader.skip(8)                   # face ID string (not needed)
        reader.read_uint()               # irColorCode
        reader.read_short()              # relativePriority
        draw_type = reader.read_char()   # drawType: 0=solid+cull, 1=solid+no-cull(double), ...
        face.double_sided = (draw_type == 1)  # DrawSolidNoBackfaceCulling
        reader.read_uchar()              # textureWhite
        reader.read_ushort()             # colorNameIndex
        reader.read_ushort()             # alternateColorNameIndex
        reader.read_uchar()              # reserved0
        reader.read_uchar()              # billboardFlags
        reader.read_short()              # detailTexturePatternIndex
        face.tex_index = reader.read_short()
        face.mat_index = reader.read_short()
        reader.read_ushort()             # surfaceMaterialCode
        reader.read_ushort()             # featureID
        reader.read_uint()               # irMaterialCode
        transparency = reader.read_ushort()
        reader.read_uchar()              # LODGenerationControl
        reader.read_uchar()              # lineStyleIndex
        misc_flags = reader.read_uint()
        reader.read_uchar()              # lightMode
        reader.read_uchar()              # reserved1
        reader.read_ushort()             # reserved2
        reader.read_uint()               # reserved3
        packed_primary = reader.read_uint()
        reader.read_uint()               # packedColorAlternate

        face.alpha = 1.0 - transparency / 65535.0

        if _is_v14x(self.version) or _is_v13x(self.version):
            color_index = 127   # white in old format
        else:
            reader.read_ushort()     # textureMappingIndex
            reader.read_ushort()     # reserved4
            color_index = reader.read_uint()
            reader.read_uint()       # alternateColorIndex

        # Resolve face color
        if misc_flags & FACE_NOCOLOR:
            face.color = (1.0, 1.0, 1.0, 1.0)
        elif misc_flags & FACE_PACKEDCOLOR:
            face.color = self.unpack_abgr(packed_primary)
        else:
            face.color = self.lookup_color(color_index)

        return face

    def _parse_vertex_list(self, reader, current_parent, pending_lp_node=None):
        """Read a list of byte offsets and resolve them to vertex palette indices.
        Assigns the resulting indices to the last face in current_parent.

        pending_lp_node: the IDX_LP that owns this VERT_LIST (its 3D position).
        Because IDX_LP does not set current_parent, the LP is tracked separately.
        """
        num = (reader.length - 4) // 4
        offsets = [reader.read_int() for _ in range(num)]

        # Light point: vertex list defines the 3D position of the light.
        # In FLT the structure is:  IDX_LP → PUSH → VERT_LIST → POP
        # The single vertex at offsets[0] is the lamp position in model space.
        lp_target = pending_lp_node if pending_lp_node is not None else (
            current_parent if isinstance(current_parent, FltLightPoint) else None
        )
        if lp_target is not None:
            if offsets:
                idx = self.vert_offset_map.get(offsets[0], None)
                if idx is not None:
                    v = self.vert_palette[idx]
                    lp_target.position = (v.x, v.y, v.z)
            return

        # Find the most recently added face in the current parent
        face = None
        if isinstance(current_parent, FltObject) and current_parent.faces:
            face = current_parent.faces[-1]
        elif isinstance(current_parent, FltGroup):
            if hasattr(current_parent, '_implicit_faces') and current_parent._implicit_faces:
                face = current_parent._implicit_faces[-1]

        if face is None:
            return

        for off in offsets:
            idx = self.vert_offset_map.get(off, None)
            if idx is not None:
                face.vertex_indices.append(idx)
                v = self.vert_palette[idx]
                face.uv_list.append((v.u, v.v))
            else:
                # Offset not found - leave a placeholder
                face.vertex_indices.append(-1)
                face.uv_list.append((0.0, 0.0))

    # ── Light Point Appearance Palette parser ─────────────────────────────────

    def _parse_lp_app_palette_entry(self, payload):
        """Parse key fields from a Light Point Appearance Palette (op=128) payload.

        Offset map (relative to payload start, i.e. after the 4-byte record header):
          0    int32   index
          4    char[256] name
          260  int16   state (0=enabled)
          262  int16   lp_type  (0=omni, 1=unidirectional, 2=bidirectional)
          264  float   intensityFront
          268  float   intensityBack
          272  float   minDefocus
          276  float   maxDefocus
          280  int16   fadingMode
          282  int16   fogPunchThrough
          284  float   dirAmbIntensity
          288  float   significance
          292  int32   visibilityRange
          296  float   fadeRangeRatio
          300  float   fadeInDuration
          304  float   fadeOutDuration
          308  float   LOD1Range
          312  float   LOD2Range
          316  int16   primaryColor    ← FLT color palette index
          318  int16   altColor
          320  uint16  flags
          322  int16   reserved (2 bytes padding to align subsequent floats)
          324  float   minPixelSize
          328  float   maxPixelSize
          332  float   actualSize      ← visual size in DB units
          336..361  (tp_* and fog_* fields, 26 bytes)
          362  int16   direction
          364  float   hLobeAngle      ← spot cone angle in degrees
          368  float   vLobeAngle
          372  float   rolloffExponent ← controls spot edge softness
        """
        entry = {}
        try:
            if len(payload) >= 336:
                entry['index']          = struct.unpack_from('>i',  payload,   0)[0]
                entry['name']           = payload[4:260].rstrip(b'\x00').decode('latin-1', 'replace')
                entry['lp_type']        = struct.unpack_from('>h',  payload, 262)[0]
                entry['intensity_front']= struct.unpack_from('>f',  payload, 264)[0]
                entry['primary_color']  = struct.unpack_from('>h',  payload, 316)[0]
                entry['actual_size']    = struct.unpack_from('>f',  payload, 332)[0]
            if len(payload) >= 376:
                entry['direction']      = struct.unpack_from('>h',  payload, 362)[0]
                entry['h_lobe_angle']   = struct.unpack_from('>f',  payload, 364)[0]
                entry['v_lobe_angle']   = struct.unpack_from('>f',  payload, 368)[0]
                entry['rolloff_exp']    = struct.unpack_from('>f',  payload, 372)[0]
        except Exception as e:
            pass  # partial entry is still appended below
        self.lp_app_palette_list.append(entry)

    # ── Mesh primitive conversion helpers ─────────────────────────────────────

    @staticmethod
    def tristrip_to_polys(indices):
        """Convert a triangle strip index list to individual triangle index lists."""
        polys = []
        n = len(indices)
        for i in range(n - 2):
            if i % 2 == 0:
                polys.append([indices[i], indices[i + 1], indices[i + 2]])
            else:
                polys.append([indices[i + 1], indices[i], indices[i + 2]])
        return polys

    @staticmethod
    def trifan_to_polys(indices):
        """Convert a triangle fan index list to individual triangle index lists."""
        polys = []
        n = len(indices)
        for i in range(1, n - 1):
            polys.append([indices[0], indices[i], indices[i + 1]])
        return polys

    @staticmethod
    def quadstrip_to_polys(indices):
        """Convert a quad strip index list to individual quad index lists."""
        polys = []
        n = len(indices)
        i = 0
        while i + 3 < n:
            polys.append([indices[i], indices[i + 1], indices[i + 3], indices[i + 2]])
            i += 2
        return polys
