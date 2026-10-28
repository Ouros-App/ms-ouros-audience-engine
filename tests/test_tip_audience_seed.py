import pytest

from ouros_audience_engine.domain.tip_audience_catalog import (
    daily_water_meter_audience,
    default_tip_audiences,
)
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


def test_seed_creates_disabled_observation_rules_and_one_enabled_meter_audience() -> None:
    definitions = MemoryDefinitions()
    metadata = MemoryMetadata()

    first = seed_tip_audiences(definitions, metadata)
    second = seed_tip_audiences(definitions, metadata)

    meter_key = daily_water_meter_audience().key
    assert (first.created_count, first.linked_count) == (33, 33)
    assert (second.created_count, second.linked_count) == (0, 0)
    assert len(definitions.items) == 33
    assert {key for key, item in definitions.items.items() if item.enabled} == {meter_key}
    assert len(metadata.links) == 33
    assert metadata.links[meter_key] == [TipAudienceLink(121, True)]
    assert all(
        not links[0].enabled
        for key, links in metadata.links.items()
        if key != meter_key
    )


def test_seed_repairs_drifted_tip_link_enabled_states() -> None:
    definitions = MemoryDefinitions()
    metadata = MemoryMetadata()
    seed_tip_audiences(definitions, metadata)

    enabled_item = daily_water_meter_audience()
    disabled_item = default_tip_audiences()[0]
    metadata.links[enabled_item.key] = [TipAudienceLink(enabled_item.tip_id, False)]
    metadata.links[disabled_item.key] = [TipAudienceLink(disabled_item.tip_id, True)]

    result = seed_tip_audiences(definitions, metadata)

    assert result.linked_count == 2
    assert metadata.links[enabled_item.key] == [TipAudienceLink(enabled_item.tip_id, True)]
    assert metadata.links[disabled_item.key] == [TipAudienceLink(disabled_item.tip_id, False)]


def test_seed_refuses_to_overwrite_any_existing_custom_rule() -> None:
    definitions = MemoryDefinitions()
    metadata = MemoryMetadata()
    catalog = default_tip_audiences()
    definitions.items[catalog[0].key] = catalog[0].model_copy(update={"version": "2"})

    with pytest.raises(TipAudienceSeedError, match="refusing to overwrite"):
        seed_tip_audiences(definitions, metadata)

    assert definitions.created == []
    assert metadata.links == {}
