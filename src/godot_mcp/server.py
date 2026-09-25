#!/usr/bin/env python3
"""
Godot MCP Server -- Pure Python / FastMCP
==========================================
Engineered with zero-trust security boundaries and M8ven Trust Index metadata.

28 tools covering:
  - Core runtime (14): launch editor, run project, scene ops via GDScript bridge
  - Animation text pipeline (4): keyframe quantizer, interpolation, library stitcher, track filter
  - AnimationTree blueprinting (3): state machine, transitions, blend space 2D
  - Camera & cinematic (3): bezier path, LookAt tracking, camera switcher timeline
  - NPR materials (2): batch shader uniforms, texture channel remap
  - Pipeline automation (2): Movie Maker toggle, headless diagnostics

Transport : stdio (MCP standard)
Usage     : uvx godot-mcp
Env vars  : GODOT_PATH, DEBUG
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import signal
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as err:
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP
        import warnings
        warnings.warn(
            "Running with mcp>=2 MCPServer fallback. Ensure client environment uses mcp>=1.3.0,<2 for official FastMCP schema mapping.",
            RuntimeWarning,
            stacklevel=2,
        )
    except ImportError:
        raise ImportError(
            "FastMCP framework not found. Please install mcp>=1.3.0,<2: pip install 'mcp[cli]>=1.3.0,<2'"
        ) from err

from mcp.types import ToolAnnotations


class EnhancedFastMCP(FastMCP):
    """FastMCP server with native custom_options and M8ven Trust Index metadata injection."""

    def tool(
        self,
        name: str | None = None,
        title: str | None = None,
        description: str | None = None,
        annotations: ToolAnnotations | None = None,
        icons: list | None = None,
        meta: dict[str, Any] | None = None,
        structured_output: bool | None = None,
        custom_options: dict[str, Any] | None = None,
    ):
        if custom_options is not None:
            if annotations is None:
                annotations = ToolAnnotations(**custom_options)
            if meta is None:
                meta = {}
            meta["custom_options"] = custom_options
        return super().tool(
            name=name,
            title=title,
            description=description,
            annotations=annotations,
            icons=icons,
            meta=meta,
            structured_output=structured_output,
        )


# ---------------------------------------------------------------------------
# Boot config
# ---------------------------------------------------------------------------
DEBUG: bool = os.environ.get("DEBUG", "").lower() == "true"
_GODOT_PATH_ENV: str | None = os.environ.get("GODOT_PATH")

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.WARNING,
    stream=sys.stderr,
    format="[%(levelname)s] %(message)s",
)
log = logging.getLogger("godot_mcp")

mcp = EnhancedFastMCP("godot-mcp")

# Path to the bundled GDScript engine (co-located in the package)
_SCRIPTS_DIR = Path(__file__).parent / "scripts"
_GD_SCRIPT = _SCRIPTS_DIR / "godot_operations.gd"


# ===========================================================================
# SECTION 1 -- Godot executable detection
# ===========================================================================

_godot_path_cache: str | None = None
_godot_path_lock = threading.Lock()


def _candidate_paths() -> list[str]:
    sys_platform = platform.system()
    candidates: list[str] = ["godot", "godot4"]
    if sys_platform == "Windows":
        userprofile = os.environ.get("USERPROFILE", "")
        candidates += [
            r"C:\Program Files\Godot\Godot.exe",
            r"C:\Program Files (x86)\Godot\Godot.exe",
            r"C:\Program Files\Godot_4\Godot.exe",
            f"{userprofile}\\Godot\\Godot.exe",
        ]
    elif sys_platform == "Darwin":
        home = os.environ.get("HOME", "")
        candidates += [
            "/Applications/Godot.app/Contents/MacOS/Godot",
            "/Applications/Godot_4.app/Contents/MacOS/Godot",
            f"{home}/Applications/Godot.app/Contents/MacOS/Godot",
        ]
    else:
        home = os.environ.get("HOME", "")
        candidates += [
            "/usr/bin/godot",
            "/usr/local/bin/godot",
            "/snap/bin/godot",
            f"{home}/.local/bin/godot",
        ]
    return candidates


def _test_godot(path: str) -> bool:
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=8)
        return r.returncode == 0
    except Exception:
        return False


def _find_godot() -> str:
    global _godot_path_cache
    with _godot_path_lock:
        if _godot_path_cache:
            return _godot_path_cache
        search = [_GODOT_PATH_ENV] if _GODOT_PATH_ENV else []
        search += _candidate_paths()
        for p in search:
            if p and _test_godot(p):
                log.debug("Found Godot at: %s", p)
                _godot_path_cache = p
                return p
        raise RuntimeError("Godot executable not found. Set GODOT_PATH=/path/to/godot.")


# ===========================================================================
# SECTION 2 -- Active process management
# ===========================================================================

_active: dict[str, Any] = {
    "proc": None,
    "stdout": deque(maxlen=10_000),
    "stderr": deque(maxlen=10_000),
}
_active_lock = threading.Lock()


def _drain_stream(stream, buf: deque) -> None:
    try:
        for line in iter(stream.readline, ""):
            buf.append(line.rstrip("\n"))
    except Exception:
        pass


def _launch_process(args: list[str]) -> subprocess.Popen:
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, bufsize=1)
    with _active_lock:
        _stop_active_locked()
        _active["proc"] = proc
        _active["stdout"].clear()
        _active["stderr"].clear()
    for stream, buf in [(proc.stdout, _active["stdout"]), (proc.stderr, _active["stderr"])]:
        t = threading.Thread(target=_drain_stream, args=(stream, buf), daemon=True)
        t.start()
    return proc


def _stop_active_locked() -> None:
    proc: subprocess.Popen | None = _active["proc"]
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    _active["proc"] = None


def _stop_active() -> dict:
    with _active_lock:
        stdout = list(_active["stdout"])
        stderr = list(_active["stderr"])
        _stop_active_locked()
    return {"stdout": stdout, "stderr": stderr}


def _get_output() -> dict | None:
    with _active_lock:
        if _active["proc"] is None:
            return None
        return {
            "output": list(_active["stdout"]),
            "errors": list(_active["stderr"]),
            "running": _active["proc"].poll() is None,
        }


def _atexit_cleanup(*_):
    with _active_lock:
        _stop_active_locked()


try:
    signal.signal(signal.SIGINT, _atexit_cleanup)
    signal.signal(signal.SIGTERM, _atexit_cleanup)
except (OSError, ValueError):
    pass


# ===========================================================================
# SECTION 3 -- Zero-Trust Security Guards & Helpers
# ===========================================================================

def _sanitize_path(path_str: str, base_dir: str | Path | None = None) -> Path:
    """Zero-trust path sanitization guard to completely mitigate directory traversal attacks.

    Ensures that incoming path strings contain no null bytes or path traversal tokens,
    resolves absolute paths safely, and enforces boundary containment when a base_dir is supplied.
    """
    if not path_str or not isinstance(path_str, str):
        raise ValueError("Path argument must be a non-empty string.")
    if "\0" in path_str:
        raise ValueError("Null bytes are strictly prohibited in path arguments.")

    normalized = path_str.replace("\\", "/")
    segments = normalized.split("/")
    if ".." in segments:
        raise ValueError(f"Directory traversal sequence ('..') detected in path: {path_str}")

    resolved = Path(path_str).resolve()
    if base_dir is not None:
        base_resolved = Path(base_dir).resolve()
        try:
            resolved.relative_to(base_resolved)
        except ValueError:
            raise ValueError(f"Path '{path_str}' escapes target workspace boundary '{base_dir}'.")
    return resolved


def _sanitize_project_relative_path(project_path: str, relative_path: str) -> Path:
    """Sanitizes and anchors a project-relative resource path within the project workspace boundary."""
    safe_proj = _sanitize_path(project_path)
    if not relative_path or not isinstance(relative_path, str):
        raise ValueError("Relative path argument must be a non-empty string.")
    if "\0" in relative_path:
        raise ValueError("Null bytes are strictly prohibited in path arguments.")

    clean_rel = relative_path.replace("res://", "").strip("/\\")
    segments = clean_rel.replace("\\", "/").split("/")
    if ".." in segments:
        raise ValueError(f"Directory traversal sequence ('..') detected in relative path: {relative_path}")

    full_path = (safe_proj / clean_rel).resolve()
    try:
        full_path.relative_to(safe_proj)
    except ValueError:
        raise ValueError(f"Relative path '{relative_path}' escapes project boundary '{project_path}'.")
    return full_path


def _validate_class_name(n: str) -> bool:
    return bool(n) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) is not None


def _ok(data: Any = None, message: str = "") -> str:
    """Returns a structured, descriptive JSON response string."""
    payload: dict[str, Any] = {"status": "success"}
    if message:
        payload["message"] = message
    if isinstance(data, dict):
        payload.update(data)
    elif data is not None:
        payload["result"] = data
    return json.dumps(payload, indent=2)


def _err(message: str, error_type: str = "ExecutionError") -> str:
    """Returns a structured, descriptive error JSON response string instead of throwing raw stack traces."""
    return json.dumps({
        "status": "error",
        "error_type": error_type,
        "message": message
    }, indent=2)


def _run_godot_sync(args: list[str], timeout: int = 120) -> tuple[str, str]:
    godot = _find_godot()
    try:
        r = subprocess.run([godot] + args, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Godot timed out after {timeout}s")


def _execute_gdscript_op(project_path: str, operation: str, params: dict) -> tuple[str, str]:
    """Invoke godot_operations.gd headlessly -- mirrors original TS executeOperation()."""
    if not _GD_SCRIPT.exists():
        raise FileNotFoundError(f"GDScript engine not found at {_GD_SCRIPT}")
    params_json = json.dumps(params)
    args = ["--headless", "--path", project_path,
            "--script", str(_GD_SCRIPT),
            operation, params_json, "--debug-godot"]
    return _run_godot_sync(args, timeout=60)


def _read_text(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _write_text(path: Path | str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def _uid(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()[:8]


def _res_id(type_name: str, seed: str) -> str:
    return f"{type_name}_{_uid(seed)}"


def _parse_packed_floats(s: str) -> list[float]:
    return [float(v.strip()) for v in s.split(",") if v.strip()]


def _fmt_floats(vals: list[float], precision: int = 6) -> str:
    parts = []
    for v in vals:
        s = f"{v:.{precision}f}".rstrip("0").rstrip(".")
        parts.append(s if s else "0")
    return ", ".join(parts)


def _catmull_to_bezier(pts: list[list[float]], tension: float = 0.0) -> list[list[float]]:
    """Convert Catmull-Rom waypoints to flat [in_h, position, out_h] list for Curve3D.

    Args:
        pts: List of [x, y, z] points.
        tension: Tension control variable (-1.0 to 1.0, default 0.0).
                 c = 0.0: standard uniform velocity Catmull-Rom (tangent scale = 1/6).
                 c < 0.0: loose/sweeping tangents for dramatic, hyper-fast cinematic camera moves.
                 c > 0.0: tighter/stiffer tangents for controlled, high-precision camera moves.
    """
    n = len(pts)
    result: list[list[float]] = []
    factor = (1.0 - max(-2.0, min(1.0, tension))) / 6.0
    for i in range(n):
        prev = pts[max(0, i - 1)]
        curr = pts[i]
        nxt = pts[min(n - 1, i + 1)]
        tang = [(nxt[j] - prev[j]) * factor for j in range(3)]
        in_h = [curr[j] - tang[j] for j in range(3)]
        out_h = [curr[j] + tang[j] for j in range(3)]
        result.extend([in_h, curr, out_h])
    return result


# ===========================================================================
# SECTION 4 -- Core Runtime Tools (14)
# ===========================================================================

@mcp.tool(
    name="get_godot_version",
    description="Retrieves the installed Godot 4 engine version string by querying the local Godot binary.",
    custom_options={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def get_godot_version() -> str:
    try:
        stdout, _ = _run_godot_sync(["--version"])
        return _ok({"version": stdout.strip()}, message="Godot engine version retrieved successfully.")
    except Exception as e:
        log.error("Error in get_godot_version: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="launch_editor",
    description="Spawns the Godot visual editor GUI as an independent desktop process anchored to the specified project directory.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def launch_editor(project_path: str) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        pf = safe_proj / "project.godot"
        if not pf.exists():
            return _err(f"Not a valid Godot project (project.godot missing at {safe_proj})", error_type="FileNotFoundError")
        godot = _find_godot()
        subprocess.Popen([godot, "-e", "--path", str(safe_proj)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _ok({"project_path": str(safe_proj)}, message=f"Godot editor launched successfully for {safe_proj}")
    except Exception as e:
        log.error("Error in launch_editor: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="run_project",
    description="Executes a Godot project in debug mode as a background process and captures runtime stdout/stderr streams.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)
async def run_project(project_path: str, scene: str = "") -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        pf = safe_proj / "project.godot"
        if not pf.exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        godot = _find_godot()
        args = [godot, "-d", "--path", str(safe_proj)]
        if scene:
            safe_scene = _sanitize_project_relative_path(str(safe_proj), scene)
            args.append(str(safe_scene))
        _launch_process(args)
        return _ok({
            "project_path": str(safe_proj),
            "scene": scene or "default"
        }, message="Project started in debug mode. Call get_debug_output() to stream console output.")
    except Exception as e:
        log.error("Error in run_project: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="get_debug_output",
    description="Reads currently buffered stdout and stderr log lines from the active background Godot debug process.",
    custom_options={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_debug_output() -> str:
    try:
        data = _get_output()
        if data is None:
            return _err("No active Godot process found. Invoke run_project() first.", error_type="ProcessNotRunningError")
        return _ok(data, message="Debug output buffer retrieved successfully.")
    except Exception as e:
        log.error("Error in get_debug_output: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="stop_project",
    description="Terminates the actively running Godot debug process and returns its final flushed console output.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def stop_project() -> str:
    try:
        with _active_lock:
            if _active["proc"] is None:
                return _err("No active Godot process is currently running.", error_type="ProcessNotRunningError")
        data = _stop_active()
        return _ok(data, message="Godot process terminated successfully.")
    except Exception as e:
        log.error("Error in stop_project: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="list_projects",
    description="Discovers and lists Godot project directories containing a valid project.godot file within a target directory.",
    custom_options={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def list_projects(directory: str, recursive: bool = False) -> str:
    try:
        safe_dir = _sanitize_path(directory)
        if not safe_dir.exists():
            return _err(f"Target directory does not exist: {safe_dir}", error_type="DirectoryNotFoundError")
        projects: list[dict] = []
        if recursive:
            for p in safe_dir.rglob("project.godot"):
                d = p.parent
                projects.append({"path": str(d), "name": d.name})
        else:
            if (safe_dir / "project.godot").exists():
                projects.append({"path": str(safe_dir), "name": safe_dir.name})
            for child in safe_dir.iterdir():
                if child.is_dir() and (child / "project.godot").exists():
                    projects.append({"path": str(child), "name": child.name})
        return _ok({"projects": projects, "count": len(projects)}, message=f"Found {len(projects)} Godot projects.")
    except Exception as e:
        log.error("Error in list_projects: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="get_project_info",
    description="Inspects project.godot and project tree to return metadata, asset counts, script inventory, and engine compatibility.",
    custom_options={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_project_info(project_path: str) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        pf = safe_proj / "project.godot"
        if not pf.exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        name = safe_proj.name
        try:
            cfg = _read_text(pf)
            m = re.search(r'config/name="([^"]+)"', cfg)
            if m:
                name = m.group(1)
        except Exception:
            pass
        ext_counts: dict[str, int] = {}
        for f in safe_proj.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                ext = f.suffix.lstrip(".") or "other"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
        structure = {
            "scenes": ext_counts.get("tscn", 0),
            "scripts": ext_counts.get("gd", 0),
            "resources": ext_counts.get("tres", 0),
            "assets_png": ext_counts.get("png", 0),
        }
        try:
            stdout, _ = _run_godot_sync(["--version"])
            godot_version = stdout.strip()
        except Exception:
            godot_version = "unknown"
        return _ok({
            "name": name,
            "path": str(safe_proj),
            "godot_version": godot_version,
            "structure": structure
        }, message="Project metadata retrieved successfully.")
    except Exception as e:
        log.error("Error in get_project_info: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="create_scene",
    description="Creates a new Godot scene (.tscn) file with a validated root node type using the headless GDScript bridge.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def create_scene(project_path: str, scene_path: str, root_node_type: str = "Node2D") -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        _sanitize_project_relative_path(str(safe_proj), scene_path)
        if not _validate_class_name(root_node_type):
            return _err(f"Invalid root_node_type: '{root_node_type}'. Must be a valid identifier.", error_type="ValueError")
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        stdout, stderr = _execute_gdscript_op(str(safe_proj), "create_scene", {
            "scene_path": scene_path, "root_node_type": root_node_type,
        })
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript bridge error: {stderr.strip()}", error_type="GDScriptBridgeError")
        return _ok({
            "scene_path": scene_path,
            "root_node_type": root_node_type,
            "output": stdout.strip()
        }, message=f"Scene created successfully at {scene_path}")
    except Exception as e:
        log.error("Error in create_scene: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="add_node",
    description="Instantiates and appends a node of specified Godot class and property dictionary into an existing scene.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def add_node(
    project_path: str,
    scene_path: str,
    node_type: str,
    node_name: str,
    parent_node_path: str = "root",
    properties: dict | None = None,
) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        _sanitize_project_relative_path(str(safe_proj), scene_path)
        if not _validate_class_name(node_type):
            return _err(f"Invalid node_type: '{node_type}'. Must be a valid identifier.", error_type="ValueError")
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        params: dict[str, Any] = {
            "scene_path": scene_path, "node_type": node_type,
            "node_name": node_name, "parent_node_path": parent_node_path,
        }
        if properties:
            params["properties"] = properties
        stdout, stderr = _execute_gdscript_op(str(safe_proj), "add_node", params)
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript bridge error: {stderr.strip()}", error_type="GDScriptBridgeError")
        return _ok({
            "scene_path": scene_path,
            "node_name": node_name,
            "node_type": node_type,
            "output": stdout.strip()
        }, message=f"Node '{node_name}' added to {scene_path}")
    except Exception as e:
        log.error("Error in add_node: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="load_sprite",
    description="Loads a texture resource and binds it to a 2D or 3D sprite node within a scene file.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def load_sprite(project_path: str, scene_path: str, node_path: str, texture_path: str) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        _sanitize_project_relative_path(str(safe_proj), scene_path)
        _sanitize_project_relative_path(str(safe_proj), texture_path)
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        stdout, stderr = _execute_gdscript_op(str(safe_proj), "load_sprite", {
            "scene_path": scene_path, "node_path": node_path, "texture_path": texture_path,
        })
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript bridge error: {stderr.strip()}", error_type="GDScriptBridgeError")
        return _ok({
            "scene_path": scene_path,
            "node_path": node_path,
            "texture_path": texture_path,
            "output": stdout.strip()
        }, message=f"Texture bound successfully on {node_path}")
    except Exception as e:
        log.error("Error in load_sprite: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="export_mesh_library",
    description="Extracts 3D mesh instances from a scene and compiles them into a MeshLibrary (.res) resource for GridMap tiles.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def export_mesh_library(
    project_path: str, scene_path: str, output_path: str,
    mesh_item_names: list[str] | None = None,
) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        _sanitize_project_relative_path(str(safe_proj), scene_path)
        _sanitize_project_relative_path(str(safe_proj), output_path)
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        params: dict[str, Any] = {"scene_path": scene_path, "output_path": output_path}
        if mesh_item_names:
            params["mesh_item_names"] = mesh_item_names
        stdout, stderr = _execute_gdscript_op(str(safe_proj), "export_mesh_library", params)
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript bridge error: {stderr.strip()}", error_type="GDScriptBridgeError")
        return _ok({
            "output_path": output_path,
            "output": stdout.strip()
        }, message=f"MeshLibrary compiled to {output_path}")
    except Exception as e:
        log.error("Error in export_mesh_library: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="save_scene",
    description="Persists scene modifications or creates a variant scene copy at a new relative path.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def save_scene(project_path: str, scene_path: str, new_path: str = "") -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        _sanitize_project_relative_path(str(safe_proj), scene_path)
        if new_path:
            _sanitize_project_relative_path(str(safe_proj), new_path)
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        params: dict[str, Any] = {"scene_path": scene_path}
        if new_path:
            params["new_path"] = new_path
        stdout, stderr = _execute_gdscript_op(str(safe_proj), "save_scene", params)
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript bridge error: {stderr.strip()}", error_type="GDScriptBridgeError")
        target = new_path or scene_path
        return _ok({"target_path": target, "output": stdout.strip()}, message=f"Scene saved successfully to {target}")
    except Exception as e:
        log.error("Error in save_scene: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="get_uid",
    description="Reads and returns the Godot 4.4+ unique resource identifier (UID) for a specified asset file.",
    custom_options={
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def get_uid(project_path: str, file_path: str) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        _sanitize_project_relative_path(str(safe_proj), file_path)
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        stdout, _ = _run_godot_sync(["--version"])
        ver = stdout.strip()
        m = re.match(r"(\d+)\.(\d+)", ver)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            if not (major > 4 or (major == 4 and minor >= 4)):
                return _err(f"UID queries require Godot 4.4+. Detected: {ver}", error_type="VersionIncompatibleError")
        stdout2, stderr2 = _execute_gdscript_op(str(safe_proj), "get_uid", {"file_path": file_path})
        return _ok({"output": stdout2.strip(), "log": stderr2.strip()}, message=f"UID query completed for {file_path}")
    except Exception as e:
        log.error("Error in get_uid: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="update_project_uids",
    description="Recursively iterates all project scenes, scripts, and shaders to regenerate and update missing Godot 4.4+ UIDs.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def update_project_uids(project_path: str) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        if not (safe_proj / "project.godot").exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        stdout, stderr = _execute_gdscript_op(
            str(safe_proj), "resave_resources", {"project_path": str(safe_proj)}
        )
        return _ok({"output": stdout.strip()}, message="Project resource UID resave completed.")
    except Exception as e:
        log.error("Error in update_project_uids: %s", e)
        return _err(str(e), error_type=type(e).__name__)


# ===========================================================================
# SECTION 5 -- Animation Text-Manipulation Pipeline Tools (4)
# ===========================================================================

@mcp.tool(
    name="quantize_animation_keyframes",
    description="Parses raw keyframe times in a .tscn or .tres animation and quantizes them to discrete frame steps (e.g. 12-fps stepped anime look).",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def quantize_animation_keyframes(
    file_path: str,
    target_fps: int = 12,
    animation_name: str = "",
) -> str:
    try:
        p = _sanitize_path(file_path)
        if not p.exists():
            return _err(f"File not found: {p}", error_type="FileNotFoundError")
        content = _read_text(p)
        frame_dur = 1.0 / max(1, target_fps)
        changes = 0

        def _round_times(m: re.Match) -> str:
            nonlocal changes
            vals = _parse_packed_floats(m.group(1))
            new_vals = [round(round(v / frame_dur) * frame_dur, 6) for v in vals]
            if new_vals != vals:
                changes += 1
            return f'"times": PackedFloat32Array({_fmt_floats(new_vals)})'

        pattern = re.compile(r'"times":\s*PackedFloat32Array\(([^)]+)\)', re.MULTILINE)
        new_content = pattern.sub(_round_times, content)
        _write_text(p, new_content)
        return _ok({
            "file": str(p),
            "target_fps": target_fps,
            "frame_duration_s": round(frame_dur, 6),
            "arrays_modified": changes,
        }, message=f"Quantized keyframe times to {target_fps} fps ({changes} array tracks modified)")
    except Exception as e:
        log.error("Error in quantize_animation_keyframes: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="set_animation_interpolation_mode",
    description="Bulk-edits animation track interpolation modes (nearest/constant, linear, cubic) in a scene or resource file.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def set_animation_interpolation_mode(
    file_path: str,
    mode: str = "linear",
    track_index: int = -1,
) -> str:
    MODE_MAP = {
        "nearest": 0, "constant": 0,
        "linear": 1,
        "cubic": 2,
        "linear_angle": 3,
        "cubic_angle": 4,
    }
    mode_int = MODE_MAP.get(mode.lower())
    if mode_int is None:
        return _err(f"Unknown interpolation mode '{mode}'. Choose from: {', '.join(MODE_MAP)}", error_type="ValueError")
    try:
        p = _sanitize_path(file_path)
        if not p.exists():
            return _err(f"File not found: {p}", error_type="FileNotFoundError")
        content = _read_text(p)
        changes = 0

        def _repl(m: re.Match) -> str:
            nonlocal changes
            if track_index >= 0 and int(m.group(2)) != track_index:
                return m.group(0)
            if int(m.group(4)) != mode_int:
                changes += 1
            return f"{m.group(1)}{m.group(2)}{m.group(3)}{mode_int}"

        new_content = re.sub(r"(tracks/)(\d+)(/interp\s*=\s*)(\d+)", _repl, content)
        _write_text(p, new_content)
        return _ok({
            "file": str(p),
            "mode": mode,
            "mode_value": mode_int,
            "tracks_modified": changes
        }, message=f"Interpolation mode set to '{mode}' across {changes} tracks.")
    except Exception as e:
        log.error("Error in set_animation_interpolation_mode: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="stitch_animation_library",
    description="Compiles external animation clip files into a unified master AnimationLibrary (.tres) resource referencing named animations.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def stitch_animation_library(output_res_path: str, clips: list[dict]) -> str:
    try:
        out_p = _sanitize_path(output_res_path)
        if not clips:
            return _err("Clips list cannot be empty.", error_type="ValueError")
        load_steps = len(clips) + 1
        lines = [f'[gd_resource type="AnimationLibrary" load_steps={load_steps} format=3]\n']
        ext_ids: list[str] = []
        for i, clip in enumerate(clips):
            name = clip.get("name", f"clip_{i}")
            path = clip.get("path", "")
            if not path:
                return _err(f"Clip at index {i} missing required 'path' field.", error_type="ValueError")
            if "\0" in path or ".." in path.split("/"):
                return _err(f"Invalid characters or traversal in clip path: {path}", error_type="ValueError")
            rid = f"{i + 1}_{_uid(path + name)}"
            ext_ids.append(rid)
            lines.append(f'[ext_resource type="Animation" path="{path}" id="{rid}"]')
        lines.append("\n[resource]")
        data_entries = ", ".join(
            f'"{clip.get("name", f"clip_{i}")}": ExtResource("{ext_ids[i]}")'
            for i, clip in enumerate(clips)
        )
        lines.append(f"_data = {{{data_entries}}}\n")
        content = "\n".join(lines)
        _write_text(out_p, content)
        return _ok({
            "output_res_path": str(out_p),
            "clips_stitched": len(clips)
        }, message=f"AnimationLibrary with {len(clips)} clips successfully generated at {out_p}")
    except Exception as e:
        log.error("Error in stitch_animation_library: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="generate_track_filter",
    description="Generates an AnimationNodeBlendFilter resource block and configuration code to isolate upper-body bones from locked lower-body bones.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def generate_track_filter(
    scene_path: str,
    locked_bone_paths: list[str],
    blend_node_name: str = "UpperBodyFilter",
) -> str:
    try:
        p = _sanitize_path(scene_path)
        rid = _res_id("AnimationNodeBlendFilter", str(p) + blend_node_name)
        filter_paths = ", ".join(f'NodePath("{bp}")' for bp in locked_bone_paths)
        block = (
            f'\n; -- Track filter: {blend_node_name} (generated by godot-mcp) --\n'
            f'[sub_resource type="AnimationNodeBlendFilter" id="{rid}"]\n'
            f'filter_enabled = true\n'
            f'filters = [{filter_paths}]\n'
        )
        script = (
            f'# GDScript to configure filter in AnimationTree:\n'
            f'var filter := AnimationNodeBlendFilter.new()\n'
            f'filter.filter_enabled = true\n'
            + "".join(f'filter.add_filter_path(NodePath("{bp}"), true)\n'
                      for bp in locked_bone_paths)
            + f'blend_tree.add_node("{blend_node_name}", filter)\n'
        )
        appended = False
        if p.exists():
            existing = _read_text(p)
            _write_text(p, existing.rstrip() + "\n" + block)
            appended = True
        return _ok({
            "scene_path": str(p),
            "blend_node_name": blend_node_name,
            "resource_id": rid,
            "appended_to_scene": appended,
            "gdscript_snippet": script,
            "resource_block": block,
        }, message=f"AnimationNodeBlendFilter '{blend_node_name}' synthesized successfully.")
    except Exception as e:
        log.error("Error in generate_track_filter: %s", e)
        return _err(str(e), error_type=type(e).__name__)


# ===========================================================================
# SECTION 6 -- AnimationTree & State Machine Blueprinting (3)
# ===========================================================================

@mcp.tool(
    name="generate_state_machine",
    description="Injects an AnimationNodeStateMachine sub-resource graph with layout coordinates and state nodes into a target .tscn scene file.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def generate_state_machine(
    scene_path: str,
    node_name: str,
    states: list[dict],
    anim_player_path: str = "AnimationPlayer",
    start_state: str = "",
) -> str:
    try:
        p = _sanitize_path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {p}", error_type="FileNotFoundError")
        if not states:
            return _err("States list cannot be empty.", error_type="ValueError")

        start = start_state or states[0]["name"]
        sm_id = _res_id("AnimationNodeStateMachine", str(p) + node_name)
        anim_node_ids: dict[str, str] = {}
        sub_blocks: list[str] = []
        for state in states:
            sname = state["name"]
            anim = state.get("animation", sname.lower())
            aid = _res_id("AnimationNodeAnimation", str(p) + sname)
            anim_node_ids[sname] = aid
            sub_blocks.append(
                f'[sub_resource type="AnimationNodeAnimation" id="{aid}"]\n'
                f'animation = &"{anim}"\n'
            )
        sm_lines = [f'[sub_resource type="AnimationNodeStateMachine" id="{sm_id}"]']
        for i, state in enumerate(states):
            sname = state["name"]
            pos = state.get("position", [100 + i * 200, 150])
            sm_lines.append(f'states/{sname}/node = SubResource("{anim_node_ids[sname]}")')
            sm_lines.append(f'states/{sname}/position = Vector2({pos[0]}, {pos[1]})')
        sm_lines += [
            "transitions = []",
            f'start_node = &"{start}"',
            'end_node = &"End"',
            'graph_offset = Vector2(0, 0)',
        ]
        tree_node = (
            f'[node name="{node_name}" type="AnimationTree" parent="."]\n'
            f'tree_root = SubResource("{sm_id}")\n'
            f'anim_player = NodePath("{anim_player_path}")\n'
            f'active = true\n'
        )
        block = "\n".join(sub_blocks) + "\n" + "\n".join(sm_lines) + "\n\n" + tree_node
        existing = _read_text(p)
        _write_text(p, existing.rstrip() + "\n\n" + block)
        return _ok({
            "scene_path": str(p),
            "node_name": node_name,
            "state_machine_id": sm_id,
            "start_state": start,
            "states_count": len(states)
        }, message=f"AnimationTree '{node_name}' with {len(states)} states injected into {p.name}")
    except Exception as e:
        log.error("Error in generate_state_machine: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="compile_state_transitions",
    description="Serializes and injects transition logic connections, advance conditions, and crossfade times into an existing AnimationNodeStateMachine block.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def compile_state_transitions(
    scene_path: str,
    state_machine_resource_id: str,
    transitions: list[dict],
) -> str:
    try:
        p = _sanitize_path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {p}", error_type="FileNotFoundError")
        content = _read_text(p)
        if state_machine_resource_id not in content:
            return _err(f"Resource id '{state_machine_resource_id}' not found in scene.", error_type="ResourceNotFoundError")
        t_list: list[str] = []
        for t in transitions:
            frm = t.get("from", "")
            to = t.get("to", "")
            cond = t.get("condition", "")
            xfade = float(t.get("xfade_time", 0.2))
            advance = int(t.get("advance_mode", 1))
            t_list.append(
                '{"advance_condition": &"' + cond + '", "advance_expression": "", '
                '"advance_mode": ' + str(advance) + ', "break_loop_at_end": false, '
                '"from": &"' + frm + '", "reset": true, "to": &"' + to + '", '
                '"xfade_curve": null, "xfade_time": ' + f"{xfade:.3f}" + '}'
            )
        transitions_str = "transitions = [" + ", ".join(t_list) + "]"
        new_content = re.sub(r"transitions\s*=\s*\[\]", transitions_str, content, count=1)
        if new_content == content:
            new_content = content.replace(
                f'[sub_resource type="AnimationNodeStateMachine" id="{state_machine_resource_id}"]',
                f'[sub_resource type="AnimationNodeStateMachine" id="{state_machine_resource_id}"]\n{transitions_str}',
                1,
            )
        _write_text(p, new_content)
        return _ok({
            "scene_path": str(p),
            "state_machine_resource_id": state_machine_resource_id,
            "transitions_compiled": len(transitions)
        }, message=f"Compiled {len(transitions)} state transitions into {state_machine_resource_id}")
    except Exception as e:
        log.error("Error in compile_state_transitions: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="calculate_blend_space_2d",
    description="Computes and injects an AnimationNodeBlendSpace2D sub-resource for directional movement blending based on 2D controller coordinates.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def calculate_blend_space_2d(
    scene_path: str,
    node_name: str,
    blend_points: list[dict],
    x_label: str = "Horizontal",
    y_label: str = "Vertical",
    anim_player_path: str = "AnimationPlayer",
) -> str:
    try:
        p = _sanitize_path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {p}", error_type="FileNotFoundError")
        bs_id = _res_id("AnimationNodeBlendSpace2D", str(p) + node_name)
        anim_ids: list[str] = []
        anim_blocks: list[str] = []
        for i, bp in enumerate(blend_points):
            anim = bp.get("animation", f"anim_{i}")
            aid = _res_id("AnimationNodeAnimation", str(p) + node_name + anim)
            anim_ids.append(aid)
            anim_blocks.append(
                f'[sub_resource type="AnimationNodeAnimation" id="{aid}"]\n'
                f'animation = &"{anim}"\n'
            )
        bs_lines = [f'[sub_resource type="AnimationNodeBlendSpace2D" id="{bs_id}"]']
        for i, bp in enumerate(blend_points):
            pos = bp.get("position", [0.0, 0.0])
            bs_lines.append(f'blend_points/{i}/node = SubResource("{anim_ids[i]}")')
            bs_lines.append(f'blend_points/{i}/pos = Vector2({pos[0]}, {pos[1]})')
        bs_lines += [
            "triangles = PackedInt32Array()",
            "min_space = Vector2(-1, -1)",
            "max_space = Vector2(1, 1)",
            "snap = Vector2(0.1, 0.1)",
            f'x_label = "{x_label}"',
            f'y_label = "{y_label}"',
            "auto_triangles = true",
        ]
        tree_node = (
            f'[node name="{node_name}" type="AnimationTree" parent="."]\n'
            f'tree_root = SubResource("{bs_id}")\n'
            f'anim_player = NodePath("{anim_player_path}")\n'
            f'active = true\n'
        )
        block = "\n".join(anim_blocks) + "\n" + "\n".join(bs_lines) + "\n\n" + tree_node
        existing = _read_text(p)
        _write_text(p, existing.rstrip() + "\n\n" + block)
        return _ok({
            "scene_path": str(p),
            "node_name": node_name,
            "blend_space_id": bs_id,
            "points_count": len(blend_points)
        }, message=f"BlendSpace2D '{node_name}' with {len(blend_points)} points injected into {p.name}")
    except Exception as e:
        log.error("Error in calculate_blend_space_2d: %s", e)
        return _err(str(e), error_type=type(e).__name__)


# ===========================================================================
# SECTION 7 -- Camera & Cinematic Tools (3)
# ===========================================================================

@mcp.tool(
    name="generate_bezier_camera_path",
    description="Generates a 3D Catmull-Rom bezier camera rail (Path3D, Curve3D, PathFollow3D) with adjustable speed-curve tension multiplier in a .tscn scene.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def generate_bezier_camera_path(
    scene_path: str,
    path_node_name: str,
    waypoints: list[dict],
    add_path_follow: bool = True,
    add_camera: bool = True,
    tension: float = 0.0,
) -> str:
    try:
        p = _sanitize_path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {p}", error_type="FileNotFoundError")
        if len(waypoints) < 2:
            return _err("At least 2 waypoints are required to construct a camera path.", error_type="ValueError")
        pts = [wp["position"] for wp in waypoints]
        tilts = [float(wp.get("tilt", 0.0)) for wp in waypoints]
        handles = _catmull_to_bezier(pts, tension=tension)

        def _v3(v: list[float]) -> str:
            return f"{v[0]:.4f}, {v[1]:.4f}, {v[2]:.4f}"

        flat_pts = ", ".join(_v3(h) for h in handles)
        flat_tilts = ", ".join(f"{t:.4f}" for t in tilts)
        curve_id = _res_id("Curve3D", str(p) + path_node_name)
        curve_block = (
            f'[sub_resource type="Curve3D" id="{curve_id}"]\n'
            f'_data = {{\n'
            f'"points": PackedVector3Array({flat_pts}),\n'
            f'"tilts": PackedFloat32Array({flat_tilts})\n'
            f'}}\n'
        )
        path_node = (
            f'[node name="{path_node_name}" type="Path3D" parent="."]\n'
            f'curve = SubResource("{curve_id}")\n'
        )
        follow_node = ""
        if add_path_follow:
            follow_node = (
                f'[node name="PathFollow3D" type="PathFollow3D" '
                f'parent="{path_node_name}"]\nloop = false\n'
            )
        cam_node = ""
        if add_camera and add_path_follow:
            cam_node = (
                f'[node name="Camera3D" type="Camera3D" '
                f'parent="{path_node_name}/PathFollow3D"]\n'
            )
        block = curve_block + "\n" + path_node + follow_node + cam_node
        existing = _read_text(p)
        _write_text(p, existing.rstrip() + "\n\n" + block)
        return _ok({
            "scene_path": str(p),
            "path_node_name": path_node_name,
            "curve_id": curve_id,
            "waypoints_count": len(waypoints),
            "tension": tension,
        }, message=f"Bezier camera path '{path_node_name}' synthesized with {len(waypoints)} waypoints (tension={tension})")
    except Exception as e:
        log.error("Error in generate_bezier_camera_path: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="inject_lookat_tracking",
    description="Generates and attaches a procedural slerp look-at tracking GDScript to a Camera3D node to follow target Node3D transforms.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def inject_lookat_tracking(
    scene_path: str,
    camera_node_path: str,
    target_node_path: str,
    script_output_path: str = "",
) -> str:
    try:
        p = _sanitize_path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {p}", error_type="FileNotFoundError")
        gd_script = (
            "# LookAt tracking script -- auto-generated by godot-mcp\n"
            "extends Camera3D\n\n"
            "## Target node to track. Assign in the Inspector or via code.\n"
            "@export var target: Node3D\n"
            "@export var offset := Vector3.ZERO\n"
            "@export var smooth_speed := 5.0\n\n"
            "func _ready() -> void:\n"
            "\tif target == null:\n"
            f'\t\ttarget = get_node_or_null("{target_node_path}")\n\n'
            "func _process(delta: float) -> void:\n"
            "\tif target == null:\n"
            "\t\treturn\n"
            "\tvar look_target := target.global_position + offset\n"
            "\tvar saved_basis := global_transform.basis\n"
            "\tlook_at(look_target, Vector3.UP)\n"
            "\tglobal_transform.basis = saved_basis.slerp(\n"
            "\t\tglobal_transform.basis, clampf(smooth_speed * delta, 0.0, 1.0)\n"
            "\t)\n"
        )
        if not script_output_path:
            script_path = p.parent / "camera_lookat_tracker.gd"
        else:
            script_path = _sanitize_path(script_output_path)
        _write_text(script_path, gd_script)
        return _ok({
            "scene_path": str(p),
            "script_path": str(script_path),
            "camera_node": camera_node_path,
            "target_node": target_node_path,
        }, message=f"LookAt tracking script generated and anchored at {script_path}")
    except Exception as e:
        log.error("Error in inject_lookat_tracking: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="build_camera_switcher_timeline",
    description="Synthesizes an Animation resource that toggles Camera3D.current across multiple cameras at defined cut timestamps for cinematic editing.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def build_camera_switcher_timeline(
    scene_path: str,
    anim_player_node: str,
    switches: list[dict],
    animation_name: str = "CameraTimeline",
    timeline_length: float = 10.0,
) -> str:
    try:
        p = _sanitize_path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {p}", error_type="FileNotFoundError")
        anim_id = _res_id("Animation", str(p) + animation_name)
        track_lines: list[str] = []
        for i, sw in enumerate(switches):
            cam_path = sw.get("camera_path", f"Camera3D_{i}")
            activate = float(sw.get("activate_at", 0.0))
            deactivate = sw.get("deactivate_at")
            times: list[float] = [activate]
            vals: list[str] = ["true"]
            if deactivate is not None:
                times.append(float(deactivate))
                vals.append("false")
            times_str = ", ".join(f"{t:.4f}" for t in times)
            trans_str = ", ".join("1" for _ in times)
            vals_str = ", ".join(vals)
            track_lines.append(
                f'tracks/{i}/type = "value"\n'
                f'tracks/{i}/path = NodePath("{cam_path}:current")\n'
                f'tracks/{i}/interp = 0\n'
                f'tracks/{i}/loop_wrap = false\n'
                f'tracks/{i}/keys = {{\n'
                f'"times": PackedFloat32Array({times_str}),\n'
                f'"transitions": PackedFloat32Array({trans_str}),\n'
                f'"update": 1,\n'
                f'"values": [{vals_str}]\n'
                f'}}\n'
            )
        anim_block = (
            f'[sub_resource type="Animation" id="{anim_id}"]\n'
            f'resource_name = "{animation_name}"\n'
            f'length = {timeline_length:.4f}\n'
            f'loop_mode = 0\nstep = 0.0333\n'
            + "\n".join(track_lines)
        )
        lib_id = _res_id("AnimationLibrary", str(p) + "SwitcherLib")
        lib_block = (
            f'[sub_resource type="AnimationLibrary" id="{lib_id}"]\n'
            f'_data = {{"{animation_name}": SubResource("{anim_id}")}}\n'
        )
        block = anim_block + "\n" + lib_block
        existing = _read_text(p)
        _write_text(p, existing.rstrip() + "\n\n" + block)
        return _ok({
            "scene_path": str(p),
            "animation_name": animation_name,
            "animation_id": anim_id,
            "duration_s": timeline_length,
            "cuts_count": len(switches)
        }, message=f"Camera switcher timeline '{animation_name}' compiled with {len(switches)} cuts.")
    except Exception as e:
        log.error("Error in build_camera_switcher_timeline: %s", e)
        return _err(str(e), error_type=type(e).__name__)


# ===========================================================================
# SECTION 8 -- NPR Materials & Shader Tools (2)
# ===========================================================================

@mcp.tool(
    name="batch_shader_uniforms",
    description="Batch scans .tres ShaderMaterial files in a directory and overwrites shader_parameter/* values for global NPR look adjustments.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def batch_shader_uniforms(
    directory: str,
    uniform_overrides: dict,
    recursive: bool = True,
    dry_run: bool = False,
) -> str:
    try:
        root = _sanitize_path(directory)
        if not root.exists():
            return _err(f"Directory not found: {root}", error_type="DirectoryNotFoundError")
        glob = "**/*.tres" if recursive else "*.tres"
        files = list(root.glob(glob))
        results: list[dict] = []
        total_changes = 0
        for f in files:
            try:
                content = _read_text(f)
                if "shader_parameter" not in content:
                    continue
                new_content = content
                file_changes = 0
                for param, value in uniform_overrides.items():
                    pat = re.compile(
                        rf'(shader_parameter/{re.escape(param)}\s*=\s*)(.+)',
                        re.MULTILINE
                    )
                    replaced, n = pat.subn(lambda m, v=str(value): m.group(1) + v, new_content)
                    if n:
                        new_content = replaced
                        file_changes += n
                if file_changes:
                    total_changes += file_changes
                    results.append({"file": str(f.relative_to(root)), "changes": file_changes})
                    if not dry_run:
                        _write_text(f, new_content)
            except Exception as ex:
                results.append({"file": str(f), "error": str(ex)})
        return _ok({
            "directory": str(root),
            "files_scanned": len(files),
            "files_modified": len([r for r in results if "changes" in r]),
            "total_replacements": total_changes,
            "dry_run": dry_run,
            "details": results,
        }, message=f"Batch uniform overrides completed across {len(results)} files ({total_changes} parameters modified).")
    except Exception as e:
        log.error("Error in batch_shader_uniforms: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="remap_texture_channels",
    description="Rewires texture slot properties and manages ext_resource references in a .tres material file for hand-painted art workflows.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def remap_texture_channels(material_path: str, channel_map: dict) -> str:
    try:
        p = _sanitize_path(material_path)
        if not p.exists():
            return _err(f"Material file not found: {p}", error_type="FileNotFoundError")
        content = _read_text(p)
        new_content = content
        ls_match = re.search(r'load_steps=(\d+)', content)
        load_steps = int(ls_match.group(1)) if ls_match else 1
        existing_ids = len(re.findall(r'\[ext_resource', content))
        next_id_num = existing_ids + 1
        ext_ids: dict[str, str] = {}
        changes: list[dict] = []
        for prop, res_path in channel_map.items():
            if not isinstance(res_path, str):
                continue
            if not res_path.startswith("res://"):
                res_path = "res://" + res_path
            if res_path not in ext_ids:
                rid = f"{next_id_num}_{_uid(res_path)}"
                ext_ids[res_path] = rid
                next_id_num += 1
                hdr_match = re.search(r'(\[gd_resource[^\]]+\])', new_content)
                if hdr_match:
                    pos = hdr_match.end()
                    insert = f'\n[ext_resource type="Texture2D" path="{res_path}" id="{rid}"]'
                    new_content = new_content[:pos] + insert + new_content[pos:]
            rid = ext_ids[res_path]
            new_line = f'{prop} = ExtResource("{rid}")'
            pat = re.compile(rf'^{re.escape(prop)}\s*=.*$', re.MULTILINE)
            if pat.search(new_content):
                new_content = pat.sub(new_line, new_content)
                changes.append({"property": prop, "path": res_path, "action": "replaced"})
            else:
                new_content = new_content.rstrip() + f'\n{new_line}\n'
                changes.append({"property": prop, "path": res_path, "action": "appended"})
        new_count = load_steps + len(ext_ids)
        new_content = re.sub(r'load_steps=\d+', f'load_steps={new_count}', new_content, count=1)
        _write_text(p, new_content)
        return _ok({
            "material_path": str(p),
            "changes": changes,
            "new_ext_resources": len(ext_ids)
        }, message=f"Successfully remapped {len(changes)} texture channels in {p.name}")
    except Exception as e:
        log.error("Error in remap_texture_channels: %s", e)
        return _err(str(e), error_type=type(e).__name__)


# ===========================================================================
# SECTION 9 -- Pipeline Automation Tools (2)
# ===========================================================================

@mcp.tool(
    name="toggle_movie_maker",
    description="Configures project.godot to toggle Godot Movie Maker mode, locking render time-steps for deterministic video and frame sequence capture.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)
async def toggle_movie_maker(
    project_path: str,
    enable: bool = True,
    output_file: str = "res://render/output.avi",
    fps: int = 60,
) -> str:
    try:
        safe_proj = _sanitize_path(project_path)
        pf = safe_proj / "project.godot"
        if not pf.exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        content = _read_text(pf)
        content = re.sub(r'movie_writer/[^\n]+\n?', '', content)
        if enable:
            settings = (
                f'movie_writer/movie_file="{output_file}"\n'
                f'movie_writer/fps={fps}\n'
                f'movie_writer/mix_rate=48000\n'
            )
            if "[editor]" in content:
                content = content.replace("[editor]\n", f"[editor]\n{settings}", 1)
            else:
                content = content.rstrip() + f"\n\n[editor]\n{settings}\n"
        _write_text(pf, content)
        return _ok({
            "project_path": str(safe_proj),
            "movie_maker_enabled": enable,
            "output_file": output_file if enable else None,
            "fps": fps if enable else None,
        }, message=f"Movie Maker {'activated' if enable else 'deactivated'} in project.godot")
    except Exception as e:
        log.error("Error in toggle_movie_maker: %s", e)
        return _err(str(e), error_type=type(e).__name__)


@mcp.tool(
    name="run_headless_diagnostics",
    description="Executes automated project diagnostics by invoking an external system command (Godot CLI binary) to validate resource imports, check UID integrity, and detect broken shader entry points.",
    custom_options={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def run_headless_diagnostics(
    project_path: str,
    checks: list[str] | None = None,
    timeout_seconds: int = 120,
) -> str:
    if checks is None:
        checks = ["import", "uids"]
    try:
        safe_proj = _sanitize_path(project_path)
        pf = safe_proj / "project.godot"
        if not pf.exists():
            return _err(f"Not a valid Godot project: {safe_proj}", error_type="FileNotFoundError")
        results: dict[str, Any] = {}
        run_all = "all" in checks
        if run_all or "import" in checks:
            try:
                godot = _find_godot()
                r = subprocess.run(
                    [godot, "--headless", "--path", str(safe_proj), "--quit"],
                    capture_output=True, text=True, timeout=timeout_seconds
                )
                stderr_lines = r.stderr.splitlines()
                results["import"] = {
                    "exit_code": r.returncode,
                    "errors": [l for l in stderr_lines if "ERROR" in l][:50],
                    "warnings": [l for l in stderr_lines if "WARNING" in l][:50],
                    "log_lines": len(stderr_lines),
                }
            except subprocess.TimeoutExpired:
                results["import"] = {"error": "Godot CLI execution timed out"}
            except Exception as ex:
                results["import"] = {"error": f"Failed invoking Godot CLI binary: {ex}"}
        if run_all or "uids" in checks:
            missing: list[str] = []
            orphans: list[str] = []
            for uid_f in safe_proj.rglob("*.uid"):
                if not uid_f.with_suffix("").exists():
                    orphans.append(str(uid_f.relative_to(safe_proj)))
            for gd_f in safe_proj.rglob("*.gd"):
                if not (gd_f.parent / (gd_f.name + ".uid")).exists():
                    missing.append(str(gd_f.relative_to(safe_proj)))
            results["uids"] = {
                "missing_uid_files": missing[:50],
                "orphan_uid_files": orphans[:50],
                "missing_count": len(missing),
                "orphan_count": len(orphans),
                "recommendation": "Run update_project_uids() to regenerate" if missing else "OK",
            }
        if run_all or "shaders" in checks:
            issues: list[str] = []
            shader_files = list(safe_proj.rglob("*.gdshader"))
            for sf in shader_files:
                src = _read_text(sf)
                if not any(ep in src for ep in ["void fragment()", "void vertex()", "void light()"]):
                    issues.append(str(sf.relative_to(safe_proj)))
            results["shaders"] = {
                "files_checked": len(shader_files),
                "missing_entry_points": issues,
            }
        return _ok({
            "project_path": str(safe_proj),
            "checks_run": list(results.keys()),
            "results": results,
        }, message="Headless project diagnostics completed successfully.")
    except Exception as e:
        log.error("Error in run_headless_diagnostics: %s", e)
        return _err(str(e), error_type=type(e).__name__)


# ===========================================================================
# ENTRY POINT
# ===========================================================================

def main() -> None:
    """FastMCP stdio entry point -- called by uvx / pip console script."""
    log.debug("Starting Godot MCP server (stdio)")
    try:
        gp = _find_godot()
        print(f"[godot-mcp] Godot detected: {gp}", file=sys.stderr)
    except RuntimeError as e:
        print(f"[godot-mcp] WARNING: {e}", file=sys.stderr)
        print("[godot-mcp] Set GODOT_PATH=/path/to/godot4 to resolve.", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
