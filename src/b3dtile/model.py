"""Backend result types. Importing this module never imports Blender."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KitInstance:
    object_id: str
    kit_id: str
    root: Any
    mesh: Any
    regions: dict[str, list[int]]
    # UV coordinates are per Blender mesh loop, normalized in a region's chart.
    # Origin is top-left; atlas mapping is responsible for Blender V conversion.
    uv_template: dict[int, tuple[float, float]]
    meta: dict = field(default_factory=dict)
    region_slots: dict[str, str] = field(default_factory=dict)
    region_tiles: dict[str, str] = field(default_factory=dict)
    rig: Any = None
