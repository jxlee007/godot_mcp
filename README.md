# godot-mcp — Pure Python FastMCP Server for Godot 4

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![FastMCP](https://img.shields.io/badge/FastMCP-1.3%2B-green)](https://github.com/modelcontextprotocol/python-sdk)
[![License: MIT](https://img.shields.io/badge/License-MIT-red)](LICENSE)

A **zero Node.js** Model Context Protocol server for Godot 4 — translated from
`Coding-Solo/godot-mcp` and `youichi-uda/godot-mcp-pro` into pure Python using
the official **FastMCP** framework. Runs with ultra-low RAM via `uvx`.

## Quick Start

You can invoke this server over standard I/O (`stdio`) from anywhere on your machine instantly using `uvx` without manually cloning files or configuring virtual environments:

```bash
# Run globally via uvx (Bypasses local setup completely)
GODOT_PATH=/path/to/godot4 uvx --from git+https://github.com/your-fork/godot-mcp godot-mcp

# Local development installation (For active contributors)
git clone https://github.com/your-fork/godot-mcp
cd godot-mcp
uv tool install --editable .
```

## MCP Client Configuration

Add this structural configuration block straight into your global client matrix environment profile (e.g., `~/.gemini/antigravity-cli/settings.json` or Claude / Cline settings):

```json
{
  "mcpServers": {
    "godot-arcane": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/your-fork/godot-mcp",
        "godot-mcp"
      ],
      "env": {
        "GODOT_PATH": "/path/to/godot4",
        "DEBUG": "false"
      }
    }
  }
}
```

## Environment Variables

| Variable | Description |
|---|---|
| `GODOT_PATH` | Path to the Godot 4 executable (auto-detected if unset) |
| `DEBUG` | `"true"` enables verbose stderr logging |

## Tools (28 total)

### Core Runtime (14)
| Tool | Description |
|---|---|
| `get_godot_version` | Return installed Godot version |
| `launch_editor` | Open Godot editor for a project |
| `run_project` | Run project in debug mode, capture output |
| `get_debug_output` | Read stdout/stderr from running project |
| `stop_project` | Kill running project, return final output |
| `list_projects` | Find Godot projects in a directory |
| `get_project_info` | Metadata, file counts, version |
| `create_scene` | Create a new .tscn file via GDScript |
| `add_node` | Add a node to an existing scene |
| `load_sprite` | Load texture onto Sprite2D/TextureRect |
| `export_mesh_library` | Export .tscn as MeshLibrary .res |
| `save_scene` | Save / save-as a scene file |
| `get_uid` | Get UID for a Godot 4.4+ resource |
| `update_project_uids` | Resave all resources to regenerate UIDs |

### Animation Text Pipeline (4)
| Tool | Description |
|---|---|
| `quantize_animation_keyframes` | Round timestamps to fixed fps increments (12/24 fps anime look) |
| `set_animation_interpolation_mode` | Bulk-swap easing: linear, cubic, nearest/stepped |
| `stitch_animation_library` | Merge external .glb/.tres clips into master AnimationLibrary |
| `generate_track_filter` | Generate AnimationNodeBlendFilter for upper/lower body isolation |

### AnimationTree Blueprinting (3)
| Tool | Description |
|---|---|
| `generate_state_machine` | Inject AnimationNodeStateMachine into a .tscn file |
| `compile_state_transitions` | Write transition arrows with conditions |
| `calculate_blend_space_2d` | Generate 2D blend space for directional locomotion |

### Camera & Cinematic (3)
| Tool | Description |
|---|---|
| `generate_bezier_camera_path` | Catmull-Rom bezier Path3D + PathFollow3D + Camera3D with tension control |
| `inject_lookat_tracking` | Generate LookAt tracking GDScript for Camera3D |
| `build_camera_switcher_timeline` | Write Animation that switches Camera3D.current at timestamps |

### NPR Materials & Shaders (2)
| Tool | Description |
|---|---|
| `batch_shader_uniforms` | Bulk-override shader_parameter/* in all .tres files |
| `remap_texture_channels` | Wire textures into material property slots |

### Pipeline Automation (2)
| Tool | Description |
|---|---|
| `toggle_movie_maker` | Edit project.godot to enable/disable Movie Maker mode |
| `run_headless_diagnostics` | Validate imports, UIDs, shader entry points via Godot CLI |

## Architecture

```
src/godot_mcp/
├── __init__.py
├── server.py              # FastMCP app — all 28 tools
└── scripts/
    └── godot_operations.gd  # Bundled GDScript engine (preserved untouched)
```

**Two operation modes:**
1. **Subprocess bridge** — core 14 tools invoke `godot_operations.gd` headlessly via `subprocess.run()`
2. **Text manipulation** — pro-port 14 tools parse/write `.tscn`, `.tres`, `project.godot` files as plain text using Python stdlib `re` only

## Requirements

- Python 3.10+
- Godot 4.x installed (set `GODOT_PATH` or ensure it is on `$PATH`)
- `uv` or `pip`

## License

MIT
