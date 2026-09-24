from job_sniffer.models import JobSearch
from job_sniffer.sources.theprotocol import _with_page, build_search_url


def test_full_filters_build_filters_url() -> None:
    search = JobSearch(
        filters={
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
    search = JobSearch(filters={"technologies": ["java"], "keywords": "python"})
    assert build_search_url(search) == "https://theprotocol.it/filtry/java;t?kw=python"


def test_technologies_only_build_single_segment() -> None:
    search = JobSearch(filters={"technologies": ["java", "angular"]})
    assert build_search_url(search) == "https://theprotocol.it/filtry/java,angular;t"


def test_technology_hash_is_url_encoded() -> None:
    search = JobSearch(filters={"technologies": ["c#"]})
    assert build_search_url(search) == "https://theprotocol.it/filtry/c%23;t"


def test_locations_are_slugified() -> None:
    search = JobSearch(filters={"locations": ["Wrocław", "Łódź"]})
    assert build_search_url(search) == "https://theprotocol.it/filtry/wroclaw,lodz;wp"


def test_empty_filters_fall_back_to_praca() -> None:
    search = JobSearch(filters={})
    assert build_search_url(search) == "https://theprotocol.it/praca"


def test_keyword_without_filters_builds_praca_url() -> None:
    search = JobSearch(filters={"keywords": "python"})
    assert build_search_url(search) == "https://theprotocol.it/praca?kw=python"


def test_keywords_list_is_flattened_into_kw_query() -> None:
    search = JobSearch(filters={"keywords": ["python", "remote"]})
    assert build_search_url(search) == "https://theprotocol.it/praca?kw=python%2C%20remote"


def test_empty_filter_values_are_ignored() -> None:
    search = JobSearch(filters={"specializations": [], "technologies": "   "})
    assert build_search_url(search) == "https://theprotocol.it/praca"


def test_locations_slugifying_to_empty_are_skipped() -> None:
    search = JobSearch(filters={"locations": ["???", "Wrocław"]})
    assert build_search_url(search) == "https://theprotocol.it/filtry/wroclaw;wp"


def test_all_locations_slugifying_to_empty_drop_segment() -> None:
    search = JobSearch(filters={"locations": ["???"]})
    assert build_search_url(search) == "https://theprotocol.it/praca"


def test_with_page_preserves_query_and_adds_page_number() -> None:
    url = "https://theprotocol.it/filtry/java;t?kw=python"
    assert _with_page(url, 1) == url
    assert _with_page(url, 3) == "https://theprotocol.it/filtry/java;t?kw=python&pageNumber=3"


def test_location_slugs_pass_through_unchanged() -> None:
    search = JobSearch(filters={"locations": ["warszawa", "ue", "reszta-swiata"]})
    assert build_search_url(search) == "https://theprotocol.it/filtry/warszawa,ue,reszta-swiata;wp"
