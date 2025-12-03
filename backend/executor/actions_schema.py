"""
Schemas and validation helpers for action plans.

Supported actions and expected params:
- open_app: {"target": "<app name or exe stem>", "count": 2}.
- open_url: {"url": "https://example.com", "browser": "edge"}.
- switch_window: {"title": "<window title or partial match>"}.
- activate_window: {"title_keywords": ["notepad"], "class_keywords": ["notepad"]}.
- fuzzy_switch_window: {"title": "<partial title>"}.
- list_windows: {}.
- get_active_window: {}.
- type_text: {"text": "<text to type>"}.
- key_press: {"keys": ["ctrl", "c"]} (list of key identifiers).
- hotkey: {"keys": ["ctrl", "s"]} or {"key": "ctrl+s"}.
- mouse_move: {"x": <int>, "y": <int>}.
- click: {"x": <int>, "y": <int>} or {"button": "left", "times": 1}. Optional targeting aids: "text"/"target"/"label", "target_icon", "visual_description", "strategy_hint".
- right_click: {"x": <int>, "y": <int>} or {"times": 1}.
- double_click: {"x": <int>, "y": <int>}.
- scroll: {"direction": "down", "amount": 2} or {"dx": <int>, "dy": <int>}.
- drag: {"start": {"x": <int>, "y": <int>}, "end": {"x": <int>, "y": <int>}, "duration": <float>}. Optional targeting aids for click/drag actions: "target_icon" (template path), "visual_description" (text for VLM), "strategy_hint" (e.g., "icon", "color").
- move_file: {"source": "<path>", "destination_dir": "<dir>"} (alias: "destination").
- copy_file: {"source": "<path>", "destination_dir": "<dir>"} (alias: "destination").
- rename_file: {"source": "<path>", "new_name": "<filename>"}.
- list_files: {"path": "<dir>"}.
- delete_file: {"path": "<file>"}.
- create_folder: {"path": "<dir>"}.
- read_file: {"path": "<file>"}.
- write_file: {"path": "<file>", "content": "<text>"}.
- wait: {"seconds": <float>} duration to pause.
- adjust_volume: {"level": <0-100>} or {"delta": <int>}.
- click_text: {"query": "<text to click>", "boxes": [...]}.
- browser_click: {"text": "<label to click>", "variants": ["Alt"], "button": "left"}.
- browser_input: {"text": "<field label>", "variants": ["Alt"], "value": "<text to type>"}.
- browser_extract_text: {"text": "<label>", "variants": ["Alt"]}.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, model_validator

ActionName = Literal[
    "open_app",
    "open_url",
    "switch_window",
    "activate_window",
    "type_text",
    "key_press",
    "click",
    "move_file",
    "copy_file",
    "rename_file",
    "mouse_move",
    "right_click",
    "double_click",
    "scroll",
    "drag",
    "hotkey",
    "list_windows",
    "get_active_window",
    "fuzzy_switch_window",
    "list_files",
    "delete_file",
    "create_folder",
    "read_file",
    "write_file",
    "wait",
    "adjust_volume",
    "click_text",
    "browser_click",
    "browser_input",
    "browser_extract_text",
]


class ScrollAction(BaseModel):
    """Validated schema for scroll actions."""

    direction: Optional[Literal["up", "down", "left", "right"]] = Field(
        default=None,
        description="Scroll direction; optional when explicit dx/dy provided.",
    )
    amount: int = Field(
        default=120,
        ge=1,
        le=10_000,
        description="Magnitude of scroll movement; positive integer.",
    )
    dx: int = Field(
        default=0,
        description="Horizontal delta override; positive scrolls right.",
    )
    dy: int = Field(
        default=0,
        description="Vertical delta override; positive scrolls up.",
    )

    @model_validator(mode="after")
    def _ensure_intent(self) -> "ScrollAction":
        if self.direction is None and self.dx == 0 and self.dy == 0:
            raise ValueError("direction or non-zero dx/dy is required for scroll action")
        return self

    def to_deltas(self) -> tuple[int, int]:
        dx = int(self.dx)
        dy = int(self.dy)
        if self.direction == "up":
            dy += self.amount
        elif self.direction == "down":
            dy -= self.amount
        elif self.direction == "left":
            dx -= self.amount
        elif self.direction == "right":
            dx += self.amount
        return dx, dy


class ListFilesAction(BaseModel):
    """List directory contents."""

    path: str

    @model_validator(mode="after")
    def _require_path(self) -> "ListFilesAction":
        if not self.path:
            raise ValueError("'path' is required")
        return self


class DeleteFileAction(BaseModel):
    """Delete a single file."""

    path: str
    confirm: Optional[bool] = Field(
        default=None,
        description="Explicit confirmation for destructive delete operations.",
    )

    @model_validator(mode="after")
    def _require_path(self) -> "DeleteFileAction":
        if not self.path:
            raise ValueError("'path' is required")
        return self


class WriteFileAction(BaseModel):
    """Create or overwrite a file with provided content."""

    path: str
    content: str

    @model_validator(mode="after")
    def _require_fields(self) -> "WriteFileAction":
        if not self.path:
            raise ValueError("'path' is required")
        if self.content is None:
            raise ValueError("'content' is required")
        return self


class _DirAliasMixin(BaseModel):
    """Normalize destination alias to destination_dir."""

    @model_validator(mode="before")
    def _normalize_destination(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "destination_dir" not in data and "destination" in data:
                data = {**data, "destination_dir": data.get("destination")}
        return data


class MoveFileAction(_DirAliasMixin):
    """Move a file into a destination directory."""

    source: str
    destination_dir: str

    @model_validator(mode="after")
    def _require_fields(self) -> "MoveFileAction":
        if not self.source:
            raise ValueError("'source' is required")
        if not self.destination_dir:
            raise ValueError("'destination_dir' is required")
        return self


class CopyFileAction(_DirAliasMixin):
    """Copy a file into a destination directory."""

    source: str
    destination_dir: str

    @model_validator(mode="after")
    def _require_fields(self) -> "CopyFileAction":
        if not self.source:
            raise ValueError("'source' is required")
        if not self.destination_dir:
            raise ValueError("'destination_dir' is required")
        return self


class RenameFileAction(BaseModel):
    """Rename a file within its directory."""

    source: str
    new_name: str

    @model_validator(mode="after")
    def _require_fields(self) -> "RenameFileAction":
        if not self.source:
            raise ValueError("'source' is required")
        if not self.new_name:
            raise ValueError("'new_name' is required")
        return self


class DragAction(BaseModel):
    """Drag from a start coordinate to an end coordinate."""

    start: Dict[str, Any]
    end: Dict[str, Any]
    duration: float = Field(default=0.2, ge=0.0, le=30.0)
    target_icon: Optional[str] = Field(default=None, description="Template path for icon matching")
    visual_description: Optional[str] = Field(default=None, description="Free-form visual description for VLM")
    strategy_hint: Optional[str] = Field(default=None, description="Hint such as 'icon' or 'color' to guide locator")

    @model_validator(mode="after")
    def _validate_coords(self) -> "DragAction":
        def _valid_point(point: Dict[str, Any]) -> bool:
            if not isinstance(point, dict):
                return False
            if "x" in point and "y" in point:
                try:
                    float(point["x"])
                    float(point["y"])
                    return True
                except Exception:
                    return False
            if point.get("target") or point.get("text") or point.get("visual_description") or point.get("target_icon"):
                return True
            return False

        for point, name in [(self.start, "start"), (self.end, "end")]:
            if not _valid_point(point):
                raise ValueError(f"'{name}' must have x/y or targeting hints")
        return self


class ClickAction(BaseModel):
    """Click targeting with optional visual hints."""

    x: Optional[float] = None
    y: Optional[float] = None
    button: str = Field(default="left", description="Mouse button")
    text: Optional[str] = None
    target: Optional[str] = None
    label: Optional[str] = None
    visual_description: Optional[str] = None
    target_icon: Optional[Any] = None
    strategy_hint: Optional[str] = None

    @model_validator(mode="after")
    def _require_target(self) -> "ClickAction":
        has_coords = self.x is not None and self.y is not None
        has_text = any([self.text, self.target, self.label, self.visual_description, self.target_icon])
        if not has_coords and not has_text:
            raise ValueError("click requires x/y or a target/visual hint")
        return self

class ActionStep(BaseModel):
    """One executable action with provider-specific params."""

    action: ActionName = Field(
        ...,
        description="The action to perform.",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="Action parameters; see module docstring for expected keys.",
    )

    @model_validator(mode="after")
    def _validate_action_params(self) -> "ActionStep":
        params: Dict[str, Any] = dict(self.params or {})
        validated_params: Dict[str, Any] = dict(params)
        try:
            if self.action == "scroll":
                validated_params.update(ScrollAction.model_validate(params).model_dump())
            elif self.action == "type_text":
                # auto_enter is optional; default True for backwards compatibility.
                validated_params.setdefault("auto_enter", True)
            elif self.action == "list_files":
                validated_params.update(ListFilesAction.model_validate(params).model_dump())
            elif self.action == "delete_file":
                validated_params.update(DeleteFileAction.model_validate(params).model_dump())
            elif self.action == "move_file":
                validated_params.update(MoveFileAction.model_validate(params).model_dump())
            elif self.action == "copy_file":
                validated_params.update(CopyFileAction.model_validate(params).model_dump())
            elif self.action == "rename_file":
                validated_params.update(RenameFileAction.model_validate(params).model_dump())
            elif self.action == "write_file":
                validated_params.update(WriteFileAction.model_validate(params).model_dump())
            elif self.action == "drag":
                validated_params.update(DragAction.model_validate(params).model_dump())
            elif self.action in {"click", "right_click", "double_click"}:
                validated_params.update(ClickAction.model_validate(params).model_dump())
        except ValidationError as exc:  # noqa: BLE001
            raise ValueError(f"invalid {self.action} params: {exc}") from exc
        self.params = validated_params
        return self


class ActionPlan(BaseModel):
    """A task description with an ordered list of action steps."""

    task: Optional[str] = Field(
        default=None,
        description="Human-readable task summary.",
    )
    steps: List[ActionStep] = Field(
        default_factory=list,
        description="Ordered actions to execute.",
    )


def validate_action_plan(plan: Dict[str, Any]) -> ActionPlan:
    """
    Parse and validate a raw JSON-like plan into an ActionPlan.

    Raises:
        ValueError: If validation fails with a readable message.
    """
    try:
        return ActionPlan.model_validate(plan)
    except ValidationError as exc:  # noqa: BLE001
        raise ValueError(f"Invalid action plan: {exc}") from exc
