"""Introspect the *installed* SolidWorks type libraries.

Generates win32com early-binding wrappers directly from the .tlb files shipped
with this SolidWorks install. The generated modules are version-exact ground
truth for SW2026 (typelib v22) -- better than web docs, which may describe a
different release. We read real signatures from there before calling fragile
multi-arg methods like FeatureExtrusion3 (24 params).

GetActiveObject's dispatch wrapper does not expose GetTypeInfo() on this build,
so we load the typelibs from disk instead of from the running object.
"""

import sys

import pythoncom
from win32com.client import gencache

SW_DIR = r"C:\Program Files\Dassault Systemes\SOLIDWORKS 3DEXPERIENCE R2026x\SOLIDWORKS"

# Order matters: constants first, then the main interface library.
TLBS = [
    SW_DIR + r"\swconst.tlb",
    SW_DIR + r"\swpublished.tlb",
    SW_DIR + r"\sldworks.tlb",
]

WANTED_CONSTANTS = [
    "swDocPART",
    "swDocASSEMBLY",
    "swDocDRAWING",
    "swEndCondBlind",
    "swStartSketchPlane",
    "swDefaultTemplatePart",
    "swThisConfiguration",
    "swSelDATUMPLANES",
]


def main() -> int:
    print(f"gen_py path: {gencache.GetGeneratePath()}")
    for path in TLBS:
        tlb = pythoncom.LoadTypeLib(path)
        attr = tlb.GetLibAttr()  # (guid, lcid, syskind, major, minor, flags)
        guid, lcid, major, minor = attr[0], attr[1], attr[3], attr[4]
        mod = gencache.EnsureModule(guid, lcid, major, minor)
        print(f"OK {path}")
        print(f"   guid={guid} ver={major}.{minor} module={getattr(mod, '__name__', None)}")

    import win32com.client

    consts = win32com.client.constants
    for name in WANTED_CONSTANTS:
        try:
            print(f"  const {name} = {getattr(consts, name)}")
        except Exception as exc:  # noqa: BLE001
            print(f"  const {name} -> NOT FOUND ({exc})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
