"""
FLT Importer - converts FltDatabase data into a Blender 5.0.1 scene.
"""

import os
import math
import base64
import struct
import traceback

import bpy
import bmesh
import mathutils

from .flt_parser import (
    FltDatabase,
    FltGroup, FltObject, FltLOD, FltSwitch, FltDOF,
    FltExtRef, FltMeshNode, FltLightPoint,
)
from .flt_reader import (
    MESHPRIM_TRISTRIP, MESHPRIM_TRIFAN, MESHPRIM_QUADSTRIP, MESHPRIM_POLYINDEX,
)


# ── Entry point ───────────────────────────────────────────────────────────────

def load(context, filepath, import_textures=True, import_materials=True,
         import_external_refs=True, scale=1.0, **kwargs):
    """Load a FLT file into the active Blender scene."""
    print(f"FLT Import: {filepath}")
    try:
        db = FltDatabase(filepath)
        db.parse()
        importer = FltImporter(context, db,
                               import_textures=import_textures,
                               import_materials=import_materials,
                               import_external_refs=import_external_refs,
                               scale=scale)
        importer.build_scene()
    except Exception:
        traceback.print_exc()
        return {'CANCELLED'}

    return {'FINISHED'}


# ── Main importer class ───────────────────────────────────────────────────────

class FltImporter:
    def __init__(self, context, db, import_textures=True,
                 import_materials=True, import_external_refs=True, scale=1.0):
        self.context = context
        self.db = db
        self.scale = scale
        self.import_textures = import_textures
        self.import_materials = import_materials
        self.import_external_refs = import_external_refs

        self._mat_cache = {}    # (tex_index, mat_index, color, alpha) -> bpy.data.materials
        self._img_cache = {}    # tex_path -> bpy.data.images (or None)

    # ── Scene building ────────────────────────────────────────────────────────

    def build_scene(self):
        scene = self.context.scene
        col_name = os.path.splitext(os.path.basename(self.db.filepath))[0]

        root_col = bpy.data.collections.new(col_name)
        scene.collection.children.link(root_col)

        for node in self.db.children:
            self._import_node(node, root_col, parent_obj=None)

        # Store Light Point Appearance Palette raw records (op=128) in scene
        # custom property so they survive a save/reload and can be re-exported.
        if self.db.lp_app_palette_raw:
            entries = []
            for (rec_len, payload) in self.db.lp_app_palette_raw:
                raw = struct.pack('>hH', 128, rec_len) + payload
                entries.append(base64.b64encode(raw).decode('ascii'))
            scene['flt_lp_app_palette'] = entries

        bpy.context.view_layer.update()

    # ── Recursive node import ─────────────────────────────────────────────────

    def _import_node(self, node, collection, parent_obj):
        """Import one node and recursively import its children."""
        obj = None

        if isinstance(node, FltObject):
            obj = self._import_object(node, collection, parent_obj)
        elif isinstance(node, FltGroup):
            obj = self._import_group(node, collection, parent_obj)
        elif isinstance(node, FltLOD):
            obj = self._import_lod(node, collection, parent_obj)
        elif isinstance(node, FltSwitch):
            obj = self._import_switch(node, collection, parent_obj)
        elif isinstance(node, FltDOF):
            obj = self._import_dof(node, collection, parent_obj)
        elif isinstance(node, FltMeshNode):
            obj = self._import_mesh_node(node, collection, parent_obj)
        elif isinstance(node, FltExtRef):
            obj = self._import_ext_ref(node, collection, parent_obj)
        elif isinstance(node, FltLightPoint):
            obj = self._import_light_point(node, collection, parent_obj)

        if obj is None:
            obj = self._create_empty(
                node.long_name or node.name or 'FLT_Node',
                collection, parent_obj
            )

        self._apply_matrix(obj, node.matrix)

        # Light points: the 3D position is stored in a vertex-list child record
        # (FLT structure: IDX_LP → PUSH → VERT_LIST → POP).  Apply it as the
        # object's local translation so it lands at the lamp-head position.
        if isinstance(node, FltLightPoint) and node.position is not None:
            s = self.scale
            x, y, z = node.position
            obj.location = (x * s, y * s, z * s)

        for child in node.children:
            self._import_node(child, collection, obj)

    # ── FltObject → Mesh or Empty ─────────────────────────────────────────────

    def _import_object(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'Object'
        all_faces = node.faces + getattr(node, '_implicit_faces', [])
        if all_faces:
            return self._build_mesh_object(name, all_faces, collection, parent_obj)
        return self._create_empty(name, collection, parent_obj)

    # ── FltGroup ──────────────────────────────────────────────────────────────

    def _import_group(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'Group'
        implicit = getattr(node, '_implicit_faces', [])
        if implicit:
            return self._build_mesh_object(name, implicit, collection, parent_obj)
        return self._create_empty(name, collection, parent_obj)

    # ── FltLOD ────────────────────────────────────────────────────────────────

    def _import_lod(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'LOD'
        obj = self._create_empty(name, collection, parent_obj)
        obj['flt_type'] = 'LOD'
        obj['flt_switch_in'] = node.switch_in
        obj['flt_switch_out'] = node.switch_out
        return obj

    # ── FltSwitch ─────────────────────────────────────────────────────────────

    def _import_switch(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'Switch'
        obj = self._create_empty(name, collection, parent_obj)
        obj['flt_type'] = 'SWITCH'
        obj['flt_current_mask'] = node.current_mask
        return obj

    # ── FltDOF ────────────────────────────────────────────────────────────────

    def _import_dof(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'DOF'
        obj = self._create_empty(name, collection, parent_obj)
        obj['flt_type'] = 'DOF'
        if 'origin' in node.limits:
            obj['flt_origin'] = list(node.limits['origin'])
        return obj

    # ── FltExtRef ─────────────────────────────────────────────────────────────

    def _import_ext_ref(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'ExtRef'
        obj = self._create_empty(name, collection, parent_obj)
        obj['flt_type'] = 'EXTREF'
        obj['flt_extref_path'] = node.path

        if self.import_external_refs:
            resolved = self.db.find_file(node.path)
            if resolved and resolved != self.db.filepath:
                try:
                    sub_db = FltDatabase(resolved)
                    for d in self.db.search_dirs:
                        sub_db.add_search_dir(d)
                    sub_db.parse()
                    sub_imp = FltImporter(
                        self.context, sub_db,
                        import_textures=self.import_textures,
                        import_materials=self.import_materials,
                        import_external_refs=False,  # prevent infinite recursion
                        scale=self.scale,
                    )
                    # Share material and image caches with the parent importer
                    sub_imp._mat_cache = self._mat_cache
                    sub_imp._img_cache = self._img_cache
                    for child in sub_db.children:
                        sub_imp._import_node(child, collection, obj)
                except Exception as e:
                    print(f"FLT: Could not import external ref '{resolved}': {e}")
        return obj

    # ── FltLightPoint ─────────────────────────────────────────────────────────

    def _import_light_point(self, node, collection, parent_obj):
        """Import a FLT Light Point as a Blender Light object.

        If an Appearance Palette entry (op=128) is available for the node's
        app_idx, we create a real POINT / SPOT / AREA light and map the key
        FLT properties onto it.  Otherwise we fall back to an Empty so the
        hierarchy is preserved.
        """
        name = node.long_name or node.name or 'LightPoint'

        # Try to find a matching appearance palette entry
        pal = None
        if (hasattr(self.db, 'lp_app_palette_list') and
                0 <= node.app_idx < len(self.db.lp_app_palette_list)):
            pal = self.db.lp_app_palette_list[node.app_idx]

        if pal:
            obj = self._create_light_from_palette(name, pal, collection, parent_obj)
        else:
            obj = self._create_empty(name, collection, parent_obj)

        # Save FLT metadata on the object for round-trip export
        obj['flt_type']          = 'LIGHTPOINT'
        obj['flt_lp_app_idx']    = int(node.app_idx)
        obj['flt_lp_anim_idx']   = int(node.anim_idx)
        obj['flt_lp_draw_order'] = int(node.draw_order)
        obj['flt_lp_flags']      = int(node.lp_flags)
        return obj

    def _create_light_from_palette(self, name, pal, collection, parent_obj):
        """Build a Blender Light object from a parsed FLT appearance palette dict.

        FLT light type mapping:
          0 (omni)          → POINT
          1 (unidirectional)→ SPOT
          2 (bidirectional) → AREA
        """
        # ── Light type ────────────────────────────────────────────────────────
        lp_type = pal.get('lp_type', 0)
        blender_type = {0: 'POINT', 1: 'SPOT', 2: 'AREA'}.get(lp_type, 'POINT')

        light_data = bpy.data.lights.new(name=name, type=blender_type)

        # ── Color (from FLT color palette lookup) ─────────────────────────────
        primary_color_idx = pal.get('primary_color', 127)
        try:
            r, g, b, _ = self.db.lookup_color(primary_color_idx)
        except Exception:
            r, g, b = 1.0, 1.0, 1.0
        # Clamp to valid range (lookup_color may return tiny values)
        r = max(0.0, min(1.0, r if r > 0.001 else 1.0))
        g = max(0.0, min(1.0, g if g > 0.001 else 1.0))
        b = max(0.0, min(1.0, b if b > 0.001 else 1.0))
        light_data.color = (r, g, b)

        # ── Intensity → Blender energy (Watts) ────────────────────────────────
        # FLT intensityFront is a dimensionless 0–1 multiplier with no direct
        # physical equivalent in Blender.  Practical calibration for metric scenes:
        #   Blender POINT at distance r gives  E = P / (4π·r²)  lux
        #   For a street lamp at h=6 m, target ground illuminance ~20 lux:
        #     P = 20 · 4π · 36 ≈ 9 000 W  → use 1 000 W as a conservative baseline.
        # intensityFront=1.0 → 1 000 W  (scale in Blender if the scene needs it)
        # intensityFront=0.0 → minimum 1 W so the object is still selectable
        intensity = pal.get('intensity_front', 1.0)
        light_data.energy = max(0.001, intensity) * 1000.0

        # ── Radius ────────────────────────────────────────────────────────────
        # FLT actualSize is the visual sprite size of the light-point glyph in
        # FLT's rasterizer — it has nothing to do with Blender's physical
        # emission radius (which controls soft-shadow penumbra size).
        # Set radius = 0 so the import creates a true point light.
        for attr in ('radius', 'shadow_soft_size'):
            if hasattr(light_data, attr):
                setattr(light_data, attr, 0.0)
                break

        # ── Spot-specific properties ──────────────────────────────────────────
        if blender_type == 'SPOT':
            h_lobe = pal.get('h_lobe_angle', 45.0)
            rolloff = pal.get('rolloff_exp', 1.0)
            # h_lobe_angle is in degrees (full cone angle assumed)
            light_data.spot_size = math.radians(max(1.0, min(179.0, h_lobe)))
            # rolloffExponent: higher = sharper edge → lower blend
            light_data.spot_blend = min(1.0, max(0.0, 1.0 / max(0.1, rolloff)))

        obj = bpy.data.objects.new(name, light_data)
        collection.objects.link(obj)
        self._set_parent(obj, parent_obj)
        return obj

    # ── FltMeshNode → Blender Mesh ────────────────────────────────────────────

    def _import_mesh_node(self, node, collection, parent_obj):
        name = node.long_name or node.name or 'Mesh'

        if not node.lvp_verts or not node.primitives:
            return self._create_empty(name, collection, parent_obj)

        mesh = bpy.data.meshes.new(name)
        bm = bmesh.new()
        uv_layer = bm.loops.layers.uv.new('UVMap')

        s = self.scale
        bm_verts = [bm.verts.new((v.x * s, v.y * s, v.z * s)) for v in node.lvp_verts]
        bm.verts.ensure_lookup_table()

        # Convert all primitives to polygon lists
        all_polys = []
        for prim in node.primitives:
            indices = prim.indices
            if prim.prim_type == MESHPRIM_TRISTRIP:
                all_polys.extend(FltDatabase.tristrip_to_polys(indices))
            elif prim.prim_type == MESHPRIM_TRIFAN:
                all_polys.extend(FltDatabase.trifan_to_polys(indices))
            elif prim.prim_type == MESHPRIM_QUADSTRIP:
                all_polys.extend(FltDatabase.quadstrip_to_polys(indices))
            else:   # POLYINDEX
                all_polys.append(indices)

        for poly in all_polys:
            if len(poly) < 3:
                continue
            try:
                verts = [bm_verts[i] for i in poly if 0 <= i < len(bm_verts)]
                if len(verts) < 3:
                    continue
                face = bm.faces.new(verts)
                for loop, vi in zip(face.loops, poly):
                    if 0 <= vi < len(node.lvp_verts):
                        lv = node.lvp_verts[vi]
                        loop[uv_layer].uv = (lv.u, lv.v)
            except ValueError:
                pass    # duplicate vertices in the face - skip

        bm.to_mesh(mesh)
        bm.free()
        mesh.validate()
        mesh.update()

        mat = self._get_or_create_material(
            node.tex_index, node.mat_index, node.color, node.alpha,
            double_sided=node.double_sided
        )
        if mat:
            mesh.materials.append(mat)

        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
        self._set_parent(obj, parent_obj)
        return obj

    # ── Build mesh from face list ─────────────────────────────────────────────

    def _build_mesh_object(self, name, face_list, collection, parent_obj):
        """Create a Blender mesh object from a list of FltFaceData.

        Vertices are shared by palette index so Blender can compute smooth
        normals across shared edges.  UV coordinates are stored per loop
        (face corner) so different faces may use the same vertex with
        different UV values.
        """
        bm = bmesh.new()
        uv_layer = bm.loops.layers.uv.new('UVMap')

        s = self.scale
        # palette index → bmesh vertex (fast path: same palette entry)
        pal_to_bm  = {}
        # rounded XYZ → bmesh vertex (merges palette entries at the same
        # 3D position, e.g. UV-seam duplicates on cylinders / cones).
        # UV coords are stored per-loop, so two faces can share one bmesh
        # vertex and still carry different UV values at that corner.
        pos_to_bm  = {}
        # per-face data accumulated during the loop
        faces_bv   = []   # list of [bm_vert, …]
        faces_uvs  = []   # list of [(u, v), …]
        faces_mi   = []   # material slot index

        mat_map = {}
        mats    = []

        for fdata in face_list:
            if not fdata.vertex_indices or len(fdata.vertex_indices) < 3:
                continue

            # Filter invalid palette references
            valid = [
                (vi, uv)
                for vi, uv in zip(fdata.vertex_indices, fdata.uv_list)
                if 0 <= vi < len(self.db.vert_palette)
            ]
            if len(valid) < 3:
                continue

            bverts = []
            buvs   = []
            for vi, uv in valid:
                if vi not in pal_to_bm:
                    v = self.db.vert_palette[vi]
                    co = (round(v.x * s, 6), round(v.y * s, 6), round(v.z * s, 6))
                    if co in pos_to_bm:
                        # Same 3D position already exists (UV-seam duplicate).
                        # Reuse the existing bmesh vertex so the edge is shared.
                        pal_to_bm[vi] = pos_to_bm[co]
                    else:
                        bv = bm.verts.new(co)
                        pal_to_bm[vi] = bv
                        pos_to_bm[co] = bv
                bverts.append(pal_to_bm[vi])
                buvs.append(uv)

            mat_key = (fdata.tex_index, fdata.mat_index,
                       fdata.color, round(fdata.alpha, 4),
                       fdata.double_sided)
            if mat_key not in mat_map:
                mat = self._get_or_create_material(
                    fdata.tex_index, fdata.mat_index,
                    fdata.color, fdata.alpha,
                    double_sided=fdata.double_sided
                )
                if mat:
                    mat_map[mat_key] = len(mats)
                    mats.append(mat)
                else:
                    mat_map[mat_key] = -1

            faces_bv.append(bverts)
            faces_uvs.append(buvs)
            faces_mi.append(mat_map.get(mat_key, -1))

        if not pal_to_bm:
            bm.free()
            return self._create_empty(name, collection, parent_obj)

        # Mesh is created only after we know there is real geometry
        mesh = bpy.data.meshes.new(name)

        bm.verts.ensure_lookup_table()

        for bverts, uvs, mi in zip(faces_bv, faces_uvs, faces_mi):
            try:
                face = bm.faces.new(bverts)
                face.material_index = max(0, mi)
                for loop, uv in zip(face.loops, uvs):
                    loop[uv_layer].uv = uv
            except ValueError:
                pass  # skip degenerate or duplicate faces

        bm.to_mesh(mesh)
        bm.free()

        for mat in mats:
            mesh.materials.append(mat)

        mesh.validate()
        mesh.update()

        obj = bpy.data.objects.new(name, mesh)
        collection.objects.link(obj)
        self._set_parent(obj, parent_obj)
        return obj

    # ── Utility helpers ───────────────────────────────────────────────────────

    def _create_empty(self, name, collection, parent_obj):
        obj = bpy.data.objects.new(name, None)
        obj.empty_display_type = 'PLAIN_AXES'
        obj.empty_display_size = 0.1
        collection.objects.link(obj)
        self._set_parent(obj, parent_obj)
        return obj

    def _set_parent(self, obj, parent_obj):
        if parent_obj is not None:
            obj.parent = parent_obj

    def _apply_matrix(self, obj, matrix_rows):
        """Apply a 4x4 row-major FLT transform matrix to a Blender object."""
        if matrix_rows is None or obj is None:
            return
        try:
            mat = mathutils.Matrix(matrix_rows)
            # Scale translation components
            s = self.scale
            mat[0][3] *= s
            mat[1][3] *= s
            mat[2][3] *= s
            obj.matrix_local = mat
        except Exception as e:
            print(f"FLT: Failed to apply matrix to '{obj.name}': {e}")

    # ── Material handling ─────────────────────────────────────────────────────

    def _get_or_create_material(self, tex_index, mat_index, color, alpha,
                               double_sided=False):
        """Return a cached or newly created Principled BSDF material."""
        if not self.import_materials:
            return None

        key = (tex_index, mat_index, color, round(alpha, 4), double_sided)
        if key in self._mat_cache:
            return self._mat_cache[key]

        mat_name = self._build_mat_name(tex_index, mat_index)
        mat = bpy.data.materials.new(name=mat_name)
        # Backface culling: FLT DrawType=1 means no backface culling (double-sided)
        try:
            mat.use_backface_culling = not double_sided
        except AttributeError:
            pass
        mat.use_nodes = True

        tree = mat.node_tree
        tree.nodes.clear()

        out_node = tree.nodes.new('ShaderNodeOutputMaterial')
        out_node.location = (300, 0)

        bsdf = tree.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)
        tree.links.new(bsdf.outputs['BSDF'], out_node.inputs['Surface'])

        # Base color from material palette or from face color
        if self.import_materials and 0 <= mat_index and mat_index in self.db.mat_palette:
            mat_data = self.db.mat_palette[mat_index]
            dr, dg, db_ = mat_data['diffuse']
            bsdf.inputs['Base Color'].default_value = (dr, dg, db_, 1.0)
            shininess = mat_data.get('shininess', 0.0)
            # FLT shininess is an OpenGL Phong exponent (0-128).
            # Linear mapping 1-s/128 makes terrain look like oily plastic.
            # Square-root mapping compresses the high-shininess range and
            # keeps a minimum roughness of 0.25 even for shininess=128.
            import math
            roughness = max(0.25, math.sqrt(max(0.0, 1.0 - shininess / 128.0)))
            bsdf.inputs['Roughness'].default_value = roughness
            mat_alpha = mat_data.get('alpha', alpha)
        else:
            r, g, b, a = color
            bsdf.inputs['Base Color'].default_value = (r, g, b, 1.0)
            # Default for palette-less faces: slightly matte (0.8).
            # FLT terrain surfaces (concrete, gravel, grass) are rarely shiny.
            bsdf.inputs['Roughness'].default_value = 0.8
            mat_alpha = alpha

        # Transparency from face/material alpha value
        if mat_alpha < 0.9999:
            bsdf.inputs['Alpha'].default_value = mat_alpha
            # BLEND → BLENDED in Blender 4.2+; disable overlap to avoid
            # walls-through-walls sorting artifacts.
            try:
                mat.blend_method = 'BLEND'
                mat.shadow_method = 'CLIP'
            except AttributeError:
                pass
            try:
                # Blender 4.2+ EEVEE Next: disable transparency overlap
                mat.use_transparency_overlap = False
            except AttributeError:
                pass

        # Texture
        if self.import_textures and tex_index >= 0 and tex_index in self.db.tex_palette:
            tex_path = self.db.tex_palette[tex_index]
            img = self._get_or_load_image(tex_path)
            if img:
                tex_node = tree.nodes.new('ShaderNodeTexImage')
                tex_node.image = img
                tex_node.location = (-300, 0)
                uv_node = tree.nodes.new('ShaderNodeTexCoord')
                uv_node.location = (-500, 0)
                tree.links.new(uv_node.outputs['UV'], tex_node.inputs['Vector'])
                tree.links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                if img.channels == 4:
                    # RGBA texture: use HASHED (→ DITHERED in Blender 4.2+).
                    # Renders alpha cutouts correctly without overlap/sorting artifacts.
                    # BLEND/BLENDED causes "Transparency Overlap" making surfaces
                    # render through each other.
                    tree.links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
                    try:
                        mat.blend_method = 'HASHED'
                    except AttributeError:
                        try:
                            mat.surface_render_method = 'DITHERED'
                        except AttributeError:
                            pass

        self._mat_cache[key] = mat
        return mat

    def _build_mat_name(self, tex_index, mat_index):
        """Build a descriptive material name from palette indices."""
        parts = ['FLT']
        if 0 <= mat_index and mat_index in self.db.mat_palette:
            n = self.db.mat_palette[mat_index].get('name', '').strip('\x00').strip()
            parts.append(n if n else f'Mat{mat_index}')
        if tex_index >= 0 and tex_index in self.db.tex_palette:
            bn = os.path.splitext(os.path.basename(self.db.tex_palette[tex_index]))[0]
            parts.append(bn)
        return '_'.join(parts)

    def _get_or_load_image(self, tex_path):
        """Load or retrieve a texture image, with path resolution and caching."""
        if not tex_path:
            return None
        if tex_path in self._img_cache:
            return self._img_cache[tex_path]

        resolved = self.db.find_file(tex_path)
        if not resolved:
            # Try common texture extensions
            for ext in ('.rgb', '.rgba', '.png', '.jpg', '.jpeg', '.tga', '.bmp', '.dds'):
                candidate = self.db.find_file(tex_path + ext)
                if candidate:
                    resolved = candidate
                    break

        if not resolved:
            print(f"FLT: Texture not found: '{tex_path}'")
            self._img_cache[tex_path] = None
            return None

        # Check if the image is already loaded in Blender
        resolved_abs = os.path.abspath(resolved)
        for img in bpy.data.images:
            try:
                if os.path.abspath(img.filepath) == resolved_abs:
                    self._img_cache[tex_path] = img
                    return img
            except Exception:
                pass

        try:
            img = bpy.data.images.load(resolved, check_existing=True)
            self._img_cache[tex_path] = img
            return img
        except Exception as e:
            print(f"FLT: Failed to load image '{resolved}': {e}")
            self._img_cache[tex_path] = None
            return None
