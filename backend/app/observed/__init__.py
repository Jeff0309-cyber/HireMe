"""Loader for data/observed.json — the ONLY source of prices, fees, FX rates and cashback.

Every entry must carry value, unit, source_url, observed_at (ISO timestamp) and observer.
Synthetic entries must say label="SIMULATED" and use a simulated:// source_url, so they can
never be mistaken for a real-world observation.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "data" / "observed.json"


class ObservedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    value: int  # integers only: money in minor units, rates in basis points
    unit: str
    source_url: str
    observed_at: str
    observer: str
    label: Optional[Literal["SIMULATED"]] = None
    note: str = ""

    @field_validator("value", mode="before")
    @classmethod
    def _no_floats(cls, v: object) -> object:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("value must be an integer (minor units or basis points)")
        return v

    @field_validator("observed_at")
    @classmethod
    def _iso(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))  # raises if not ISO
        return v

    @model_validator(mode="after")
    def _source_matches_label(self) -> "ObservedValue":
        if self.label == "SIMULATED" and not self.source_url.startswith("simulated://"):
            raise ValueError(f"{self.id}: SIMULATED entries must use a simulated:// source_url")
        if self.label is None and not self.source_url.startswith("https://"):
            raise ValueError(f"{self.id}: real observations need an https:// source_url")
        return self

    @property
    def is_simulated(self) -> bool:
        return self.label == "SIMULATED"


def load_observed(path: Path = DEFAULT_PATH) -> dict[str, ObservedValue]:
    """Load and validate every entry. Any problem raises, so callers fail closed."""
    raw = json.loads(Path(path).read_text())
    entries = [ObservedValue(**e) for e in raw["entries"]]
    by_id = {e.id: e for e in entries}
    if len(by_id) != len(entries):
        raise ValueError("duplicate ids in observed.json")
    return by_id
