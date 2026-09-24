from job_sniffer.sources.filters import (
    SourceFilter,
    get_source_filter_schema,
    parse_text_filter_value,
    text_filter_value,
)
from job_sniffer.sources.registry import ACTIVE_SOURCES

ACTIVE_SOURCE_KEYS = tuple(source_def.key for source_def in ACTIVE_SOURCES)


def test_every_active_source_has_a_filter_schema() -> None:
    for key in ACTIVE_SOURCE_KEYS:
        schema = get_source_filter_schema(key)
        assert schema, f"Missing filter schema for source {key}"


def test_filter_keys_are_unique_within_a_schema() -> None:
    for key in ACTIVE_SOURCE_KEYS:
        schema = get_source_filter_schema(key)
        filter_keys = [item.key for item in schema]
        assert len(filter_keys) == len(set(filter_keys))


def test_only_multi_select_filters_have_options() -> None:
    for key in ACTIVE_SOURCE_KEYS:
        for item in get_source_filter_schema(key):
            if item.kind == "multi_select":
                assert item.options, f"Filter {item.key} has no options"
            else:
                assert not item.options


def test_theprotocol_schema_contains_known_values() -> None:
    schema = {item.key: item for item in get_source_filter_schema("theprotocol")}

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
    schema = {item.key: item for item in get_source_filter_schema("theprotocol")}
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


def test_parse_text_filter_value_single_text() -> None:
    assert parse_text_filter_value("text", "  Warszawa  ") == "Warszawa"
    assert parse_text_filter_value("text", "   ") is None
    assert parse_text_filter_value("text", "") is None


def test_parse_text_filter_value_multi_text_splits_on_commas() -> None:
    assert parse_text_filter_value("multi_text", "java, .net , c#") == ["java", ".net", "c#"]
    assert parse_text_filter_value("multi_text", "java") == ["java"]
    assert parse_text_filter_value("multi_text", " , , ") is None
