from __future__ import annotations

import pytest
from pydantic import ValidationError

from fire_viewer.core.config import Settings
from fire_viewer.core.research_sources import (
    DEFAULT_RESEARCH_ALLOWED_DOMAINS,
    DEFAULT_RESEARCH_SEARCH_TEMPLATES,
    SOURCE_REGISTRY_VERSION,
    TRUSTED_RESEARCH_SOURCES,
)


def test_default_research_registry_is_versioned_and_separates_providers() -> None:
    settings = Settings(_env_file=None)

    assert settings.agent_research_source_registry_version == SOURCE_REGISTRY_VERSION
    assert tuple(settings.agent_research_allowed_domains) == DEFAULT_RESEARCH_ALLOWED_DOMAINS
    assert settings.agent_research_search_templates == DEFAULT_RESEARCH_SEARCH_TEMPLATES
    assert len(TRUSTED_RESEARCH_SOURCES) == len(DEFAULT_RESEARCH_ALLOWED_DOMAINS)
    assert not set(DEFAULT_RESEARCH_SEARCH_TEMPLATES) & set(DEFAULT_RESEARCH_ALLOWED_DOMAINS)


@pytest.mark.parametrize(
    "templates",
    [
        {"mairie-die.fr": "https://mairie-die.fr/search?q={query}"},
        {"search.example": "http://search.example/search?q={query}"},
        {"search.example": "https://other.example/search?q={query}"},
        {"search.example": "https://search.example/search"},
    ],
)
def test_settings_reject_unsafe_search_provider_templates(
    templates: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            agent_research_enabled=True,
            agent_research_allowed_domains=["mairie-die.fr"],
            agent_research_search_templates=templates,
        )


def test_settings_reject_domain_without_executable_policy() -> None:
    with pytest.raises(ValidationError, match="executable source policy"):
        Settings(
            _env_file=None,
            agent_research_enabled=True,
            agent_research_allowed_domains=["unregistered.example"],
            agent_research_search_templates={
                "search.example": "https://search.example/search?q={query}"
            },
        )


def test_settings_rejects_registry_version_override() -> None:
    with pytest.raises(ValidationError, match="must match the executable registry"):
        Settings(
            _env_file=None,
            agent_research_source_registry_version="stale-registry-v0",
        )
