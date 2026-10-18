from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from job_sniffer.sources import bulldogjob, nofluffjobs, olx, pracuj, theprotocol
from job_sniffer.sources.base import JobSource, ScanHooks
from job_sniffer.sources.filters import SourceFilter

SourceStatus = Literal["active", "not_implemented", "disabled"]


@dataclass(frozen=True)
class SourceDefinition:
    """Describes a job source: how it is shown, filtered and created for a scan."""

    key: str
    name: str
    status: SourceStatus
    description: str
    icon: str = ""
    filters: tuple[SourceFilter, ...] = ()
    create: Callable[[ScanHooks], JobSource] | None = None


SOURCE_DEFINITIONS: tuple[SourceDefinition, ...] = (
    SourceDefinition(
        key="pracuj",
        name="Pracuj.pl",
        status="active",
        description="",
        icon="pracuj.png",
        filters=pracuj.FILTERS,
        create=pracuj.PracujJobSource,
    ),
    SourceDefinition(
        key="olx",
        name="OLX",
        status="active",
        description="",
        icon="olx.png",
        filters=olx.FILTERS,
        create=olx.OlxJobSource,
    ),
    SourceDefinition(
        key="theprotocol",
        name="the:protocol",
        status="active",
        description="",
        icon="theprotocol.png",
        filters=theprotocol.FILTERS,
        create=theprotocol.TheProtocolJobSource,
    ),
    SourceDefinition(
        key="nofluffjobs",
        name="No Fluff Jobs",
        status="active",
        description="",
        icon="nofluffjobs.png",
        filters=nofluffjobs.FILTERS,
        create=nofluffjobs.NoFluffJobsSource,
    ),
    SourceDefinition(
        key="bulldogjob",
        name="Bulldogjob",
        status="active",
        description="",
        icon="bulldogjob.png",
        filters=bulldogjob.FILTERS,
        create=bulldogjob.BulldogjobSource,
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

_SOURCES_BY_KEY = {source.key: source for source in SOURCE_DEFINITIONS}


def get_source_definition(key: str) -> SourceDefinition:
    """Return the definition of a known source; raises KeyError for an unknown key."""
    return _SOURCES_BY_KEY[key]
