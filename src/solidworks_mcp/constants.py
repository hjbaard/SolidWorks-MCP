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
SW_DOC_DRAWING = 3

# swEndConditions_e
SW_END_COND_BLIND = 0
SW_END_COND_THROUGH_ALL = 1
SW_END_COND_UP_TO_SURFACE = 4  # the face selected with mark 1
SW_END_COND_MID_PLANE = 6
SW_END_COND_UP_TO_NEXT = 11

# swBodyType_e
SW_BODY_SOLID = 0

# swFeatureError_e: what IFeature.GetErrorCode2 reports, for the codes a tool meets
FEATURE_ERRORS = {
    1: "unknown",
    12: "the fillet radius does not fit",
    38: "a mate edge is not valid",
    39: "a mate face is not valid",
    41: "a mate entity is not valid",
    43: "dangling: a face or edge it used is gone",
    46: "over-defined: it fights another mate",
    47: "ill-defined",
    48: "broken: a face or edge it used is gone",
}

# swFeatureFilletType_e
SW_FILLET_TYPE_SIMPLE = 0
SW_FILLET_TYPE_VARIABLE = 1
SW_FILLET_TYPE_FULL_ROUND = 3

# swFeatureFilletOptions_e (bitmask). UNIFORM_RADIUS makes the fillet use the
# single R1 radius for all edges; without it the API expects a per-edge Radii
# array and returns None. Tangent propagation is intentionally NOT enabled, so
# the explicit edge selection equals exactly what gets filleted.
SW_FILLET_OPT_UNIFORM_RADIUS = 2
SW_FILLET_OPT_PROPAGATE = 1        # swFeatureFilletPropagate: on along edges that run on tangentially
# swFeatureFilletVarRadiusType: a variable radius runs straight from end to end
# (without it, along a smooth curve that misses the hand calculation)
SW_FILLET_OPT_STRAIGHT_TRANSITION = 4

# Full round fillet selection marks: side face set 1, centre face set, side face set 2
SW_MARK_FULL_ROUND_SIDE_1 = 2
SW_MARK_FULL_ROUND_CENTRE = 512
SW_MARK_FULL_ROUND_SIDE_2 = 4

# swChamferType_e -- AngleDistance is a setback distance + an angle (45 deg gives
# a symmetric chamfer). EqualDistance(16) alone is a silent no-op on this build.
SW_CHAMFER_ANGLE_DISTANCE = 1
# swFeatureChamferOption_e: measure the distance and angle from each edge's other face
SW_FEATURE_CHAMFER_FLIP = 1

# swSketchSlotCreationType_e / swSketchSlotLengthType_e
SW_SLOT_CREATION_LINE = 0       # straight slot
SW_SLOT_LENGTH_CENTER = 0       # length is centre-to-centre of the end arcs

# swRefPlaneReferenceConstraints_e -- offset a new plane a fixed distance from a
# selected reference plane (for lofts: one parallel plane per profile); with
# OptionFlip the offset goes against the plane's normal.
SW_REF_PLANE_DISTANCE = 8
SW_REF_PLANE_FLIP = 256
# A plane at an angle to another about an axis it holds (add_plane): Angle to
# the plane (selected first, mark 0) and Coincident with the axis (mark 1).
SW_REF_PLANE_COINCIDENT = 4
SW_REF_PLANE_ANGLE = 16

# IFeatureManager.InsertMirrorFeature2 (verified): selection marks for what to
# mirror (features 1, a body 256) and the mirror plane (2); swFeatureScope_e.
SW_MARK_MIRROR_FEATURE = 1
SW_MARK_MIRROR_PLANE = 2
SW_MARK_MIRROR_BODY = 256
SW_FEATURE_SCOPE_ALL_BODIES = 0

# SolidWorks' own modelled thread: IFeatureManager.CreateDefinition(swFmSweepThread)
# gives an IThreadFeatureData. The profile libraries (File Locations > Thread
# Profiles) hold one configuration per valid size, e.g. 'M10x1.5'.
SW_FM_SWEEP_THREAD = 87          # swFeatureNameID_e
SW_THREAD_METHOD_CUT = 0         # swThreadMethod_e
SW_THREAD_END_BLIND = 0          # swThreadEndCondition_e
THREAD_PROFILE_EXTERNAL = "Metric Die"
THREAD_PROFILE_INTERNAL = "Metric Tap"

# Hole Wizard (IFeatureManager.HoleWizard5): SolidWorks' own standard tables.
SW_WZD_COUNTERBORE = 0             # swWzdGeneralHoleTypes_e
SW_WZD_COUNTERSINK = 1
SW_WZD_HOLE = 2
SW_WZD_TAP = 4
SW_WZD_STANDARD_ISO = 8            # swWzdHoleStandards_e
SW_ISO_SOCKET_HEAD_CAP = 139       # swWzdHoleStandardFastenerTypes_e (ISO 4762)
SW_ISO_SOCKET_COUNTERSUNK = 140    # ISO 10642
SW_ISO_SCREW_CLEARANCES = 144      # ISO 273
SW_ISO_TAPPED_HOLE = 147
SW_COSMETIC_THREAD_WITH_CALLOUT = 1  # swWzdHoleCosmeticThreadTypes_e
SCREW_FITS = {"close": 0, "normal": 1, "loose": 2}  # swWzdHoleScrewClearanceTypes_e
SW_SEL_FACES = 2                   # swSelectType_e, for IModelDocExtension.SelectByRay
SW_SEL_SKETCH_POINTS = 11          # swSelectType_e: a sketch point a dimension is attached to
SW_SEL_EXT_SKETCH_POINTS = 25      # swSelectType_e: an external one, such as the origin's
SW_HOR_LINEAR_DIMENSION = 11       # swDimensionType_e of a display dimension
SW_VERT_LINEAR_DIMENSION = 12

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
SW_PREF_DEFAULT_TEMPLATE_DRAWING = 10
SW_FILE_LOCATIONS_MATERIALS = 28   # swFileLocationsMaterialDatabases: the folders SolidWorks reads .sldmat from
MCP_MATERIAL_DATABASE = "solidworks-mcp"  # where set_material(density_kg_m3=...) materials come from

# A document's length unit (IModelDocExtension.GetUserPreferenceInteger with
# swUnitsLinear, swUserPreferenceIntegerValue_e, and no option): the tools work
# in SI, but equations are typed in these units. Names per swLengthUnit_e.
SW_UNITS_LINEAR = 47
SW_DETAILING_NO_OPTION = 0
LENGTH_UNITS = {0: "millimetres", 1: "centimetres", 2: "metres", 3: "inches", 4: "feet",
                5: "feet and inches", 6: "angstroms", 7: "nanometres", 8: "microns", 9: "mils",
                10: "microinches"}

# swConstraintType_e -- relation types for ISketchRelationManager.AddRelation,
# which (unlike the string-keyed SketchAddConstraints) returns the relation, so a
# refusal is detectable.
SW_CONSTRAINT_HORIZONTAL = 4
SW_CONSTRAINT_VERTICAL = 5
SW_CONSTRAINT_COINCIDENT = 9
SW_CONSTRAINT_FIXED = 17
SW_CONSTRAINT_HORIZONTAL_POINTS = 25   # two points on one horizontal line
SW_CONSTRAINT_VERTICAL_POINTS = 26     # two points on one vertical line
SW_CONSTRAINT_RADIUS = 3               # the relation behind a radius dimension
SW_CONSTRAINT_TANGENT = 6
SW_CONSTRAINT_COLINEAR = 27
SW_RELATIONS_ALL = 0                   # swSketchRelationFilterType_e, for GetRelations

# swTextJustification_e (IModelDoc2.InsertSketchText): the point is the text's
# lower-left corner and the text runs right from it.
SW_TEXT_JUSTIFY_LEFT = 1

# swConstrainedCornerAction_e (ISketchManager.CreateFillet): keep a rounded
# corner as a virtual sharp, so its dimensions and relations stay.
SW_CONSTRAINED_CORNER_KEEP = 1

# swConstrainedStatus_e (ISketch.GetConstrainedStatus), in the words SolidWorks
# uses in its status bar
SW_FULLY_CONSTRAINED = 3
SKETCH_STATUSES = {1: "status unknown", 2: "under defined", 3: "fully defined", 4: "over defined",
                   5: "no solution", 6: "invalid solution", 7: "autosolve off"}

# swDeleteSelectionOptions_e (IModelDocExtension.DeleteSelection2, a bitmask)
SW_DELETE_CHILDREN = 1
SW_DELETE_ABSORBED = 2   # the sketches the feature consumed go with it

# swMoveLocation_e (IModelDocExtension.ReorderFeature): the feature lands before the
# location feature, with the sketch it absorbed
SW_MOVE_BEFORE = 2

# swFeatureSuppressionAction_e / swInConfigurationOpts_e (IFeature.SetSuppression2).
# Suppressing takes the dependents along by itself; unsuppressing brings them
# back only with UNSUPPRESS_DEPENDENT (verified).
SW_SUPPRESS_FEATURE = 0
SW_UNSUPPRESS_DEPENDENT = 2
SW_THIS_CONFIGURATION = 1

# swSketchSegments_e (ISketchSegment.GetType)
SW_SKETCH_LINE = 0
SW_SKETCH_ARC = 1
SW_SKETCH_SPLINE = 3
SW_SKETCH_TEXT = 4

# swDimensionType_e (IDisplayDimension.GetType) / swDimensionDrivenState_e
SW_ANGULAR_DIMENSION = 3
SW_DIMENSION_DRIVING = 2
# swDimensionParamType_e (IDimension.GetType): its SystemValue is in radians
SW_DIMENSION_PARAM_ANGULAR = 1

# --- assemblies ---------------------------------------------------------------

# swAddComponentConfigOptions_e -- insert the component using the configuration
# that is currently selected in the part.
SW_ADD_COMPONENT_CURRENT_CONFIG = 0

# swMateType_e. Concentric takes two cylindrical faces (picked by list_faces
# index), the others two planar faces.
MATE_TYPES = {
    "coincident": 0,     # swMateCOINCIDENT
    "concentric": 1,     # swMateCONCENTRIC
    "perpendicular": 2,  # swMatePERPENDICULAR
    "parallel": 3,       # swMatePARALLEL
    "distance": 5,       # swMateDISTANCE
    "angle": 6,          # swMateANGLE
}

# swMateAlign_e -- CLOSEST lets SolidWorks keep the solution nearest the current
# position, which is what we want because every component is pre-positioned
# before it is mated.
SW_MATE_ALIGN_CLOSEST = 2

# swAddMateError_e -- note NoError is 1, not 0.
SW_ADD_MATE_NO_ERROR = 1

# swComponentSuppressionState_e (IComponent2.GetSuppression2): a suppressed
# component holds no material; a lightweight one has its geometry unloaded.
SW_COMPONENT_SUPPRESSED = 0
SW_COMPONENT_LIGHTWEIGHT_STATES = {1, 4}

# swUserPreferenceToggle_e
SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE = 10
SW_TOGGLE_STL_DONT_TRANSLATE = 71  # keep STL output in model coordinates (default: moved to positive space)
SW_TOGGLE_STL_ONE_FILE = 72        # swSTLComponentsIntoOneFile: an assembly's STL as one file, not one per part
SW_TOGGLE_DISPLAY_AXES = 4         # swDisplayAxes: a document's View > Axes
SW_TOGGLE_DISPLAY_PLANES = 5       # swDisplayPlanes: a document's View > Planes
# swMultiCAD_Enable3DInterconnect: with it on, an imported assembly arrives as ONE
# wrapped sub-assembly, so its parts cannot be listed or mated (verified)
SW_TOGGLE_3D_INTERCONNECT = 691

# swRebuildOnActivation_e (ISldWorks.ActivateDoc3): bring a document to the front as it is
SW_DONT_REBUILD_ACTIVE_DOC = 1

# swOpenDocOptions_e -- silent load, no dialogs. AddComponent5 returns None for a
# part that is not loaded yet (verified), so components are opened this way first.
SW_OPEN_DOC_SILENT = 1

# swSaveAsVersion_e
SW_SAVE_AS_CURRENT_VERSION = 0

# swSaveAsOptions_e
SW_SAVE_AS_OPTIONS_SILENT = 1

# swStandardViews_e
SW_VIEW_ISOMETRIC = 7
VIEWS = {"front": 1, "back": 2, "left": 3, "right": 4, "top": 5, "bottom": 6, "iso": SW_VIEW_ISOMETRIC}

# Drawings (make_drawing): an A4 sheet (swDwgPaperSizes_e) with the model's
# dimensions (IDrawingDoc.InsertModelAnnotations3: swImportModelItemsSource_e,
# swInsertAnnotation_e dimensions + those marked for drawings).
SW_DWG_PAPER_A4 = 6
SW_IMPORT_ENTIRE_MODEL = 0
SW_INSERT_DIMENSIONS = 8 | 32768
DRAWING_FORMATS = {"pdf", "slddrw"}

# swRayPtsOpts_e (IModelDoc2.RayIntersections): hit normals, and entry/exit
# points. Each hit comes back as 9 doubles: body, ray, hit type, x y z, nx ny nz.
SW_RAY_NORMALS_ENTRY_EXIT = 1 | 4
RAY_HIT_WIDTH = 9
SW_RAY_HIT_EXIT = 32  # swRayPtsResults_e flag in a hit's type: the ray leaves the body there

# Neutral formats open_part imports as a new part (ISldWorks.LoadFile4).
IMPORT_FORMATS = {"step", "stp", "iges", "igs", "x_t", "x_b"}

# Formats SaveAs3 can write (by extension), allow-listed for `export`. Image
# extensions (png/jpg/tif) are here because `screenshot` writes via the same
# SaveAs3 path; .bmp wrote nothing.
EXPORT_FORMATS = {"step", "stp", "stl", "iges", "igs", "x_t", "x_b", "3mf", "png", "jpg", "tif"}
