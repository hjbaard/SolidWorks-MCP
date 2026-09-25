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

# SolidWorks main typelib (sldworks.tlb). Its major version equals the major
# revision of the release (2026 = 34, 2025 = 33, ...), so connect() reads it from
# the running instance instead of pinning one release.
_SLDWORKS_TLB_GUID = "{83A33D31-27C5-11CE-BFD4-00400513BB57}"
_SLDWORKS_TLB_LCID = 0

_mod = None


def module():
    """Return the generated sldworks wrapper module (ISldWorks, IModelDoc2, ...)
    for the SolidWorks that connect() attached to."""
    if _mod is None:
        raise SolidWorksError("Not connected to SolidWorks yet; call connect() first.")
    return _mod


def _typelib_major(revision):
    """Typelib major version for a SolidWorks revision string ("34.3.0" -> 34)."""
    head = str(revision).split(".")[0]
    if not head.isdigit():
        raise SolidWorksError(f"Unexpected SolidWorks revision number: {revision!r}.")
    return int(head)


def _load_module(revision):
    major = _typelib_major(revision)
    try:
        mod = gencache.EnsureModule(_SLDWORKS_TLB_GUID, _SLDWORKS_TLB_LCID, major, 0)
    except pythoncom.com_error as exc:
        raise SolidWorksError(
            f"SolidWorks {revision} is running, but its type library (version {major}) is not "
            f"registered. Repair the SolidWorks installation. (COM error: {exc})"
        )
    if mod is None:
        raise SolidWorksError(
            f"Could not load or generate the SolidWorks type library wrappers (version {major}). "
            "Is SolidWorks installed correctly?"
        )
    return mod


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

    Also loads the wrapper module matching that release, which module() returns.
    Raises SolidWorksError with a readable message if SolidWorks is not running.
    """
    global _mod
    try:
        raw = win32com.client.GetActiveObject("SldWorks.Application")
    except pythoncom.com_error as exc:
        raise SolidWorksError(
            "No running SolidWorks found. Start SolidWorks and try again. "
            f"(COM error: {exc})"
        )
    # Read the revision without type info: the matching wrappers aren't loaded yet.
    revision = win32com.client.dynamic.DumbDispatch(raw._oleobj_).RevisionNumber
    _mod = _load_module(revision)
    sw = wrap(raw, _mod.ISldWorks)
    sw.Visible = True
    return sw
