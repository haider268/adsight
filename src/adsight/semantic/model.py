"""The semantic model: a YAML mapping from canonical ad concepts to a dataset's columns."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

MeasureName = Literal["spend", "impressions", "clicks", "link_clicks", "reach", "conversions"]
MEASURES: tuple[MeasureName, ...] = (
    "spend",
    "impressions",
    "clicks",
    "link_clicks",
    "reach",
    "conversions",
)
REQUIRED_MEASURES: tuple[MeasureName, ...] = ("spend", "impressions", "conversions")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Fact(_Frozen):
    table: str
    date: str
    measures: dict[MeasureName, str]  # canonical measure -> column

    @model_validator(mode="after")
    def _required_measures(self) -> "Fact":
        missing = [m for m in REQUIRED_MEASURES if m not in self.measures]
        if missing:
            raise ValueError(f"required measures not mapped: {', '.join(missing)}")
        return self


class Level(_Frozen):
    """One level of the entity hierarchy (account, campaign, ...).

    A level whose table is the fact table lives in the fact rows (flat data). parent_key
    is the column in THIS level's table holding the parent level's key.
    """

    name: str
    table: str
    key: str
    label: str | None = None
    parent_key: str | None = None


class Attribute(_Frozen):
    """A categorical column usable for group-by."""

    name: str
    table: str
    column: str
    time_varying: bool = False  # its value changes over time (e.g. status), so it is a
    # snapshot at the end of the data and must not be used to group past periods


class ChangeLog(_Frozen):
    table: str
    date: str
    entity_type: str | None = None
    entity_id: str
    change_type: str
    old_value: str | None = None
    new_value: str | None = None


class SemanticModel(_Frozen):
    version: Literal[1]
    name: str
    currency: str = "USD"
    conversion_name: str = "conversion"
    grain: Literal["day", "week"]
    fact: Fact
    levels: list[Level]  # top to bottom
    attributes: list[Attribute] = []
    change_log: ChangeLog | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "SemanticModel":
        if not self.levels:
            raise ValueError("at least one level is required")
        for kind, names in (
            ("level", [lv.name for lv in self.levels]),
            ("attribute", [a.name for a in self.attributes]),
        ):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate {kind} names: {', '.join(dupes)}")
        for lv in self.levels[1:]:
            if lv.parent_key is None and lv.table != self.fact.table:
                raise ValueError(
                    f"level {lv.name!r} has its own table {lv.table!r} but no parent_key"
                )
        return self

    def level(self, name: str) -> Level:
        for lv in self.levels:
            if lv.name == name:
                return lv
        raise KeyError(f"unknown level {name!r}")

    def attribute(self, name: str) -> Attribute:
        for attr in self.attributes:
            if attr.name == name:
                return attr
        raise KeyError(f"unknown attribute {name!r}")

    def in_fact(self, level: Level) -> bool:
        return level.table == self.fact.table


def load_model(path: Path) -> SemanticModel:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SemanticModel.model_validate(data)


def dump_model(model: SemanticModel, path: Path) -> None:
    data = model.model_dump(mode="json", exclude_none=True)
    for attr in data.get("attributes", []):
        if not attr.get("time_varying"):
            attr.pop("time_varying", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8", newline="\n")
