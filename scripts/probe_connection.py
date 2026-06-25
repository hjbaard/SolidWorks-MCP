"""M0 connection probe.

Standalone diagnostic that verifies Python can attach to a *running*
SolidWorks instance over COM. This is intentionally NOT the MCP server:
it isolates the single question "can we talk to SolidWorks at all?" from
any server wiring, so a failure points at exactly one layer.

Run it while SolidWorks is open:
    .venv\\Scripts\\python.exe scripts\\probe_connection.py
"""

import sys

import pythoncom
import win32com.client

# swDocumentTypes_e — returned by IModelDoc2.GetType()
DOC_TYPES = {0: "none", 1: "part", 2: "assembly", 3: "drawing", 4: "sdm"}


def main() -> int:
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
    except pythoncom.com_error as exc:
        print("FAIL: kon geen draaiende SolidWorks vinden (GetActiveObject).")
        print(f"  COM error: {exc}")
        print("  -> Is SolidWorks volledig opgestart, zonder open modale dialoog?")
        return 1

    # During development we want to see every step happen live.
    sw.Visible = True

    # Late binding (GetActiveObject) exposeert RevisionNumber als property-string;
    # early binding als methode. Handel beide af.
    try:
        rev = sw.RevisionNumber
        if callable(rev):
            rev = rev()
    except Exception as exc:  # noqa: BLE001 - API faalt vaak stil; rapporteer leesbaar
        rev = f"<onbekend: {exc}>"

    print("OK: verbonden met SolidWorks")
    print(f"  RevisionNumber: {rev}")

    active = sw.ActiveDoc
    if active is None:
        print("  ActiveDoc: <geen document open>")
    else:
        try:
            title = active.GetTitle()
        except Exception:  # noqa: BLE001
            title = "<onbekend>"
        try:
            dtype = active.GetType()
        except Exception:  # noqa: BLE001
            dtype = -1
        print(f"  ActiveDoc: '{title}' (type={DOC_TYPES.get(dtype, dtype)})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
