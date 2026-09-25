"""The server must start and list its tools without pywin32.

MCP directories (e.g. Glama, required by awesome-mcp-servers) start a server in
a Linux container and send introspection requests before listing it. Without
Windows there is no SolidWorks, so every tool call must still fail loud.
"""

import ast
import json
import pathlib
import subprocess
import sys
import threading

# Runs the real stdio entry point in a child process with pywin32 blocked. The
# SDK is imported first with the real platform; only the server module sees
# "linux", so the pose cannot break the SDK's own platform checks.
_PROBE = r"""
import importlib.abc, sys
import mcp.server.fastmcp

class NoPywin32(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("pythoncom", "pywintypes", "win32com", "win32api"):
            raise ImportError(f"No module named {name!r} (not Windows)")
        return None

sys.meta_path.insert(0, NoPywin32())
real_platform, sys.platform = sys.platform, "linux"
from solidworks_mcp import server
sys.platform = real_platform
server.main()
"""

_REQUESTS = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "introspection-probe", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "get_status", "arguments": {}}},
]


def _declared_tools():
    source = pathlib.Path("src/solidworks_mcp/server.py").read_text(encoding="utf-8")
    return sorted(node.name for node in ast.parse(source).body
                  if isinstance(node, ast.AsyncFunctionDef) and node.decorator_list)


def _exchange(requests, last_id, timeout=60):
    """Talk to the probe like a real client: keep stdin open until the reply to
    `last_id` arrives, then close it and wait for a clean exit."""
    proc = subprocess.Popen([sys.executable, "-c", _PROBE], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    replies, stderr, answered = {}, [], threading.Event()

    def read_replies():
        for line in proc.stdout:
            message = json.loads(line)
            if "id" in message:
                replies[message["id"]] = message
                if message["id"] == last_id:
                    answered.set()
        answered.set()  # stdout closed: the server exited, stop waiting

    threading.Thread(target=read_replies, daemon=True).start()
    threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True).start()
    proc.stdin.write("".join(json.dumps(r) + "\n" for r in requests))
    proc.stdin.flush()
    answered.wait(timeout)
    proc.stdin.close()
    return replies, proc.wait(timeout), stderr


def test_server_starts_without_pywin32_and_tools_fail_loud():
    replies, returncode, stderr = _exchange(_REQUESTS, last_id=3)

    assert returncode == 0 and 3 in replies, (
        "the server does not run cleanly without pywin32, so Glama's Linux "
        f"introspection check fails and awesome-mcp-servers will not list it:\n{stderr}"
    )
    listed = sorted(t["name"] for t in replies[2]["result"]["tools"])
    assert listed == _declared_tools(), "off Windows the server lists a different tool set"
    call_text = json.dumps(replies[3]["result"])
    assert "only works on Windows" in call_text, (
        f"a tool call off Windows must fail with a readable message, got: {call_text}"
    )


def test_server_hands_every_client_its_guidelines():
    """Clients show a server's `instructions` to the model at connect time, and
    list its resources: that is how the modelling guidelines reach an agent
    without it having to find this repository first."""
    requests = _REQUESTS[:2] + [
        {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": "solidworks://guide"}},
    ]
    replies, returncode, stderr = _exchange(requests, last_id=5)

    assert returncode == 0 and 5 in replies, f"the server did not answer:\n{stderr}"
    instructions = replies[1]["result"].get("instructions") or ""
    assert "solidworks://guide" in instructions and "+z:inner" in instructions, (
        f"the connect-time instructions miss the conventions or the pointer to the guide: {instructions!r}"
    )
    listed = [r["uri"] for r in replies[4]["result"]["resources"]]
    assert "solidworks://guide" in listed, f"the guide is not listed as a resource: {listed}"
    guide = replies[5]["result"]["contents"][0]["text"]
    assert guide.startswith("# ") and "reverse-engineer" in guide.lower(), (
        "the guide resource is empty or not the modelling guide"
    )
