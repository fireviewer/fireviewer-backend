"""Versioned runtime registry for private public-source research."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

ResearchSourceKind = Literal[
    "authority",
    "emergency_service",
    "satellite",
    "weather",
    "air_quality",
    "context",
    "directory",
    "press",
]
ResearchSourceScope = Literal["national", "regional", "departmental", "local", "global"]
ResearchConfidence = Literal["A+", "A", "B", "lead"]
ResearchPublicationPolicy = Literal[
    "facts_with_attribution",
    "dataset_license_required",
    "per_item_license_check",
    "private_analysis_only",
]


@dataclass(frozen=True, slots=True)
class TrustedResearchSource:
    domain: str
    label: str
    kind: ResearchSourceKind
    scope: ResearchSourceScope
    confidence_level: ResearchConfidence
    claim_types: tuple[str, ...]
    publication_policy: ResearchPublicationPolicy
    minimum_refresh_minutes: int


SOURCE_REGISTRY_VERSION = "firewarning-fr-sources-2026-07-19-v1"

OPERATIONAL_CLAIMS = (
    "operational_confirmation",
    "official_update",
    "fire_progression",
    "burned_area",
    "evacuation_and_shelter",
    "population_instruction",
    "access_restriction",
    "personnel_engaged",
    "ground_resources_engaged",
    "aircraft_engaged",
    "casualties_and_damage",
    "service_disruption",
    "public_relief_and_donations",
    "media_asset",
)
SATELLITE_CLAIMS = ("thermal_anomaly", "burned_area", "satellite_imagery")
CONTEXT_CLAIMS = ("historical_context", "prevention", "risk_exposure")
PRESS_CLAIMS = (
    "reported_situational_claim",
    "reported_witness_account",
    "media_asset",
)
_WEATHER_SOURCES: tuple[tuple[str, str, ResearchSourceScope, ResearchPublicationPolicy], ...] = (
    ("meteofrance.com", "Météo-France", "national", "per_item_license_check"),
    (
        "vigilance.meteofrance.fr",
        "Vigilance Météo-France",
        "national",
        "per_item_license_check",
    ),
    (
        "portail-api.meteofrance.fr",
        "API Météo-France",
        "national",
        "dataset_license_required",
    ),
    (
        "meteo.data.gouv.fr",
        "Données Météo-France",
        "national",
        "dataset_license_required",
    ),
    (
        "donneespubliques.meteofrance.fr",
        "Ancien portail Météo-France",
        "national",
        "dataset_license_required",
    ),
)
_PRESS_SOURCES: tuple[tuple[str, str, ResearchSourceScope], ...] = (
    ("france3-regions.francetvinfo.fr", "France 3 Régions", "regional"),
    ("francebleu.fr", "France Bleu", "regional"),
    ("ledauphine.com", "Le Dauphiné Libéré", "regional"),
)


def _source(
    domain: str,
    label: str,
    kind: ResearchSourceKind,
    scope: ResearchSourceScope,
    confidence: ResearchConfidence,
    claims: tuple[str, ...],
    publication: ResearchPublicationPolicy,
    refresh_minutes: int,
) -> TrustedResearchSource:
    return TrustedResearchSource(
        domain=domain,
        label=label,
        kind=kind,
        scope=scope,
        confidence_level=confidence,
        claim_types=claims,
        publication_policy=publication,
        minimum_refresh_minutes=refresh_minutes,
    )


# This list controls network access, not truth. Every extracted assertion still requires
# provenance and human review. Exact domains are used: no regional or SDIS wildcard.
TRUSTED_RESEARCH_SOURCES: tuple[TrustedResearchSource, ...] = (
    _source(
        "securite-civile.interieur.gouv.fr",
        "Sécurité civile",
        "authority",
        "national",
        "A+",
        OPERATIONAL_CLAIMS,
        "per_item_license_check",
        15,
    ),
    _source(
        "interieur.gouv.fr",
        "Ministère de l'Intérieur",
        "authority",
        "national",
        "A+",
        OPERATIONAL_CLAIMS,
        "per_item_license_check",
        15,
    ),
    _source(
        "mairie-die.fr",
        "Ville de Die",
        "authority",
        "local",
        "A+",
        (*OPERATIONAL_CLAIMS, "local_instruction"),
        "per_item_license_check",
        10,
    ),
    _source(
        "drome.gouv.fr",
        "Préfecture de la Drôme",
        "authority",
        "departmental",
        "A+",
        OPERATIONAL_CLAIMS,
        "per_item_license_check",
        10,
    ),
    _source(
        "pompiers26.com",
        "SDIS 26",
        "emergency_service",
        "departmental",
        "A+",
        (
            "operational_confirmation",
            "official_update",
            "fire_progression",
            "burned_area",
            "personnel_engaged",
            "ground_resources_engaged",
            "aircraft_engaged",
            "casualties_and_damage",
            "public_relief_and_donations",
            "media_asset",
        ),
        "per_item_license_check",
        10,
    ),
    _source(
        "firms.modaps.eosdis.nasa.gov",
        "NASA FIRMS",
        "satellite",
        "global",
        "A",
        SATELLITE_CLAIMS,
        "dataset_license_required",
        30,
    ),
    _source(
        "earthdata.nasa.gov",
        "NASA Earthdata",
        "satellite",
        "global",
        "A",
        SATELLITE_CLAIMS,
        "dataset_license_required",
        30,
    ),
    _source(
        "forest-fire.emergency.copernicus.eu",
        "Copernicus EFFIS",
        "satellite",
        "global",
        "A",
        (*SATELLITE_CLAIMS, "fire_danger"),
        "dataset_license_required",
        30,
    ),
    _source(
        "maps.effis.emergency.copernicus.eu",
        "Copernicus EFFIS Maps",
        "satellite",
        "global",
        "A",
        SATELLITE_CLAIMS,
        "dataset_license_required",
        30,
    ),
    _source(
        "data.effis.emergency.copernicus.eu",
        "Copernicus EFFIS Data",
        "satellite",
        "global",
        "A",
        SATELLITE_CLAIMS,
        "dataset_license_required",
        30,
    ),
    *(
        _source(
            domain,
            label,
            "satellite",
            "global",
            "A",
            ("satellite_imagery",),
            "dataset_license_required",
            60,
        )
        for domain, label in (
            ("dataspace.copernicus.eu", "Copernicus Data Space"),
            ("browser.dataspace.copernicus.eu", "Copernicus Browser"),
            ("catalogue.dataspace.copernicus.eu", "Copernicus Catalogue"),
            ("documentation.dataspace.copernicus.eu", "Copernicus Documentation"),
        )
    ),
    *(
        _source(
            domain,
            label,
            "weather",
            scope,
            "A",
            ("weather", "fire_danger"),
            publication,
            60,
        )
        for domain, label, scope, publication in _WEATHER_SOURCES
    ),
    _source(
        "bdiff.agriculture.gouv.fr",
        "BDIFF",
        "context",
        "national",
        "B",
        ("historical_context",),
        "dataset_license_required",
        1_440,
    ),
    _source(
        "georisques.gouv.fr",
        "Géorisques",
        "context",
        "national",
        "B",
        CONTEXT_CLAIMS,
        "dataset_license_required",
        1_440,
    ),
    _source(
        "data.gouv.fr",
        "data.gouv.fr",
        "context",
        "national",
        "B",
        CONTEXT_CLAIMS,
        "dataset_license_required",
        1_440,
    ),
    _source(
        "auvergne-rhone-alpes.developpement-durable.gouv.fr",
        "DREAL Auvergne-Rhône-Alpes",
        "context",
        "regional",
        "B",
        CONTEXT_CLAIMS,
        "per_item_license_check",
        1_440,
    ),
    _source(
        "atmo-france.org",
        "Atmo France",
        "air_quality",
        "national",
        "A",
        ("air_quality", "air_quality_alert", "smoke_effect"),
        "dataset_license_required",
        60,
    ),
    _source(
        "atmo-auvergnerhonealpes.fr",
        "Atmo Auvergne-Rhône-Alpes",
        "air_quality",
        "regional",
        "A",
        ("air_quality", "air_quality_alert", "smoke_effect"),
        "dataset_license_required",
        60,
    ),
    _source(
        "lannuaire.service-public.fr",
        "Annuaire Service-Public.fr",
        "directory",
        "national",
        "B",
        ("official_domain_identity",),
        "facts_with_attribution",
        10_080,
    ),
    *(
        _source(
            domain,
            label,
            "press",
            scope,
            "lead",
            PRESS_CLAIMS,
            "private_analysis_only",
            30,
        )
        for domain, label, scope in _PRESS_SOURCES
    ),
)

DEFAULT_RESEARCH_ALLOWED_DOMAINS = tuple(source.domain for source in TRUSTED_RESEARCH_SOURCES)


def research_source_policy_payload(
    allowed_domains: Iterable[str],
) -> dict[str, dict[str, object]]:
    requested = {domain.casefold().rstrip(".") for domain in allowed_domains}
    return {
        source.domain: {
            "source_name": source.label,
            "kind": source.kind,
            "scope": source.scope,
            "confidence_level": source.confidence_level,
            "claim_types": list(source.claim_types),
            "publication_policy": source.publication_policy,
            "minimum_refresh_minutes": source.minimum_refresh_minutes,
        }
        for source in TRUSTED_RESEARCH_SOURCES
        if source.domain in requested
    }


# The provider is discovery-only. Its pages can never be fetched as evidence or persisted
# as candidates because provider and source domains are enforced as disjoint sets.
DEFAULT_RESEARCH_SEARCH_TEMPLATES = {
    "html.duckduckgo.com": "https://html.duckduckgo.com/html/?q={query}",
}
