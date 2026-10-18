from job_sniffer.models import JobSearch
from job_sniffer.sources.theprotocol import _with_page, build_search_url


def test_profile_filters_build_a_paginated_theprotocol_listing_url() -> None:
    search = JobSearch(
        filters={
            "keywords": "python",
            "specializations": ["backend"],
            "technologies": ["c#", "java"],
            "levels": ["junior"],
            "locations": ["Wrocław", "Łódź"],
            "work_modes": ["zdalna"],
            "contract_types": ["kontrakt-b2b"],
        },
    )

    url = build_search_url(search)

    assert url == (
        "https://theprotocol.it/filtry/backend;sp/c%23,java;t/junior;p/wroclaw,lodz;wp"
        "/zdalna;rw/kontrakt-b2b;c?kw=python"
    )
    assert _with_page(url, 1) == url
    assert _with_page(url, 2) == f"{url}&pageNumber=2"
