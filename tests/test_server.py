"""M8ven Trust Index compliance test suite for godot_mcp.

Tests run without a live Godot binary — pure Python unit tests.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest

# Ensure src/ is on path for both local dev and CI
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from godot_mcp.server import mcp, _sanitize_path, _err, _ok

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
EXPECTED_TOOLS = [
    "get_godot_version",
    "launch_editor",
    "run_project",
    "get_debug_output",
    "stop_project",
    "list_projects",
    "get_project_info",
    "create_scene",
    "add_node",
    "load_sprite",
    "export_mesh_library",
    "save_scene",
    "get_uid",
    "update_project_uids",
    "quantize_animation_keyframes",
    "set_animation_interpolation_mode",
    "stitch_animation_library",
    "generate_track_filter",
    "generate_state_machine",
    "compile_state_transitions",
    "calculate_blend_space_2d",
    "generate_bezier_camera_path",
    "inject_lookat_tracking",
    "build_camera_switcher_timeline",
    "batch_shader_uniforms",
    "remap_texture_channels",
    "toggle_movie_maker",
    "run_headless_diagnostics",
]
HINT_KEYS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def all_tools():
    """Returns {name: tool} dict via sync _tool_manager API (no Godot binary needed)."""
    return {t.name: t for t in mcp._tool_manager.list_tools()}


# ---------------------------------------------------------------------------
# 1. Registration tests
# ---------------------------------------------------------------------------
class TestToolRegistration:
    def test_exactly_28_tools_registered(self, all_tools):
        assert len(all_tools) == 28, f"Expected 28, got {len(all_tools)}: {sorted(all_tools)}"

    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_each_expected_tool_is_registered(self, all_tools, name):
        assert name in all_tools, f"Tool '{name}' not found in registry"


# ---------------------------------------------------------------------------
# 2. M8ven hint matrix tests
# ---------------------------------------------------------------------------
class TestHintMatrix:
    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_tool_has_all_four_hints(self, all_tools, name):
        tool = all_tools[name]
        opts = (tool.meta or {}).get("custom_options", {})
        for key in HINT_KEYS:
            assert key in opts, f"{name}: missing hint '{key}'"

    @pytest.mark.parametrize("name", EXPECTED_TOOLS)
    def test_all_hints_are_booleans(self, all_tools, name):
        tool = all_tools[name]
        opts = (tool.meta or {}).get("custom_options", {})
        for key in HINT_KEYS:
            assert isinstance(opts.get(key), bool), (
                f"{name}.{key} = {opts.get(key)!r} is not bool"
            )


# ---------------------------------------------------------------------------
# 3. run_headless_diagnostics disclosure test
# ---------------------------------------------------------------------------
class TestDiagnosticsDisclosure:
    def test_description_mentions_external_system_command(self, all_tools):
        desc = all_tools["run_headless_diagnostics"].description or ""
        assert "external system command" in desc, (
            f"Description should mention 'external system command', got: {desc!r}"
        )

    def test_description_mentions_godot_cli_binary(self, all_tools):
        desc = all_tools["run_headless_diagnostics"].description or ""
        assert "Godot CLI binary" in desc, (
            f"Description should mention 'Godot CLI binary', got: {desc!r}"
        )

    def test_read_only_hint_is_false(self, all_tools):
        opts = all_tools["run_headless_diagnostics"].meta.get("custom_options", {})
        assert opts["readOnlyHint"] is False

    def test_open_world_hint_is_true(self, all_tools):
        opts = all_tools["run_headless_diagnostics"].meta.get("custom_options", {})
        assert opts["openWorldHint"] is True


# ---------------------------------------------------------------------------
# 4. Zero-trust path sanitization tests
# ---------------------------------------------------------------------------
class TestPathSanitization:
    def test_blocks_parent_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            _sanitize_path("../../etc/passwd")

    def test_blocks_embedded_traversal(self):
        with pytest.raises(ValueError, match="traversal"):
            _sanitize_path("project/../../../etc")

    def test_blocks_null_byte(self):
        with pytest.raises(ValueError, match="Null"):
            _sanitize_path("valid/path\x00evil")

    def test_accepts_valid_path(self, tmp_path):
        result = _sanitize_path(str(tmp_path))
        assert result.exists()


# ---------------------------------------------------------------------------
# 5. Structured response & error format tests
# ---------------------------------------------------------------------------
class TestStructuredResponses:
    def test_ok_returns_json_string(self):
        result = _ok({"data": 123}, message="Operation completed")
        assert isinstance(result, str)
        data = json.loads(result)
        assert data["status"] == "success"
        assert data["message"] == "Operation completed"
        assert data["data"] == 123

    def test_err_returns_json_string(self):
        result = _err("test message")
        assert isinstance(result, str)
        data = json.loads(result)
        assert data["status"] == "error"
        assert "message" in data
        assert data["message"] == "test message"

    def test_err_includes_error_type(self):
        result = _err("something broke", error_type="FileNotFoundError")
        data = json.loads(result)
        assert data["error_type"] == "FileNotFoundError"

    def test_err_default_error_type(self):
        result = _err("oops")
        data = json.loads(result)
        assert "error_type" in data
