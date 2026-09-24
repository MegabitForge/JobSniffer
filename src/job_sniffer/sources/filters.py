from dataclasses import dataclass
from typing import Literal

FilterKind = Literal["text", "multi_text", "multi_select"]


@dataclass(frozen=True)
class FilterOption:
    value: str
    label: str


@dataclass(frozen=True)
class SourceFilter:
    key: str
    label: str
    kind: FilterKind
    hint: str = ""
    options: tuple[FilterOption, ...] = ()

    @property
    def is_text(self) -> bool:
        """Whether the filter is edited as free text (single or comma-separated values)."""
        return self.kind in ("text", "multi_text")


_THEPROTOCOL_SPECIALIZATIONS = (
    FilterOption("backend", "Backend"),
    FilterOption("frontend", "Frontend"),
    FilterOption("devops", "DevOps"),
    FilterOption("fullstack", "Fullstack"),
    FilterOption("gamedev", "GameDev"),
    FilterOption("mobile", "Mobile"),
    FilterOption("embedded", "Embedded"),
    FilterOption("architecture", "Architektura"),
    FilterOption("qa-testing", "QA / Testing"),
    FilterOption("security", "Bezpieczeństwo"),
    FilterOption("ai-ml", "AI / ML"),
    FilterOption("big-data-science", "Big Data / Data Science"),
    FilterOption("helpdesk", "Helpdesk"),
    FilterOption("it-admin", "Administracja IT"),
    FilterOption("agile", "Agile"),
    FilterOption("product-management", "Product Management"),
    FilterOption("project-management", "Project Management"),
    FilterOption("ux-ui", "UX / UI"),
    FilterOption("business-analytics", "Business Analytics"),
    FilterOption("sap-erp", "SAP / ERP"),
    FilterOption("system-analytics", "System Analytics"),
    FilterOption("data-analytics-bi", "Data Analytics / BI"),
)

_THEPROTOCOL_LEVELS = (
    FilterOption("trainee", "Trainee"),
    FilterOption("assistant", "Assistant"),
    FilterOption("junior", "Junior"),
    FilterOption("mid", "Mid"),
    FilterOption("senior", "Senior"),
    FilterOption("expert", "Expert"),
    FilterOption("lead", "Lead"),
)

_THEPROTOCOL_WORK_MODES = (
    FilterOption("zdalna", "Zdalna"),
    FilterOption("hybrydowa", "Hybrydowa"),
    FilterOption("stacjonarna", "Stacjonarna"),
)

_THEPROTOCOL_CONTRACT_TYPES = (
    FilterOption("kontrakt-b2b", "Kontrakt B2B"),
    FilterOption("umowa-o-prace", "Umowa o pracę"),
    FilterOption("umowa-zlecenie", "Umowa zlecenie"),
    FilterOption("umowa-o-dzielo", "Umowa o dzieło"),
    FilterOption("umowa-na-zastepstwo", "Umowa na zastępstwo"),
    FilterOption("umowa-agencyjna", "Umowa agencyjna"),
    FilterOption("umowa-o-prace-tymczasowa", "Umowa o pracę tymczasową"),
    FilterOption("umowa-o-staz-praktyki", "Umowa o staż / praktyki"),
)

_THEPROTOCOL_LOCATIONS = (
    FilterOption("warszawa", "Warszawa"),
    FilterOption("krakow", "Kraków"),
    FilterOption("wroclaw", "Wrocław"),
    FilterOption("gdansk", "Gdańsk"),
    FilterOption("poznan", "Poznań"),
    FilterOption("katowice", "Katowice"),
    FilterOption("lodz", "Łódź"),
    FilterOption("lublin", "Lublin"),
    FilterOption("rzeszow", "Rzeszów"),
    FilterOption("szczecin", "Szczecin"),
    FilterOption("bialystok", "Białystok"),
    FilterOption("bydgoszcz", "Bydgoszcz"),
    FilterOption("torun", "Toruń"),
    FilterOption("gdynia", "Gdynia"),
    FilterOption("opole", "Opole"),
    FilterOption("zielona-gora", "Zielona Góra"),
    FilterOption("kielce", "Kielce"),
    FilterOption("bielsko-biala", "Bielsko-Biała"),
    FilterOption("olsztyn", "Olsztyn"),
    FilterOption("czestochowa", "Częstochowa"),
    FilterOption("sopot", "Sopot"),
    FilterOption("ue", "UE"),
    FilterOption("reszta-swiata", "Reszta świata"),
)

_SOURCE_FILTER_SCHEMAS: dict[str, tuple[SourceFilter, ...]] = {
    "pracuj": (
        SourceFilter(key="keywords", label="Słowa kluczowe", kind="text"),
        SourceFilter(key="location", label="Lokalizacja", kind="text"),
    ),
    "olx": (
        SourceFilter(key="keywords", label="Słowa kluczowe", kind="text"),
        SourceFilter(key="location", label="Lokalizacja", kind="text"),
    ),
    "nofluffjobs": (SourceFilter(key="location", label="Lokalizacja", kind="text"),),
    "bulldogjob": (SourceFilter(key="location", label="Lokalizacja", kind="text"),),
    "theprotocol": (
        SourceFilter(key="keywords", label="Słowa kluczowe", kind="text"),
        SourceFilter(
            key="specializations",
            label="Specjalizacje",
            kind="multi_select",
            options=_THEPROTOCOL_SPECIALIZATIONS,
        ),
        SourceFilter(
            key="technologies",
            label="Technologie i narzędzia",
            kind="multi_text",
            hint="np. java, .net, c#",
        ),
        SourceFilter(
            key="levels",
            label="Poziom stanowiska",
            kind="multi_select",
            options=_THEPROTOCOL_LEVELS,
        ),
        SourceFilter(
            key="locations",
            label="Lokalizacje",
            kind="multi_select",
            options=_THEPROTOCOL_LOCATIONS,
        ),
        SourceFilter(
            key="work_modes",
            label="Tryb pracy",
            kind="multi_select",
            options=_THEPROTOCOL_WORK_MODES,
        ),
        SourceFilter(
            key="contract_types",
            label="Rodzaj umowy",
            kind="multi_select",
            options=_THEPROTOCOL_CONTRACT_TYPES,
        ),
    ),
}


def get_source_filter_schema(source_key: str) -> tuple[SourceFilter, ...]:
    return _SOURCE_FILTER_SCHEMAS.get(source_key, ())


def text_filter_value(stored: str | list[str] | None) -> str:
    """Flatten a stored text filter value into one string (display or search text)."""
    if isinstance(stored, list):
        return ", ".join(stored)
    if isinstance(stored, str):
        return stored
    return ""


def parse_text_filter_value(kind: FilterKind, raw: str) -> str | list[str] | None:
    """Parse field text into a stored filter value; None means "no filter"."""
    stripped = raw.strip()
    if not stripped:
        return None
    if kind == "text":
        return stripped
    values = [part.strip() for part in stripped.split(",") if part.strip()]
    return values or None
