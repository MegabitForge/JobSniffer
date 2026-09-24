from importlib import resources

from job_sniffer.sources.registry import SOURCE_DEFINITIONS


def test_every_source_definition_has_bundled_icon_file() -> None:
    for source_def in SOURCE_DEFINITIONS:
        assert source_def.icon, f"Source {source_def.key} has no icon in registry"
        icon = resources.files("job_sniffer").joinpath("assets", "icons", source_def.icon)
        assert icon.is_file(), f"Missing assets/icons/{source_def.icon} file"
