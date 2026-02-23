"""
OpenFlight (.flt) Importer/Exporter for Blender 5.0.1
Created by Aleksander Pininski - https://github.com/eltales
Based on OpenFlight format specification and blight-1.2 by Greg MacDonald.
Rewritten for Blender 5.x (Python 3, bpy API 4.x/5.x).

Supported features:
- Hierarchy: Header, Group, Object, Face, LOD, Switch, DOF, ExternalReference
- Vertices: all 4 types (C, CN, CUV, CNUV) with color, normals and UV
- Mesh (op=84) with LocalVertexPool (op=85) and MeshPrimitive (op=86)
- Textures (palette, UV mapping)
- Materials (material palette, Principled BSDF, packed color, transparency)
- Transform matrices (op=49)
- LongID (op=33), Comment (op=31)
- External references (op=63) with optional recursive import
- Light Points (op=130) imported as Blender POINT/SPOT/AREA lights
    - 3D position from vertex list (op=72)
    - Light type, color, energy from Appearance Palette (op=128)
    - Full round-trip: op=128 raw bytes preserved for lossless re-export
- Export: Blender scene → FLT with full material/color/transparency round-trip
- Export: Blender Light objects → FLT op=130 Indexed Light Points
"""

bl_info = {
    "name": "OpenFlight FLT format",
    "author": "Aleksander Pininski (https://github.com/eltales), original: Greg MacDonald",
    "version": (2, 4, 0),
    "blender": (5, 0, 0),
    "location": "File > Import-Export > OpenFlight (.flt)",
    "description": "Import/Export OpenFlight (.flt) files with full material and hierarchy support",
    "doc_url": "https://github.com/eltales",
    "category": "Import-Export",
}

import bpy
from bpy.props import StringProperty, BoolProperty, FloatProperty
from bpy_extras.io_utils import ImportHelper, ExportHelper


class ImportFLT(bpy.types.Operator, ImportHelper):
    """Import an OpenFlight .flt file"""
    bl_idname = "import_scene.flt"
    bl_label = "Import OpenFlight (.flt)"
    bl_options = {'UNDO'}

    filename_ext = ".flt"
    filter_glob: StringProperty(default="*.flt", options={'HIDDEN'})

    import_textures: BoolProperty(
        name="Import Textures",
        description="Load and assign textures referenced in the FLT file",
        default=True,
    )
    import_materials: BoolProperty(
        name="Import Materials",
        description="Import material palette entries",
        default=True,
    )
    import_external_refs: BoolProperty(
        name="Import External References",
        description="Recursively import externally referenced FLT files",
        default=True,
    )
    scale: FloatProperty(
        name="Scale",
        description="Scale factor applied to all coordinates",
        default=1.0,
        min=0.0001,
        max=10000.0,
    )

    def execute(self, context):
        from . import flt_import
        keywords = self.as_keywords(ignore=("filter_glob",))
        return flt_import.load(context, **keywords)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "import_textures")
        layout.prop(self, "import_materials")
        layout.prop(self, "import_external_refs")
        layout.prop(self, "scale")


def menu_func_import(self, context):
    self.layout.operator(ImportFLT.bl_idname, text="OpenFlight (.flt)")


class ExportFLT(bpy.types.Operator, ExportHelper):
    """Export scene to an OpenFlight .flt file"""
    bl_idname = "export_scene.flt"
    bl_label = "Export OpenFlight (.flt)"
    bl_options = {'UNDO'}

    filename_ext = ".flt"
    filter_glob: StringProperty(default="*.flt", options={'HIDDEN'})

    export_selected: BoolProperty(
        name="Selected Only",
        description="Export only selected objects",
        default=False,
    )
    scale: FloatProperty(
        name="Scale",
        description="Scale factor applied to all coordinates (use same value as during import)",
        default=1.0,
        min=0.0001,
        max=10000.0,
    )

    def execute(self, context):
        from . import flt_export
        keywords = self.as_keywords(ignore=("filter_glob", "check_existing"))
        return flt_export.save(context, **keywords)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_selected")
        layout.prop(self, "scale")


def menu_func_export(self, context):
    self.layout.operator(ExportFLT.bl_idname, text="OpenFlight (.flt)")


def register():
    bpy.utils.register_class(ImportFLT)
    bpy.utils.register_class(ExportFLT)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)


def unregister():
    bpy.utils.unregister_class(ImportFLT)
    bpy.utils.unregister_class(ExportFLT)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)


if __name__ == "__main__":
    register()
