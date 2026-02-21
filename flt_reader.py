"""
FLT binary reader - pure I/O with no bpy dependencies.
Format: big-endian. Each record: u16 opcode + u16 length (including the 4-byte header).
"""

import struct
import os

# ── Opcodes ──────────────────────────────────────────────────────────────────
OP_HEADER           = 1
OP_GROUP            = 2
OP_OBJECT           = 4
OP_FACE             = 5
OP_PUSH             = 10
OP_POP              = 11
OP_DOF              = 14
OP_PUSH_EXTENSION   = 21
OP_POP_EXTENSION    = 22
OP_COMMENT          = 31
OP_COLOR_PALETTE    = 32
OP_LONG_ID          = 33
OP_MATRIX           = 49
OP_MULTITEXTURE     = 52
OP_UV_LIST          = 53
OP_REPLICATE        = 60
OP_INSTANCE_REF     = 61
OP_INSTANCE_DEF     = 62
OP_EXTERNAL_REF     = 63
OP_TEXTURE_PALETTE  = 64
OP_MATERIAL_TABLE   = 66
OP_VERTEX_PALETTE   = 67
OP_VERTEX_C         = 68
OP_VERTEX_CN        = 69
OP_VERTEX_CNUV      = 70
OP_VERTEX_CUV       = 71
OP_VERTEX_LIST      = 72
OP_LOD              = 73
OP_TRANSLATE        = 78
OP_NONUNIFORM_SCALE = 79
OP_ROTATE_PT        = 80
OP_MESH             = 84
OP_LOCAL_VERTEX_POOL = 85
OP_MESH_PRIMITIVE   = 86
OP_ROAD_SEGMENT     = 87
OP_ROAD_PATH        = 92
OP_GENERAL_MATRIX   = 94
OP_SWITCH           = 96
OP_LINE_STYLE_PAL   = 97
OP_CLIP_REGION      = 98
OP_LIGHT_SOURCE     = 101
OP_LIGHT_SOURCE_PAL = 102
OP_LIGHT_POINT      = 111
OP_TEXTURE_MAP_PAL  = 112
OP_MATERIAL         = 113
OP_NAME_TABLE       = 114
OP_ROAD_CONST       = 127
OP_LIGHTPT_APP_PAL  = 128
OP_LIGHTPT_ANIM_PAL = 129
OP_INDEXED_LP       = 130
OP_LIGHTPT_SYSTEM   = 131
OP_SHADER_PAL       = 133

# MeshPrimitive primitive types
MESHPRIM_TRISTRIP  = 1
MESHPRIM_TRIFAN    = 2
MESHPRIM_QUADSTRIP = 3
MESHPRIM_POLYINDEX = 4

# LocalVertexPool attribute mask bits (bit 31 = MSB)
LVPATTR_POSITION    = (1 << 31)
LVPATTR_COLORINDEX  = (1 << 30)
LVPATTR_PACKEDCOLOR = (1 << 29)
LVPATTR_NORMAL      = (1 << 28)
LVPATTR_UV0         = (1 << 27)
LVPATTR_UV1         = (1 << 26)
LVPATTR_UV2         = (1 << 25)
LVPATTR_UV3         = (1 << 24)
LVPATTR_UV4         = (1 << 23)
LVPATTR_UV5         = (1 << 22)
LVPATTR_UV6         = (1 << 21)
LVPATTR_UV7         = (1 << 20)

# Face misc flags (bit numbering from MSB: bit 31-0 = MSB, bit 31-1 = next, ...)
FACE_NOCOLOR     = (1 << 30)   # bit 31-1: face has no color
FACE_PACKEDCOLOR = (1 << 28)   # bit 31-3: use packed color instead of palette

# Vertex flags
VERT_NO_COLOR     = 0x2000     # bit 15-2
VERT_PACKED_COLOR = 0x1000     # bit 15-3


class FltReader:
    """Low-level binary reader for FLT files."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.file = open(filepath, 'rb')
        self._pos = 0          # start position of current record
        self._length = 0       # length of current record (including 4-byte header)
        self._opcode = 0
        self._next_pos = 0     # end position of current record
        self._repeat = False
        self.level = 0

    # ── Record navigation ────────────────────────────────────────────────────

    def begin_record(self):
        """Read the next record header (opcode + length).
        Returns True on success, False at end of file."""
        if self._repeat:
            self._repeat = False
        else:
            self._pos += self._length

        try:
            self.file.seek(self._pos)
            hdr = self.file.read(4)
        except OSError:
            return False

        if len(hdr) < 4:
            return False

        self._opcode = struct.unpack('>h', hdr[:2])[0]
        self._length = struct.unpack('>H', hdr[2:4])[0]
        self._next_pos = self._pos + self._length

        # Minimum record size is 4 bytes (header only)
        if self._length < 4:
            self._length = 4
            self._next_pos = self._pos + 4

        return True

    def repeat_record(self):
        """Re-read the same record on the next begin_record() call."""
        self._repeat = True

    @property
    def opcode(self):
        return self._opcode

    @property
    def length(self):
        return self._length

    def skip_to_end(self):
        """Seek to the end of the current record."""
        self.file.seek(self._next_pos)

    def close(self):
        self.file.close()

    # ── Primitive data types (all big-endian) ────────────────────────────────

    def _tell(self):
        return self.file.tell()

    def _can_read(self, n):
        """Check if we can safely read n bytes within the current record."""
        return self._tell() + n <= self._next_pos

    def read_string(self, length):
        """Read a null-terminated string of exactly `length` bytes."""
        if not self._can_read(length):
            self.file.seek(min(self._tell() + length, self._next_pos))
            return ''
        raw = self.file.read(length)
        # Strip null terminator and everything after it
        nul = raw.find(b'\x00')
        if nul >= 0:
            raw = raw[:nul]
        try:
            return raw.decode('latin-1')
        except Exception:
            return raw.decode('ascii', errors='replace')

    def read_int(self):
        if not self._can_read(4):
            return 0
        return struct.unpack('>i', self.file.read(4))[0]

    def read_uint(self):
        if not self._can_read(4):
            return 0
        return struct.unpack('>I', self.file.read(4))[0]

    def read_double(self):
        if not self._can_read(8):
            return 0.0
        return struct.unpack('>d', self.file.read(8))[0]

    def read_float(self):
        if not self._can_read(4):
            return 0.0
        return struct.unpack('>f', self.file.read(4))[0]

    def read_ushort(self):
        if not self._can_read(2):
            return 0
        return struct.unpack('>H', self.file.read(2))[0]

    def read_short(self):
        if not self._can_read(2):
            return 0
        return struct.unpack('>h', self.file.read(2))[0]

    def read_uchar(self):
        if not self._can_read(1):
            return 0
        return struct.unpack('>B', self.file.read(1))[0]

    def read_char(self):
        if not self._can_read(1):
            return 0
        return struct.unpack('>b', self.file.read(1))[0]

    def skip(self, n):
        """Advance file position by n bytes, clamped to end of current record."""
        target = min(self._tell() + n, self._next_pos)
        self.file.seek(target)

    def read_matrix4x4(self):
        """Read 16 float32 values (row-major) and return as list of 4 rows."""
        m = [self.read_float() for _ in range(16)]
        return [
            [m[0],  m[1],  m[2],  m[3]],
            [m[4],  m[5],  m[6],  m[7]],
            [m[8],  m[9],  m[10], m[11]],
            [m[12], m[13], m[14], m[15]],
        ]
