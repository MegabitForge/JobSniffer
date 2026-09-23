from job_sniffer.models import JobSearch
from job_sniffer.sources.theprotocol import _with_page, build_search_url


def test_full_filters_build_filtry_url() -> None:
    search = JobSearch(
        keywords="",
        location="",
        source_filters={
            "specializations": ["backend", "frontend"],
            "technologies": ["java", ".net"],
            "levels": ["junior"],
            "locations": ["Wrocław", "Warszawa"],
            "work_modes": ["zdalna"],
            "contract_types": ["kontrakt-b2b"],
        },
    )
    assert build_search_url(search) == (
        "https://theprotocol.it/filtry/backend,frontend;sp/java,.net;t/junior;p"
        "/wroclaw,warszawa;wp/zdalna;rw/kontrakt-b2b;c"
    )


def test_filters_with_keywords_add_kw_query() -> None:
    search = JobSearch(
        keywords="python",
        location="",
        source_filters={"technologies": ["java"]},
    )
    assert build_search_url(search) == "https://theprotocol.it/filtry/java;t?kw=python"


def test_technologies_only_build_single_segment() -> None:
    search = JobSearch(
        keywords="",
        location="",
        source_filters={"technologies": ["java", "angular"]},
    )
    assert build_search_url(search) == "https://theprotocol.it/filtry/java,angular;t"


def test_technology_hash_is_url_encoded() -> None:
    search = JobSearch(
        keywords="",
        location="",
        source_filters={"technologies": ["c#"]},
    )
    assert build_search_url(search) == "https://theprotocol.it/filtry/c%23;t"


def test_locations_are_slugified() -> None:
    search = JobSearch(
        keywords="",
        location="",
        source_filters={"locations": ["Wrocław", "Łódź"]},
    )
    assert build_search_url(search) == "https://theprotocol.it/filtry/wroclaw,lodz;wp"


def test_empty_filters_fall_back_to_poland_listing() -> None:
    search = JobSearch(keywords="", location="", source_filters={})
    assert build_search_url(search) == "https://theprotocol.it/praca"


def test_empty_filter_values_are_ignored() -> None:
    search = JobSearch(
        keywords="",
        location="",
        source_filters={"specializations": [], "technologies": "   "},
    )
    assert build_search_url(search) == "https://theprotocol.it/praca"


def test_poland_location_with_keywords_builds_praca_url() -> None:
    search = JobSearch(keywords="java", location="polska")
    assert build_search_url(search) == "https://theprotocol.it/praca?kw=java"


def test_foreign_location_without_filters_builds_filtry_url() -> None:
    search = JobSearch(keywords="", location="Berlin")
    assert build_search_url(search) == "https://theprotocol.it/filtry/berlin;wp"


def test_with_page_preserves_query_and_adds_page_number() -> None:
    url = "https://theprotocol.it/filtry/java;t?kw=python"
    assert _with_page(url, 1) == url
    assert _with_page(url, 3) == "https://theprotocol.it/filtry/java;t?kw=python&pageNumber=3"


def test_location_slugs_pass_through_unchanged() -> None:
    search = JobSearch(
        keywords="",
        location="",
        source_filters={
            "locations": ["warszawa", "ue", "reszta-swiata"],
        },
    )
    assert build_search_url(search) == "https://theprotocol.it/filtry/warszawa,ue,reszta-swiata;wp"
