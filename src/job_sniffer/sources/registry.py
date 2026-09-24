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
    icon: str = ""


SOURCE_DEFINITIONS: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        key="pracuj",
        name="Pracuj.pl",
        status="active",
        description="",
        icon="pracuj.png",
    ),
    SourceDefinition(
        key="olx",
        name="OLX",
        status="active",
        description="",
        icon="olx.png",
    ),
    SourceDefinition(
        key="theprotocol",
        name="the:protocol",
        status="active",
        description="",
        icon="theprotocol.png",
    ),
    SourceDefinition(
        key="nofluffjobs",
        name="No Fluff Jobs",
        status="active",
        description="",
        icon="nofluffjobs.png",
    ),
    SourceDefinition(
        key="bulldogjob",
        name="Bulldogjob",
        status="active",
        description="",
        icon="bulldogjob.png",
    ),
    SourceDefinition(
        key="linkedin",
        name="LinkedIn",
        status="disabled",
        description="Paused because of aggressive blocking.",
        icon="linkedin.png",
    ),
)

ACTIVE_SOURCES: tuple[SourceDefinition, ...] = tuple(
    source for source in SOURCE_DEFINITIONS if source.status == "active"
)
