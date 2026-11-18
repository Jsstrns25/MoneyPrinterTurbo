"""Validated, credential-free production contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Settings(Model):
    mode: Literal["demo", "live"] = "demo"
    target_minutes: int = Field(default=10, ge=8, le=12)
    shorts_count: int = Field(default=2, ge=2, le=4)
    language: str = "es"
    llm_provider: str = ""  # Empty uses the existing MoneyPrinterTurbo setting.
    voice: str = "es-PA-MargaritaNeural-Female"
    visual_provider: Literal["pexels", "pixabay", "local"] = "pexels"
    local_visuals: list[str] = Field(default_factory=list)
    asset_license_note: str = ""
    music_file: str = ""
    sound_effect_file: str = ""
    audio_license_note: str = ""
    subtitles: Literal["edge", "whisper"] = "edge"
    width: int = Field(default=640, ge=320, le=1920, multiple_of=32)
    threads: int = Field(default=1, ge=1, le=4)
    demo_scene_seconds: float = Field(default=2, ge=1, le=10)
    # Reservations are conservative user estimates, NOT provider invoices.
    allow_llm_requests: bool = False
    llm_estimate_usd: float = Field(default=0.10, ge=0, le=100, allow_inf_nan=False)
    budget_usd: float = Field(default=0, ge=0, le=1000, allow_inf_nan=False)

    @model_validator(mode="after")
    def licenses(self):
        if (self.music_file or self.sound_effect_file) and not self.audio_license_note:
            raise ValueError("Describe the rights/license for music and sound effects")
        if self.local_visuals and not self.asset_license_note:
            raise ValueError("Describe the rights/license for local visuals")
        if (
            self.mode == "live"
            and self.visual_provider == "local"
            and not self.local_visuals
        ):
            raise ValueError("Local visual provider needs at least one file")
        return self


class Canon(Model):
    characters: dict[str, str] = Field(default_factory=dict)
    locations: dict[str, str] = Field(default_factory=dict)
    events: list[str] = Field(default_factory=list)
    clues: list[str] = Field(default_factory=list)
    open_mysteries: list[str] = Field(default_factory=list)
    resolved_mysteries: list[str] = Field(default_factory=list)


class Scene(Model):
    narration: str = Field(min_length=10, max_length=6000)
    visual_terms: list[str] = Field(min_length=1, max_length=5)


class Short(Model):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4000)
    source_scenes: list[int] = Field(min_length=1, max_length=4)
    scene: Scene


class Episode(Model):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=15)
    scenes: list[Scene] = Field(min_length=2, max_length=30)
    shorts: list[Short] = Field(min_length=2, max_length=4)
    # A delta: old characters/events are retained, mysteries explicitly resolved.
    canon_delta: Canon

    @model_validator(mode="after")
    def references(self):
        if any(
            i < 0 or i >= len(self.scenes) for s in self.shorts for i in s.source_scenes
        ):
            raise ValueError("Short references an unknown scene")
        if sum(len(tag) + 1 for tag in self.tags) > 450:
            raise ValueError("Tags exceed YouTube metadata budget")
        return self


def merge_canon(old: Canon, delta: Canon) -> Canon:
    data = old.model_dump()
    for field in ("characters", "locations"):
        data[field].update(getattr(delta, field))
    for field in ("events", "clues", "open_mysteries", "resolved_mysteries"):
        data[field] = list(dict.fromkeys(data[field] + getattr(delta, field)))
    data["open_mysteries"] = [
        m for m in data["open_mysteries"] if m not in data["resolved_mysteries"]
    ]
    return Canon(**data)


class Idea(Model):
    title: str = Field(min_length=1, max_length=100)
    premise: str = Field(min_length=20, max_length=2000)
    central_mystery: str = Field(min_length=10, max_length=1000)


class Ideas(Model):
    items: list[Idea] = Field(min_length=3, max_length=5)
