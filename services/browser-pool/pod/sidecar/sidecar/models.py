from typing import Literal, Optional

from pydantic import BaseModel, Field


class NavigateIn(BaseModel):
    url: str
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "load"
    timeout_ms: int = 30_000


class ClickIn(BaseModel):
    ref: str
    button: Literal["left", "right", "middle"] = "left"
    click_count: int = 1
    timeout_ms: int = 10_000


class FillIn(BaseModel):
    ref: str
    text: str
    clear: bool = True
    timeout_ms: int = 10_000


class SelectIn(BaseModel):
    ref: str
    values: list[str]
    timeout_ms: int = 10_000


class PressIn(BaseModel):
    key: str  # e.g. "Enter", "Tab", "Control+a"


class TypeIn(BaseModel):
    text: str
    delay_ms: int = 0


class EvalIn(BaseModel):
    expression: str
    arg: Optional[object] = None


class FetchIn(BaseModel):
    """fetch() executed in the page context — for requests bound to browser
    state (service-worker signing, WASM, SPA middleware)."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = None
    timeout_ms: int = 30_000


class ScreenshotIn(BaseModel):
    full_page: bool = False
    return_b64: bool = False


class SwitchTabIn(BaseModel):
    index: int


class HoverIn(BaseModel):
    ref: str
    timeout_ms: int = 10_000
