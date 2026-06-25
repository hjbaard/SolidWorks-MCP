"""Early-binding plumbing for the SolidWorks COM API.

Why this exists: GetActiveObject returns a dispatch whose GetTypeInfo() fails on
this SolidWorks build, so EnsureDispatch/CastTo cannot infer the type. Pure late
binding then breaks for model-level objects -- e.g. IModelDoc2.FirstFeature
raises DISP_E_MEMBERNOTFOUND because the object's default dispinterface does not
expose that name, even though the dispid is valid.

Fix: generate the makepy wrappers from the installed typelib once, then wrap raw
dispatches in the generated interface classes. Early-bound calls go through
InvokeTypes(dispid, ...) and bypass name resolution entirely.

All functions here must be called on the thread that owns the COM apartment
(see com_worker.ComWorker).
"""

import pythoncom
import win32com.client
from win32com.client import gencache

from .errors import SolidWorksError

# Installed SolidWorks 2026 main typelib (sldworks.tlb), version 34.0.
_SLDWORKS_TLB_GUID = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"
_SLDWORKS_TLB_LCID = 0
_SLDWORKS_TLB_MAJOR = 34
_SLDWORKS_TLB_MINOR = 0

_mod = None


def module():
    """Return the generated sldworks wrapper module (ISldWorks, IModelDoc2, ...)."""
    global _mod
    if _mod is None:
        _mod = gencache.EnsureModule(
            _SLDWORKS_TLB_GUID, _SLDWORKS_TLB_LCID, _SLDWORKS_TLB_MAJOR, _SLDWORKS_TLB_MINOR
        )
        if _mod is None:
            raise SolidWorksError(
                "Kon de SolidWorks typelib-wrappers niet laden/genereren. "
                "Is SolidWorks correct geïnstalleerd?"
            )
    return _mod


def wrap(obj, cls):
    """Re-wrap a raw or dynamic dispatch as an early-bound generated class.

    Idempotent. Returns None unchanged so callers can guard on falsy COM returns
    (many SolidWorks methods return None/False on failure without raising).
    """
    if obj is None:
        return None
    oleobj = getattr(obj, "_oleobj_", obj)
    return cls(oleobj)


def connect():
    """Attach to a running SolidWorks and return an early-bound ISldWorks.

    Raises SolidWorksError with a readable message if SolidWorks is not running.
    """
    try:
        raw = win32com.client.GetActiveObject("SldWorks.Application")
    except pythoncom.com_error as exc:
        raise SolidWorksError(
            "Geen draaiende SolidWorks gevonden. Start SolidWorks en probeer opnieuw. "
            f"(COM error: {exc})"
        )
    sw = wrap(raw, module().ISldWorks)
    sw.Visible = True
    return sw
