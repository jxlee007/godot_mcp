#!/usr/bin/env python3
"""
Godot MCP Server  --  Pure Python / FastMCP
============================================
28 tools covering:
  * Core runtime (14)  -- launch editor, run project, scene ops via GDScript bridge
  * Animation text pipeline (4) -- keyframe quantizer, interpolation, library stitcher, track filter
  * AnimationTree blueprinting (3) -- state machine, transitions, blend space 2D
  * Camera & cinematic (3) -- bezier path, LookAt tracking, camera switcher timeline
  * NPR materials (2) -- batch shader uniforms, texture channel remap
  * Pipeline automation (2) -- Movie Maker toggle, headless diagnostics

Transport : stdio  (MCP standard)
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
    # mcp >= 2.0 renamed FastMCP to MCPServer with schema changes.
    # Alert user to pin mcp<2 as mandated by pyproject.toml.
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
            "FastMCP framework not found. Please install mcp>=1.3.0,<2: 'pip install \"mcp[cli]>=1.3.0,<2\"'"
        ) from err

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

mcp = FastMCP("godot-mcp")

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
# SECTION 3 -- Helpers
# ===========================================================================

def _validate_path(p: str) -> bool:
    return bool(p) and ".." not in p


def _validate_class_name(n: str) -> bool:
    return bool(n) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) is not None


def _ok(data: Any) -> str:
    if isinstance(data, dict):
        return json.dumps({"ok": True, **data}, indent=2)
    return json.dumps({"ok": True, "result": data}, indent=2)


def _err(msg: str) -> str:
    """Signal an execution exception to the MCP client.

    Raising RuntimeError ensures that FastMCP flags the protocol frame with
    isError=True, delivering clean diagnostic text directly to the model
    instead of requiring JSON inference over an active token channel.
    """
    raise RuntimeError(msg)


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


def _read_text(path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _write_text(path, content: str) -> None:
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


# ===========================================================================
# SECTION 4 -- Core runtime tools (14) -- mirrors Coding-Solo/godot-mcp
# ===========================================================================

@mcp.tool()
def get_godot_version() -> str:
    """Return the installed Godot 4 version string."""
    try:
        stdout, _ = _run_godot_sync(["--version"])
        return _ok({"version": stdout.strip()})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def launch_editor(project_path: str) -> str:
    """
    Launch the Godot editor for the given project (non-blocking).

    Args:
        project_path: Absolute path to the Godot project directory.
    """
    if not _validate_path(project_path):
        return _err("Invalid project_path (no '..' allowed)")
    pf = Path(project_path) / "project.godot"
    if not pf.exists():
        return _err(f"Not a Godot project (project.godot missing): {project_path}")
    try:
        godot = _find_godot()
        subprocess.Popen([godot, "-e", "--path", project_path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return _ok({"message": f"Godot editor launched for {project_path}"})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def run_project(project_path: str, scene: str = "") -> str:
    """
    Run a Godot project in debug mode and capture output.

    Args:
        project_path: Absolute path to the Godot project directory.
        scene: Optional specific scene file to run.
    """
    if not _validate_path(project_path):
        return _err("Invalid project_path")
    pf = Path(project_path) / "project.godot"
    if not pf.exists():
        return _err(f"Not a Godot project: {project_path}")
    try:
        godot = _find_godot()
        args = [godot, "-d", "--path", project_path]
        if scene and _validate_path(scene):
            args.append(scene)
        _launch_process(args)
        return _ok({"message": "Project started. Call get_debug_output() to read output."})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def get_debug_output() -> str:
    """Retrieve current stdout/stderr from the running Godot project."""
    data = _get_output()
    if data is None:
        return _err("No active Godot process. Call run_project() first.")
    return _ok(data)


@mcp.tool()
def stop_project() -> str:
    """Stop the currently running Godot project and return its final output."""
    with _active_lock:
        if _active["proc"] is None:
            return _err("No active Godot process.")
    data = _stop_active()
    return _ok({"message": "Project stopped.", **data})


@mcp.tool()
def list_projects(directory: str, recursive: bool = False) -> str:
    """
    Find Godot projects under a directory.

    Args:
        directory: Root directory to search.
        recursive: If True, search all subdirectories.
    """
    if not _validate_path(directory):
        return _err("Invalid directory path")
    root = Path(directory)
    if not root.exists():
        return _err(f"Directory does not exist: {directory}")
    projects: list[dict] = []
    if recursive:
        for p in root.rglob("project.godot"):
            d = p.parent
            projects.append({"path": str(d), "name": d.name})
    else:
        if (root / "project.godot").exists():
            projects.append({"path": str(root), "name": root.name})
        for child in root.iterdir():
            if child.is_dir() and (child / "project.godot").exists():
                projects.append({"path": str(child), "name": child.name})
    return _ok({"projects": projects, "count": len(projects)})


@mcp.tool()
def get_project_info(project_path: str) -> str:
    """
    Return metadata about a Godot project (name, structure, Godot version).

    Args:
        project_path: Absolute path to the Godot project directory.
    """
    if not _validate_path(project_path):
        return _err("Invalid project_path")
    pf = Path(project_path) / "project.godot"
    if not pf.exists():
        return _err(f"Not a Godot project: {project_path}")
    name = Path(project_path).name
    try:
        cfg = _read_text(pf)
        m = re.search(r'config/name="([^"]+)"', cfg)
        if m:
            name = m.group(1)
    except Exception:
        pass
    ext_counts: dict[str, int] = {}
    for f in Path(project_path).rglob("*"):
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
    return _ok({"name": name, "path": project_path,
                "godot_version": godot_version, "structure": structure})


@mcp.tool()
def create_scene(project_path: str, scene_path: str, root_node_type: str = "Node2D") -> str:
    """
    Create a new Godot scene file via the GDScript engine.

    Args:
        project_path: Absolute path to the Godot project directory.
        scene_path: Scene path relative to the project (e.g. 'scenes/player.tscn').
        root_node_type: Godot class name for the root node (default: Node2D).
    """
    if not _validate_path(project_path) or not _validate_path(scene_path):
        return _err("Invalid path (no '..' allowed)")
    if not _validate_class_name(root_node_type):
        return _err("Invalid root_node_type -- must be a simple Godot class name")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    try:
        stdout, stderr = _execute_gdscript_op(project_path, "create_scene", {
            "scene_path": scene_path, "root_node_type": root_node_type,
        })
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript error:\n{stderr}")
        return _ok({"message": f"Scene created: {scene_path}", "output": stdout, "log": stderr})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def add_node(
    project_path: str,
    scene_path: str,
    node_type: str,
    node_name: str,
    parent_node_path: str = "root",
    properties: dict | None = None,
) -> str:
    """
    Add a node to an existing Godot scene via the GDScript engine.

    Args:
        project_path: Absolute path to the Godot project directory.
        scene_path: Scene path relative to project.
        node_type: Godot class name for the new node (e.g. Sprite2D).
        node_name: Name for the new node.
        parent_node_path: Node path of the parent (default: 'root').
        properties: Optional dict of property_name -> value.
    """
    for p, lbl in [(project_path, "project_path"), (scene_path, "scene_path")]:
        if not _validate_path(p):
            return _err(f"Invalid {lbl}")
    if not _validate_class_name(node_type):
        return _err("Invalid node_type")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    params: dict[str, Any] = {
        "scene_path": scene_path, "node_type": node_type,
        "node_name": node_name, "parent_node_path": parent_node_path,
    }
    if properties:
        params["properties"] = properties
    try:
        stdout, stderr = _execute_gdscript_op(project_path, "add_node", params)
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript error:\n{stderr}")
        return _ok({"message": f"Node '{node_name}' added to {scene_path}", "output": stdout})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def load_sprite(project_path: str, scene_path: str, node_path: str, texture_path: str) -> str:
    """
    Load a texture onto a Sprite2D/TextureRect node inside a scene.

    Args:
        project_path: Absolute path to the Godot project directory.
        scene_path: Scene path relative to project.
        node_path: Path to the Sprite2D node (e.g. 'root/Player/Sprite2D').
        texture_path: Texture path relative to project.
    """
    for p in [project_path, scene_path, node_path, texture_path]:
        if not _validate_path(p):
            return _err(f"Invalid path: {p}")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    try:
        stdout, stderr = _execute_gdscript_op(project_path, "load_sprite", {
            "scene_path": scene_path, "node_path": node_path, "texture_path": texture_path,
        })
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript error:\n{stderr}")
        return _ok({"message": f"Texture loaded on {node_path}", "output": stdout})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def export_mesh_library(
    project_path: str, scene_path: str, output_path: str,
    mesh_item_names: list[str] | None = None,
) -> str:
    """
    Export a 3D scene as a MeshLibrary .res resource for GridMap.

    Args:
        project_path: Absolute path to the Godot project directory.
        scene_path: Source .tscn scene path relative to project.
        output_path: Output .res path relative to project.
        mesh_item_names: Optional list of mesh items to include (all if omitted).
    """
    for p in [project_path, scene_path, output_path]:
        if not _validate_path(p):
            return _err(f"Invalid path: {p}")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    params: dict[str, Any] = {"scene_path": scene_path, "output_path": output_path}
    if mesh_item_names:
        params["mesh_item_names"] = mesh_item_names
    try:
        stdout, stderr = _execute_gdscript_op(project_path, "export_mesh_library", params)
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript error:\n{stderr}")
        return _ok({"message": f"MeshLibrary exported to {output_path}", "output": stdout})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def save_scene(project_path: str, scene_path: str, new_path: str = "") -> str:
    """
    Save (or save-as) a Godot scene file.

    Args:
        project_path: Absolute path to the Godot project directory.
        scene_path: Scene path relative to project.
        new_path: If provided, save to this alternate path.
    """
    for p in [project_path, scene_path]:
        if not _validate_path(p):
            return _err(f"Invalid path: {p}")
    if new_path and not _validate_path(new_path):
        return _err("Invalid new_path")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    params: dict[str, Any] = {"scene_path": scene_path}
    if new_path:
        params["new_path"] = new_path
    try:
        stdout, stderr = _execute_gdscript_op(project_path, "save_scene", params)
        if "ERROR" in stderr and "Failed to" in stderr:
            return _err(f"GDScript error:\n{stderr}")
        return _ok({"message": f"Scene saved to {new_path or scene_path}", "output": stdout})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def get_uid(project_path: str, file_path: str) -> str:
    """
    Get the UID for a Godot 4.4+ resource file.

    Args:
        project_path: Absolute path to the Godot project directory.
        file_path: File path relative to project.
    """
    if not _validate_path(project_path) or not _validate_path(file_path):
        return _err("Invalid path")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    try:
        stdout, _ = _run_godot_sync(["--version"])
        ver = stdout.strip()
        m = re.match(r"(\d+)\.(\d+)", ver)
        if m:
            major, minor = int(m.group(1)), int(m.group(2))
            if not (major > 4 or (major == 4 and minor >= 4)):
                return _err(f"UIDs require Godot 4.4+. Detected: {ver}")
        stdout2, stderr2 = _execute_gdscript_op(project_path, "get_uid", {"file_path": file_path})
        return _ok({"output": stdout2, "log": stderr2})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def update_project_uids(project_path: str) -> str:
    """
    Resave all resources to regenerate UID files (Godot 4.4+).

    Args:
        project_path: Absolute path to the Godot project directory.
    """
    if not _validate_path(project_path):
        return _err("Invalid project_path")
    if not (Path(project_path) / "project.godot").exists():
        return _err(f"Not a Godot project: {project_path}")
    try:
        stdout, stderr = _execute_gdscript_op(
            project_path, "resave_resources", {"project_path": project_path}
        )
        return _ok({"message": "UID resave complete", "output": stdout})
    except Exception as e:
        return _err(str(e))


# ===========================================================================
# SECTION 5 -- Animation text-manipulation tools
# ===========================================================================

@mcp.tool()
def quantize_animation_keyframes(
    file_path: str,
    target_fps: int = 12,
    animation_name: str = "",
) -> str:
    """
    Stepped Framerate Quantizer: Round keyframe timestamps inside a .tscn/.tres file to
    fixed frame intervals -- e.g. 0.0833s for 12-fps anime look, 0.0417s for 24-fps film.

    Args:
        file_path: Absolute path to the .tscn or .tres animation file.
        target_fps: Target framerate to quantize to (12 for anime, 24 for film).
        animation_name: Reserved for future filtering by animation name.
    """
    try:
        p = Path(file_path)
        if not p.exists():
            return _err(f"File not found: {file_path}")
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
            "message": f"Quantized to {target_fps} fps",
            "frame_duration_s": round(frame_dur, 6),
            "arrays_modified": changes,
        })
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def set_animation_interpolation_mode(
    file_path: str,
    mode: str = "linear",
    track_index: int = -1,
) -> str:
    """
    Interpolation Mode Swapper: Bulk-edit keyframe easing modes in a .tscn/.tres file.
    Flip between linear, smooth bezier (cubic), or constant (stepped/nearest).

    Args:
        file_path: Absolute path to the .tscn or .tres file.
        mode: Interpolation mode -- 'nearest'/'constant' (0), 'linear' (1), 'cubic' (2),
              'linear_angle' (3), 'cubic_angle' (4).
        track_index: If >= 0, only patch this track index. -1 means all tracks.
    """
    MODE_MAP = {
        "nearest": 0, "constant": 0,
        "linear": 1,
        "cubic": 2,
        "linear_angle": 3,
        "cubic_angle": 4,
    }
    mode_int = MODE_MAP.get(mode.lower())
    if mode_int is None:
        return _err(f"Unknown mode '{mode}'. Choose: {', '.join(MODE_MAP)}")
    try:
        p = Path(file_path)
        if not p.exists():
            return _err(f"File not found: {file_path}")
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
        return _ok({"message": f"Interpolation set to '{mode}' ({mode_int})",
                    "tracks_modified": changes})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def stitch_animation_library(output_res_path: str, clips: list[dict]) -> str:
    """
    Animation Library Stitcher: Generate a master AnimationLibrary .tres file that
    references external animation clips by clean string names (idle, run, attack).

    Each clip dict must have:
      - 'name': library key (e.g. 'idle')
      - 'path': res:// path to the Animation .tres or glb sub-resource
                (e.g. 'res://char.glb/animations/Armature|run')

    Args:
        output_res_path: Absolute filesystem path for the output .tres file.
        clips: List of {'name': str, 'path': str} dicts.
    """
    if not clips:
        return _err("clips list is empty")
    try:
        load_steps = len(clips) + 1
        lines = [f'[gd_resource type="AnimationLibrary" load_steps={load_steps} format=3]\n']
        ext_ids: list[str] = []
        for i, clip in enumerate(clips):
            name = clip.get("name", f"clip_{i}")
            path = clip.get("path", "")
            if not path:
                return _err(f"Clip {i} missing 'path'")
            rid = f"{i + 1}_{_uid(path + name)}"
            ext_ids.append(rid)
            lines.append(f'[ext_resource type="Animation" path="{path}" id="{rid}"]')
        lines.append("\n[resource]")
        data_entries = ", ".join(
            f'"{clip.get("name", f"clip_{i}")!s}": ExtResource("{ext_ids[i]}")'
            for i, clip in enumerate(clips)
        )
        lines.append(f"_data = {{{data_entries}}}\n")
        content = "\n".join(lines)
        Path(output_res_path).write_text(content, encoding="utf-8")
        return _ok({
            "message": f"AnimationLibrary written to {output_res_path}",
            "clips_stitched": len(clips),
        })
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def generate_track_filter(
    scene_path: str,
    locked_bone_paths: list[str],
    blend_node_name: str = "UpperBodyFilter",
) -> str:
    """
    Track Filtering & Blending: Generate GDScript and resource text for an
    AnimationNodeBlendFilter that locks specific bone chains while letting
    upper-body bones follow a separate overlay animation.

    Args:
        scene_path: Absolute path to the .tscn scene file (appended if exists).
        locked_bone_paths: Bone NodePaths to exclude, e.g. ['Skeleton3D:Bone/Hip'].
        blend_node_name: Name for the AnimationNodeBlendFilter node.
    """
    try:
        rid = _res_id("AnimationNodeBlendFilter", scene_path + blend_node_name)
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
        if Path(scene_path).exists():
            existing = _read_text(scene_path)
            _write_text(scene_path, existing.rstrip() + "\n" + block)
            appended = True
        return _ok({
            "message": f"AnimationNodeBlendFilter '{blend_node_name}' generated",
            "resource_id": rid,
            "appended_to_scene": appended,
            "gdscript_snippet": script,
            "resource_block": block,
        })
    except Exception as e:
        return _err(str(e))


# ===========================================================================
# SECTION 6 -- AnimationTree & State Machine blueprinting
# ===========================================================================

@mcp.tool()
def generate_state_machine(
    scene_path: str,
    node_name: str,
    states: list[dict],
    anim_player_path: str = "AnimationPlayer",
    start_state: str = "",
) -> str:
    """
    State Machine Graph Generator: Inject an AnimationNodeStateMachine sub-resource
    block into a .tscn file -- define visual state blocks without opening the editor.

    Each state dict must have:
      - 'name': state name (e.g. 'Idle')
      - 'animation': animation clip name (e.g. 'idle')
      - 'position': optional [x, y] graph layout coordinates

    Args:
        scene_path: Absolute path to the .tscn scene file.
        node_name: Name for the AnimationTree node (e.g. 'AnimationTree').
        states: List of state definition dicts.
        anim_player_path: NodePath to the AnimationPlayer.
        start_state: Start state name (defaults to first state).
    """
    try:
        p = Path(scene_path)
        if not p.exists():
            return _err(f"Scene file not found: {scene_path}")
        if not states:
            return _err("states list is empty")
        start = start_state or states[0]["name"]
        sm_id = _res_id("AnimationNodeStateMachine", scene_path + node_name)
        anim_node_ids: dict[str, str] = {}
        sub_blocks: list[str] = []
        for state in states:
            sname = state["name"]
            anim = state.get("animation", sname.lower())
            aid = _res_id("AnimationNodeAnimation", scene_path + sname)
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
        return _ok({"message": f"AnimationTree '{node_name}' with {len(states)} states injected",
                    "state_machine_id": sm_id, "start_state": start})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def compile_state_transitions(
    scene_path: str,
    state_machine_resource_id: str,
    transitions: list[dict],
) -> str:
    """
    Transition Logic Compiler: Write transition arrow connections between state blocks,
    attaching evaluation parameters like 'speed > 0.1' or 'is_attacking == true'.

    Each transition dict must have:
      - 'from': source state name
      - 'to': destination state name
      - 'condition': optional condition string
      - 'xfade_time': crossfade duration in seconds (default 0.2)
      - 'advance_mode': 0=manual, 1=auto, 2=at_end (default 1)

    Args:
        scene_path: Absolute path to the .tscn scene file.
        state_machine_resource_id: Sub-resource id of the AnimationNodeStateMachine.
        transitions: List of transition dicts.
    """
    try:
        p = Path(scene_path)
        if not p.exists():
            return _err(f"Scene not found: {scene_path}")
        content = _read_text(p)
        if state_machine_resource_id not in content:
            return _err(f"Resource id '{state_machine_resource_id}' not found in scene")
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
        return _ok({"message": f"{len(transitions)} transitions compiled"})
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def calculate_blend_space_2d(
    scene_path: str,
    node_name: str,
    blend_points: list[dict],
    x_label: str = "Horizontal",
    y_label: str = "Vertical",
    anim_player_path: str = "AnimationPlayer",
) -> str:
    """
    2D Blend Space Grid Calculator: Compute and inject an AnimationNodeBlendSpace2D
    for directional locomotion blending (N/S/E/W + idle) based on a controller vector.

    Each blend_point dict must have:
      - 'animation': animation clip name (e.g. 'walk_north')
      - 'position': [x, y] normalized blend coordinates (e.g. [0, 1] for North)

    Args:
        scene_path: Absolute path to the .tscn scene file.
        node_name: Name for the AnimationTree node.
        blend_points: List of blend point dicts.
        x_label: Horizontal axis label.
        y_label: Vertical axis label.
        anim_player_path: NodePath to the AnimationPlayer.
    """
    try:
        p = Path(scene_path)
        if not p.exists():
            return _err(f"Scene not found: {scene_path}")
        bs_id = _res_id("AnimationNodeBlendSpace2D", scene_path + node_name)
        anim_ids: list[str] = []
        anim_blocks: list[str] = []
        for i, bp in enumerate(blend_points):
            anim = bp.get("animation", f"anim_{i}")
            aid = _res_id("AnimationNodeAnimation", scene_path + node_name + anim)
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
            "message": f"BlendSpace2D '{node_name}' with {len(blend_points)} points injected",
            "blend_space_id": bs_id,
        })
    except Exception as e:
        return _err(str(e))


# ===========================================================================
# SECTION 7 -- Camera & Cinematic tools
# ===========================================================================

def _catmull_to_bezier(pts: list[list[float]], tension: float = 0.0) -> list[list[float]]:
    """Convert Catmull-Rom waypoints to flat [in_h, position, out_h] list for Curve3D.

    Args:
        pts: List of [x, y, z] points.
        tension: Tension / traction control (-1.0 to 1.0, default 0.0).
                 0.0 = standard uniform velocity Catmull-Rom (tangent scale = 1/6).
                 Negative (e.g. -0.5) = loose, sweeping tangents for dramatic hyper-fast swoops.
                 Positive (e.g. 0.5) = tight, sharp tangents for high-precision track moves.
    """
    n = len(pts)
    result: list[list[float]] = []
    # Standard Catmull-Rom tangent factor is (1.0 - tension) / 6.0
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


@mcp.tool()
def generate_bezier_camera_path(
    scene_path: str,
    path_node_name: str,
    waypoints: list[dict],
    add_path_follow: bool = True,
    add_camera: bool = True,
    tension: float = 0.0,
) -> str:
    """
    Bezier Camera Path Generator: Calculate Catmull-Rom bezier handles from world-space
    waypoints and serialize them as Path3D + Curve3D node blocks in a .tscn file.
    Supports arc pans, dolly zooms, hyper-fast cinematic swoops, and orbital moves.

    Each waypoint dict must have:
      - 'position': [x, y, z] world position
      - 'tilt': optional tilt angle in radians (default 0.0)

    Args:
        scene_path: Absolute path to the .tscn scene file.
        path_node_name: Name for the Path3D node (e.g. 'CameraRail').
        waypoints: List of waypoint dicts.
        add_path_follow: If True, add a PathFollow3D child.
        add_camera: If True, add a Camera3D child under PathFollow3D.
        tension: Tension / speed-curve multiplier (-1.0 to 1.0, default 0.0).
                 Use negative values (e.g. -0.5) for wide sweeping cinematic moves;
                 positive values (e.g. 0.5) for crisp, tight corners.
    """
    try:
        p = Path(scene_path)
        if not p.exists():
            return _err(f"Scene not found: {scene_path}")
        if len(waypoints) < 2:
            return _err("At least 2 waypoints required")
        pts = [wp["position"] for wp in waypoints]
        tilts = [float(wp.get("tilt", 0.0)) for wp in waypoints]
        handles = _catmull_to_bezier(pts, tension=tension)

        def _v3(v: list[float]) -> str:
            return f"{v[0]:.4f}, {v[1]:.4f}, {v[2]:.4f}"

        flat_pts = ", ".join(_v3(h) for h in handles)
        flat_tilts = ", ".join(f"{t:.4f}" for t in tilts)
        curve_id = _res_id("Curve3D", scene_path + path_node_name)
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
            "message": f"Bezier camera path '{path_node_name}' with {len(waypoints)} waypoints",
            "curve_id": curve_id,
            "waypoints_count": len(waypoints),
            "tension": tension,
        })
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def inject_lookat_tracking(
    scene_path: str,
    camera_node_path: str,
    target_node_path: str,
    script_output_path: str = "",
) -> str:
    """
    Tracking & LookAt Injector: Generate a GDScript that procedurally tracks a target
    node's transform vector every frame, with smooth slerp interpolation.

    Args:
        scene_path: Absolute path to the .tscn scene file.
        camera_node_path: NodePath of the Camera3D to control.
        target_node_path: NodePath of the node to track (e.g. 'Player/Body').
        script_output_path: Where to write the .gd script. Empty = next to scene file.
    """
    try:
        p = Path(scene_path)
        if not p.exists():
            return _err(f"Scene not found: {scene_path}")
        gd_script = (
            "# LookAt tracking script -- auto-generated by godot-mcp\n"
            "extends Camera3D\n\n"
            "## Target node to track. Assign in the Inspector or via code.\n"
            "@export var target: Node3D\n"
            "@export var offset := Vector3.ZERO\n"
            "@export var smooth_speed := 5.0\n\n"
            "func _ready() -> void:\n"
            "\tif target == null:\n"
            f"\t\ttarget = get_node_or_null(\"{target_node_path}\")\n\n"
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
            script_output_path = str(p.parent / "camera_lookat_tracker.gd")
        Path(script_output_path).write_text(gd_script, encoding="utf-8")
        return _ok({
            "message": "LookAt tracking script generated",
            "script_path": script_output_path,
            "camera_node": camera_node_path,
            "target_node": target_node_path,
            "script": gd_script,
        })
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def build_camera_switcher_timeline(
    scene_path: str,
    anim_player_node: str,
    switches: list[dict],
    animation_name: str = "CameraTimeline",
    timeline_length: float = 10.0,
) -> str:
    """
    Camera Switcher Timeline Builder: Write an Animation resource block that toggles
    Camera3D.current at exact timestamps for multi-angle cinematic cuts.

    Each switch dict must have:
      - 'camera_path': NodePath to a Camera3D (e.g. 'CamA')
      - 'activate_at': float timestamp in seconds
      - 'deactivate_at': optional float timestamp to turn camera off

    Args:
        scene_path: Absolute path to the .tscn scene file.
        anim_player_node: Name of the AnimationPlayer node in the scene.
        switches: List of camera switch event dicts.
        animation_name: Name for the generated animation clip.
        timeline_length: Total animation length in seconds.
    """
    try:
        p = Path(scene_path)
        if not p.exists():
            return _err(f"Scene not found: {scene_path}")
        anim_id = _res_id("Animation", scene_path + animation_name)
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
        lib_id = _res_id("AnimationLibrary", scene_path + "SwitcherLib")
        lib_block = (
            f'[sub_resource type="AnimationLibrary" id="{lib_id}"]\n'
            f'_data = {{"{animation_name}": SubResource("{anim_id}")}}\n'
        )
        block = anim_block + "\n" + lib_block
        existing = _read_text(p)
        _write_text(p, existing.rstrip() + "\n\n" + block)
        return _ok({
            "message": f"Camera switcher timeline '{animation_name}' injected",
            "cameras": len(switches),
            "duration_s": timeline_length,
            "animation_id": anim_id,
        })
    except Exception as e:
        return _err(str(e))


# ===========================================================================
# SECTION 8 -- NPR Materials & Shader tools
# ===========================================================================

@mcp.tool()
def batch_shader_uniforms(
    directory: str,
    uniform_overrides: dict,
    recursive: bool = True,
    dry_run: bool = False,
) -> str:
    """
    Uniform Parameter Batcher: Scan ShaderMaterial .tres files and bulk-override
    shader_parameter/* values -- change cell-shading threshold, outline thickness,
    ink color across hundreds of assets in one call.

    uniform_overrides maps param names to new value strings:
      {'outline_thickness': '2.0', 'light_threshold': '0.5'}

    Args:
        directory: Directory to scan for .tres files.
        uniform_overrides: Dict of {param_name: new_value_string}.
        recursive: If True, scan all subdirectories (default True).
        dry_run: If True, report changes without writing files.
    """
    try:
        root = Path(directory)
        if not root.exists():
            return _err(f"Directory not found: {directory}")
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
                    replaced, n = pat.subn(lambda m, v=value: m.group(1) + v, new_content)
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
            "message": f"{'[DRY RUN] ' if dry_run else ''}Batch uniform override complete",
            "files_scanned": len(files),
            "files_modified": len([r for r in results if "changes" in r]),
            "total_replacements": total_changes,
            "dry_run": dry_run,
            "details": results,
        })
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def remap_texture_channels(material_path: str, channel_map: dict) -> str:
    """
    Texture Channel Remapper: Link hand-painted image textures into specific material
    property slots by altering ext_resource references in a .tres file.

    channel_map maps Godot property names to new res:// paths:
      {
        'albedo_texture': 'res://textures/hero_albedo.png',
        'normal_texture': 'res://textures/hero_normal.png',
        'shader_parameter/base_tex': 'res://textures/hero_hand_painted.png'
      }

    Args:
        material_path: Absolute path to the .tres material file.
        channel_map: Dict of {property_name: res_path}.
    """
    try:
        p = Path(material_path)
        if not p.exists():
            return _err(f"Material file not found: {material_path}")
        content = _read_text(p)
        new_content = content
        ls_match = re.search(r'load_steps=(\d+)', content)
        load_steps = int(ls_match.group(1)) if ls_match else 1
        existing_ids = len(re.findall(r'\[ext_resource', content))
        next_id_num = existing_ids + 1
        ext_ids: dict[str, str] = {}
        changes: list[dict] = []
        for prop, res_path in channel_map.items():
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
        return _ok({"message": f"{len(changes)} texture channels remapped",
                    "changes": changes, "new_ext_resources": len(ext_ids)})
    except Exception as e:
        return _err(str(e))


# ===========================================================================
# SECTION 9 -- Pipeline automation tools
# ===========================================================================

@mcp.tool()
def toggle_movie_maker(
    project_path: str,
    enable: bool = True,
    output_file: str = "res://render/output.avi",
    fps: int = 60,
) -> str:
    """
    Movie Maker Engine Activator: Edit project.godot to toggle Godot's built-in
    uncompressed video recorder, locking the render time-step to a perfect fps loop.

    Args:
        project_path: Absolute path to the Godot project directory.
        enable: True to activate Movie Maker, False to disable.
        output_file: res:// output video path (e.g. 'res://render/movie.avi').
        fps: Capture framerate (default 60). Only used when enable=True.
    """
    try:
        pf = Path(project_path) / "project.godot"
        if not pf.exists():
            return _err(f"Not a Godot project: {project_path}")
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
            "message": f"Movie Maker {'enabled' if enable else 'disabled'}",
            "output_file": output_file if enable else "N/A",
            "fps": fps if enable else "N/A",
        })
    except Exception as e:
        return _err(str(e))


@mcp.tool()
def run_headless_diagnostics(
    project_path: str,
    checks: list[str] | None = None,
    timeout_seconds: int = 120,
) -> str:
    """
    Headless Project Diagnostics: Invoke Godot CLI to validate imports, check UID
    linkages, detect broken shader entry points, and output a structured diagnostic log.

    Args:
        project_path: Absolute path to the Godot project directory.
        checks: Checks to run: 'import', 'uids', 'shaders', 'all'. Default: ['import','uids'].
        timeout_seconds: Max seconds to wait for Godot (default 120).
    """
    if checks is None:
        checks = ["import", "uids"]
    try:
        pf = Path(project_path) / "project.godot"
        if not pf.exists():
            return _err(f"Not a Godot project: {project_path}")
        results: dict[str, Any] = {}
        run_all = "all" in checks
        if run_all or "import" in checks:
            try:
                godot = _find_godot()
                r = subprocess.run(
                    [godot, "--headless", "--path", project_path, "--quit"],
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
                results["import"] = {"error": "Timed out"}
        if run_all or "uids" in checks:
            root = Path(project_path)
            missing: list[str] = []
            orphans: list[str] = []
            for uid_f in root.rglob("*.uid"):
                if not uid_f.with_suffix("").exists():
                    orphans.append(str(uid_f.relative_to(root)))
            for gd_f in root.rglob("*.gd"):
                if not (gd_f.parent / (gd_f.name + ".uid")).exists():
                    missing.append(str(gd_f.relative_to(root)))
            results["uids"] = {
                "missing_uid_files": missing[:50],
                "orphan_uid_files": orphans[:50],
                "missing_count": len(missing),
                "orphan_count": len(orphans),
                "recommendation": "Run update_project_uids()" if missing else "OK",
            }
        if run_all or "shaders" in checks:
            issues: list[str] = []
            shader_files = list(Path(project_path).rglob("*.gdshader"))
            for sf in shader_files:
                src = _read_text(sf)
                if not any(ep in src for ep in ["void fragment()", "void vertex()", "void light()"]):
                    issues.append(str(sf.relative_to(Path(project_path))))
            results["shaders"] = {
                "files_checked": len(shader_files),
                "missing_entry_points": issues,
            }
        return _ok({
            "message": "Diagnostic complete",
            "project": project_path,
            "checks_run": list(results.keys()),
            "results": results,
        })
    except Exception as e:
        return _err(str(e))


# ===========================================================================
# SECTION 10 -- MCP Resources
# ===========================================================================

@mcp.resource("godot://engine/operations_template")
def get_operations_template() -> str:
    """Returns the raw underlying GDScript bridge code for structural verification."""
    if not _GD_SCRIPT.exists():
        raise FileNotFoundError(f"GDScript engine not found at {_GD_SCRIPT}")
    return _read_text(_GD_SCRIPT)


@mcp.resource("godot://engine/operations_reference")
def get_operations_reference() -> str:
    """Returns the API reference and parameter specifications for headless GDScript operations."""
    return """# Godot Operations Reference

The headless GDScript engine (`godot://engine/operations_template`) exposes 7 atomic operations:

1. `create_scene`:
   - `scene_path`: str (e.g. "scenes/main.tscn")
   - `root_node_type`: str (e.g. "Node2D", "Node3D", "Control")

2. `add_node`:
   - `scene_path`: str
   - `node_type`: str (Godot class name)
   - `node_name`: str
   - `parent_node_path`: str (default: "root")
   - `properties`: dict (optional property key-values)

3. `load_sprite`:
   - `scene_path`: str
   - `node_path`: str
   - `texture_path`: str

4. `export_mesh_library`:
   - `scene_path`: str
   - `output_path`: str (".res" or ".tres")
   - `mesh_item_names`: list[str] (optional)

5. `save_scene`:
   - `scene_path`: str
   - `new_path`: str (optional, creates variant)

6. `get_uid`:
   - `file_path`: str (requires Godot 4.4+)

7. `resave_resources`:
   - `project_path`: str (regenerates all UIDs)
"""


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
