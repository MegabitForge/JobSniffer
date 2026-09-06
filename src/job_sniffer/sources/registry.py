from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SourceStatus = Literal["active", "not_implemented", "disabled"]


@dataclass(frozen=True)
class SourceDefinition:
    """Describes a job source visible in application settings."""

    key: str
    name: str
    status: SourceStatus
    description: str


SOURCE_DEFINITIONS: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        key="pracuj",
        name="Pracuj.pl",
        status="active",
        description="",
    ),
    SourceDefinition(
        key="olx",
        name="OLX",
        status="active",
        description="",
    ),
    SourceDefinition(
        key="theprotocol",
        name="the:protocol",
        status="active",
        description="",
    ),
    SourceDefinition(
        key="nofluffjobs",
        name="No Fluff Jobs",
        status="active",
        description="",
    ),
    SourceDefinition(
        key="bulldogjob",
        name="Bulldogjob",
        status="active",
        description="",
    ),
    SourceDefinition(
        key="linkedin",
        name="LinkedIn",
        status="disabled",
        description="Paused because of aggressive blocking.",
    ),
)
