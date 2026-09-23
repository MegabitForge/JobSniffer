from job_sniffer.sources.filters import get_source_filter_schema, known_source_keys
from job_sniffer.sources.registry import SOURCE_DEFINITIONS

ACTIVE_SOURCE_KEYS = tuple(
    definition.key for definition in SOURCE_DEFINITIONS if definition.status == "active"
)


def test_every_active_source_has_a_filter_schema() -> None:
    for key in ACTIVE_SOURCE_KEYS:
        schema = get_source_filter_schema(key)
        assert schema, f"Brak schematu filtrów dla źródła {key}"


def test_filter_keys_are_unique_within_a_schema() -> None:
    for key in ACTIVE_SOURCE_KEYS:
        schema = get_source_filter_schema(key)
        filter_keys = [item.key for item in schema]
        assert len(filter_keys) == len(set(filter_keys))


def test_only_multi_select_filters_have_options() -> None:
    for key in ACTIVE_SOURCE_KEYS:
        for item in get_source_filter_schema(key):
            if item.kind == "multi_select":
                assert item.options, f"Filtr {item.key} nie ma opcji"
            else:
                assert not item.options


def test_theprotocol_schema_contains_known_values() -> None:
    schema = {item.key: item for item in get_source_filter_schema("theprotocol")}
    specializations = {option.value for option in schema["specializations"].options}
    levels = {option.value for option in schema["levels"].options}
    work_modes = {option.value for option in schema["work_modes"].options}
    contract_types = {option.value for option in schema["contract_types"].options}

    assert {"backend", "frontend"} <= specializations
    assert "junior" in levels
    assert "zdalna" in work_modes
    assert {"kontrakt-b2b", "umowa-o-prace"} <= contract_types


def test_sources_without_keyword_support_have_location_only() -> None:
    for key in ("bulldogjob", "nofluffjobs"):
        schema = {item.key: item for item in get_source_filter_schema(key)}
        assert "keywords" not in schema
        assert "location" in schema


def test_known_source_keys_covers_all_schemas() -> None:
    assert known_source_keys() >= frozenset(ACTIVE_SOURCE_KEYS)


def test_theprotocol_locations_is_multi_select_with_all_slugs() -> None:
    schema = {item.key: item for item in get_source_filter_schema("theprotocol")}
    locations = schema["locations"]
    assert locations.kind == "multi_select"
    values = {option.value for option in locations.options}
    expected = {
        "warszawa",
        "krakow",
        "wroclaw",
        "gdansk",
        "poznan",
        "katowice",
        "lodz",
        "lublin",
        "rzeszow",
        "szczecin",
        "bialystok",
        "bydgoszcz",
        "torun",
        "gdynia",
        "opole",
        "zielona-gora",
        "kielce",
        "bielsko-biala",
        "olsztyn",
        "czestochowa",
        "sopot",
        "ue",
        "reszta-swiata",
    }
    assert values == expected
