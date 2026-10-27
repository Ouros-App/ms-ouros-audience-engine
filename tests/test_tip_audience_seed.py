import pytest

from ouros_audience_engine.domain.tip_audience_catalog import default_tip_audiences
from ouros_audience_engine.repositories.metadata_postgres import Tip, TipAudienceLink
from ouros_audience_engine.services.tip_audience_seed import (
    TipAudienceSeedError,
    seed_tip_audiences,
)


class MemoryDefinitions:
    def __init__(self) -> None:
        self.items = {}
        self.created: list[str] = []

    def get(self, key: str):
        return self.items.get(key)

    def create(self, definition) -> None:
        self.created.append(definition.key)
        self.items[definition.key] = definition


class MemoryMetadata:
    def __init__(self) -> None:
        self.tips = [Tip(item.tip_id, f"Tip {item.tip_id}") for item in default_tip_audiences()]
        self.links: dict[str, list[TipAudienceLink]] = {}

    def list_tips(self, search: str | None, limit: int, offset: int) -> list[Tip]:
        return self.tips[offset : offset + limit]

    def tip_links_for_audiences(self, audience_keys: list[str]) -> dict[str, list[TipAudienceLink]]:
        return {key: self.links.get(key, []) for key in audience_keys}

    def link_tip(
        self,
        audience_key: str,
        tip_id: int,
        rule_version: str,
        *,
        enabled: bool = True,
    ) -> bool:
        self.links[audience_key] = [TipAudienceLink(tip_id, enabled)]
        return True


def test_seed_creates_all_tip_definitions_disabled_and_is_idempotent() -> None:
    definitions = MemoryDefinitions()
    metadata = MemoryMetadata()

    first = seed_tip_audiences(definitions, metadata)
    second = seed_tip_audiences(definitions, metadata)

    assert (first.created_count, first.linked_count) == (32, 32)
    assert (second.created_count, second.linked_count) == (0, 0)
    assert len(definitions.items) == 32
    assert all(not item.enabled for item in definitions.items.values())
    assert all(len(links) == 1 and not links[0].enabled for links in metadata.links.values())


def test_seed_refuses_to_overwrite_any_existing_custom_rule() -> None:
    definitions = MemoryDefinitions()
    metadata = MemoryMetadata()
    catalog = default_tip_audiences()
    definitions.items[catalog[0].key] = catalog[0].model_copy(update={"version": "2"})

    with pytest.raises(TipAudienceSeedError, match="refusing to overwrite"):
        seed_tip_audiences(definitions, metadata)

    assert definitions.created == []
    assert metadata.links == {}
