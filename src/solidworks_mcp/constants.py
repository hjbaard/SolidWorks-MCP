"""Verified SolidWorks enum constants.

Source: the installed swconst.tlb (SOLIDWORKS 2026, typelib v34), read directly
via scripts/introspect_api.py. Hardcoded here -- with their enum of origin -- so
the server has no runtime dependency on the makepy constant cache. Note e.g.
swDefaultTemplatePart == 8 on this build (not 9, as often stated online): always
trust the installed library over web docs.
"""

# swDocumentTypes_e (for OpenDoc6 / IModelDoc2.GetType)
SW_DOC_PART = 1
SW_DOC_ASSEMBLY = 2

# swEndConditions_e
SW_END_COND_BLIND = 0
SW_END_COND_THROUGH_ALL = 1
SW_END_COND_MID_PLANE = 6

# swBodyType_e
SW_BODY_SOLID = 0

# swFeatureFilletType_e
SW_FILLET_TYPE_SIMPLE = 0

# swFeatureFilletOptions_e (bitmask). UNIFORM_RADIUS makes the fillet use the
# single R1 radius for all edges; without it the API expects a per-edge Radii
# array and returns None. Tangent propagation is intentionally NOT enabled, so
# the explicit edge selection equals exactly what gets filleted.
SW_FILLET_OPT_UNIFORM_RADIUS = 2

# swChamferType_e -- AngleDistance is a setback distance + an angle (45 deg gives
# a symmetric chamfer). EqualDistance(16) alone is a silent no-op on this build.
SW_CHAMFER_ANGLE_DISTANCE = 1

# swSketchSlotCreationType_e / swSketchSlotLengthType_e
SW_SLOT_CREATION_LINE = 0       # straight slot
SW_SLOT_LENGTH_CENTER = 0       # length is centre-to-centre of the end arcs

# swRefPlaneReferenceConstraints_e -- offset a new plane a fixed distance from a
# selected reference plane (for lofts: one parallel plane per profile).
SW_REF_PLANE_DISTANCE = 8

# SolidWorks' own modelled thread: IFeatureManager.CreateDefinition(swFmSweepThread)
# gives an IThreadFeatureData. The profile libraries (File Locations > Thread
# Profiles) hold one configuration per valid size, e.g. 'M10x1.5'.
SW_FM_SWEEP_THREAD = 87          # swFeatureNameID_e
SW_THREAD_METHOD_CUT = 0         # swThreadMethod_e
SW_THREAD_END_BLIND = 0          # swThreadEndCondition_e
THREAD_PROFILE_EXTERNAL = "Metric Die"
THREAD_PROFILE_INTERNAL = "Metric Tap"

# STL/3MF tessellation, set as ISldWorks user preferences BEFORE SaveAs3 (the mesh
# translator reads them at save time). These are GLOBAL/application prefs, so the
# caller must save and restore them around the export.
SW_STL_QUALITY = 78            # swUserPreferenceIntegerValue_e (swSTLQuality)
SW_STL_QUALITY_COARSE = 1      # swSTLQuality_e
SW_STL_QUALITY_FINE = 2
SW_STL_QUALITY_CUSTOM = 3      # enables swSTLDeviation + swSTLAngleTolerance
SW_STL_DEVIATION = 2           # swUserPreferenceDoubleValue_e -- chord tolerance (METRES)
SW_STL_ANGLE_TOLERANCE = 3     # swUserPreferenceDoubleValue_e -- angular tolerance (RADIANS)

# swStartConditions_e
SW_START_SKETCH_PLANE = 0

# swUserPreferenceStringValue_e
SW_PREF_DEFAULT_TEMPLATE_PART = 8
SW_PREF_DEFAULT_TEMPLATE_ASSEMBLY = 9

# --- assemblies ---------------------------------------------------------------

# swAddComponentConfigOptions_e -- insert the component using the configuration
# that is currently selected in the part.
SW_ADD_COMPONENT_CURRENT_CONFIG = 0

# swMateType_e. Only the types that make sense between two PLANAR faces are
# exposed; concentric/tangent need a cylindrical selection, which the planar
# face selector cannot produce.
MATE_TYPES = {
    "coincident": 0,     # swMateCOINCIDENT
    "perpendicular": 2,  # swMatePERPENDICULAR
    "parallel": 3,       # swMatePARALLEL
    "distance": 5,       # swMateDISTANCE
}

# swMateAlign_e -- CLOSEST lets SolidWorks keep the solution nearest the current
# position, which is what we want because every component is pre-positioned
# before it is mated.
SW_MATE_ALIGN_CLOSEST = 2

# swAddMateError_e -- note NoError is 1, not 0.
SW_ADD_MATE_NO_ERROR = 1

# swBoundingBoxOptions_e bitmask for IAssemblyDoc.GetBox: 0 = solid geometry
# only (1 would add reference planes, 2 sketches, which would inflate the box).
SW_BOUNDING_BOX_SOLID_ONLY = 0

# swUserPreferenceToggle_e
SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE = 10

# swOpenDocOptions_e -- silent load, no dialogs. AddComponent5 returns None for a
# part that is not loaded yet (verified), so components are opened this way first.
SW_OPEN_DOC_SILENT = 1

# swSaveAsVersion_e
SW_SAVE_AS_CURRENT_VERSION = 0

# swSaveAsOptions_e
SW_SAVE_AS_OPTIONS_SILENT = 1

# swStandardViews_e
SW_VIEW_ISOMETRIC = 7

# Formats SaveAs3 can write (by extension), allow-listed for `export`. Image
# extensions (png/bmp/jpg/tif) are here because `screenshot` writes via the same
# SaveAs3 path.
EXPORT_FORMATS = {"step", "stp", "stl", "iges", "igs", "x_t", "x_b", "3mf", "png", "bmp", "jpg", "tif"}
