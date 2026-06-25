"""Verified SolidWorks enum constants.

Source: the installed swconst.tlb (SOLIDWORKS 2026, typelib v34), read directly
via scripts/introspect_api.py. Hardcoded here -- with their enum of origin -- so
the server has no runtime dependency on the makepy constant cache. Note e.g.
swDefaultTemplatePart == 8 on this build (not 9, as often stated online): always
trust the installed library over web docs.
"""

# swEndConditions_e
SW_END_COND_BLIND = 0
SW_END_COND_THROUGH_ALL = 1

# swBodyType_e
SW_BODY_SOLID = 0

# swStartConditions_e
SW_START_SKETCH_PLANE = 0

# swUserPreferenceStringValue_e
SW_PREF_DEFAULT_TEMPLATE_PART = 8

# swUserPreferenceToggle_e
SW_TOGGLE_INPUT_DIM_VAL_ON_CREATE = 10

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
