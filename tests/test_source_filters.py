from collections.abc import Callable

import pytest

from job_sniffer.models import FilterValue, JobSearch, text_filter_value
from job_sniffer.sources import bulldogjob, nofluffjobs, olx, pracuj, theprotocol
from job_sniffer.sources.base import ScanHooks
from job_sniffer.sources.filters import SourceFilter, parse_text_filter_value
from job_sniffer.sources.registry import ACTIVE_SOURCES, SourceDefinition, get_source_definition

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


def test_filter_keys_are_unique_within_a_schema() -> None:
    for source_def in ACTIVE_SOURCES:
        filter_keys = [item.key for item in source_def.filters]
        assert len(filter_keys) == len(set(filter_keys))


def test_only_multi_select_filters_have_options() -> None:
    for source_def in ACTIVE_SOURCES:
        for item in source_def.filters:
            if item.kind == "multi_select":
                assert item.options, f"Filter {item.key} has no options"
            else:
                assert not item.options


def test_theprotocol_schema_contains_known_values() -> None:
    schema = {item.key: item for item in get_source_definition("theprotocol").filters}

    required_keys = {"specializations", "levels", "work_modes", "contract_types"}
    assert required_keys <= schema.keys(), (
        f"Required keys are missing: {required_keys - schema.keys()}"
    )

    def get_values(key: str) -> set[str]:
        filter_obj = schema[key]
        assert getattr(filter_obj, "options", None) is not None, f"Filter {key} has no option"
        return {option.value for option in filter_obj.options}

    assert {"backend", "frontend"} <= get_values("specializations")
    assert "junior" in get_values("levels")
    assert "zdalna" in get_values("work_modes")
    assert {"kontrakt-b2b", "umowa-o-prace"} <= get_values("contract_types")


def test_theprotocol_locations_is_multi_select() -> None:
    schema = {item.key: item for item in get_source_definition("theprotocol").filters}
    locations = schema["locations"]
    assert locations.kind == "multi_select"


def test_is_text_covers_text_and_multi_text_kinds() -> None:
    assert SourceFilter(key="k", label="L", kind="text").is_text
    assert SourceFilter(key="k", label="L", kind="multi_text").is_text
    assert not SourceFilter(key="k", label="L", kind="multi_select").is_text


def test_text_filter_value_formats_stored_values() -> None:
    assert text_filter_value(None) == ""
    assert text_filter_value("") == ""
    assert text_filter_value("Warszawa") == "Warszawa"
    assert text_filter_value(["java", ".net"]) == "java, .net"


def test_job_search_reads_text_and_multi_value_filters() -> None:
    search = JobSearch(filters={"keywords": "  python ", "levels": ["junior", " ", "mid "]})

    assert search.text("keywords") == "python"
    assert search.text("location") == ""
    assert search.values("levels") == ["junior", "mid"]
    assert search.values("keywords") == ["python"]
    assert search.values("location") == []


def test_parse_text_filter_value_single_text() -> None:
    assert parse_text_filter_value("text", "  Warszawa  ") == "Warszawa"
    assert parse_text_filter_value("text", "   ") is None
    assert parse_text_filter_value("text", "") is None


def test_parse_text_filter_value_multi_text_splits_on_commas() -> None:
    assert parse_text_filter_value("multi_text", "java, .net , c#") == ["java", ".net", "c#"]
    assert parse_text_filter_value("multi_text", "java") == ["java"]
    assert parse_text_filter_value("multi_text", " , , ") is None
