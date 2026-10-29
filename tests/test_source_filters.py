from collections.abc import Callable

import pytest

from job_sniffer.models import FilterValue, JobSearch, text_filter_value
from job_sniffer.sources import bulldogjob, nofluffjobs, olx, pracuj, theprotocol
from job_sniffer.sources.base import ScanHooks
from job_sniffer.sources.filters import FilterKind, SourceFilter, parse_text_filter_value
from job_sniffer.sources.registry import ACTIVE_SOURCES, SourceDefinition

URL_BUILDERS: dict[str, Callable[[JobSearch], str]] = {
    "pracuj": pracuj.build_search_url,
    "olx": olx.build_search_url,
    "theprotocol": theprotocol.build_search_url,
    "nofluffjobs": nofluffjobs.build_search_url,
    "bulldogjob": bulldogjob.build_search_url,
}


def _sample_value(source_filter: SourceFilter) -> FilterValue:
    if source_filter.kind == "multi_select":
        return [source_filter.options[0].value]
    if source_filter.kind == "multi_text":
        return ["Wrocław"]
    return "Wrocław"


@pytest.mark.parametrize("source_def", ACTIVE_SOURCES, ids=lambda source_def: source_def.key)
def test_every_filter_shown_for_a_source_changes_its_search_url(
    source_def: SourceDefinition,
) -> None:
    build_search_url = URL_BUILDERS[source_def.key]
    unfiltered_url = build_search_url(JobSearch(filters={}))

    assert source_def.filters, f"Źródło {source_def.key} nie ma filtrów"
    for source_filter in source_def.filters:
        search = JobSearch(filters={source_filter.key: _sample_value(source_filter)})
        assert build_search_url(search) != unfiltered_url, (
            f"Filtr {source_filter.key} nie zmienia wyszukiwania w {source_def.key}"
        )


@pytest.mark.parametrize("source_def", ACTIVE_SOURCES, ids=lambda source_def: source_def.key)
def test_active_source_creates_a_scraper_reporting_its_key(source_def: SourceDefinition) -> None:
    assert source_def.create is not None
    assert source_def.create(ScanHooks()).source_name == source_def.key


@pytest.mark.parametrize(
    ("kind", "typed", "shown_after_reload"),
    [
        ("text", "  Wrocław ", "Wrocław"),
        ("multi_text", "java, .net , c#", "java, .net, c#"),
        ("multi_text", " , ", ""),
    ],
)
def test_filter_typed_in_the_profile_form_is_shown_the_same_after_reload(
    kind: FilterKind, typed: str, shown_after_reload: str
) -> None:
    assert text_filter_value(parse_text_filter_value(kind, typed)) == shown_after_reload
