from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fire_viewer.core.research_sources import (
    DEFAULT_RESEARCH_ALLOWED_DOMAINS,
    DEFAULT_RESEARCH_SEARCH_TEMPLATES,
    SOURCE_REGISTRY_VERSION,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FV_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Fire-Viewer API"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "staging", "production"] = "development"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite:///./data/fire_viewer.db"
    database_pool_size: int = Field(default=2, ge=1, le=20)
    database_max_overflow: int = Field(default=3, ge=0, le=40)
    database_pool_recycle_seconds: int = Field(default=300, ge=30, le=3_600)
    database_statement_timeout_ms: int = Field(default=15_000, ge=1_000, le=120_000)
    database_schema_revision: str = Field(
        default="db7c2e4f9a10",
        pattern=r"^[0-9a-f]{12}$",
    )
    log_level: str = "INFO"
    max_body_bytes: int = Field(default=1_048_576, ge=16_384, le=16_777_216)
    # Spatial archives deliberately have an independent budget.  The general HTTP
    # body guard remains conservative for JSON and regular form endpoints.
    zone_upload_storage_dir: Path = Path("./data/zone_uploads")
    object_storage_backend: Literal["local", "vercel_blob"] = "local"
    object_storage_prefix: str = Field(default="firewarning", pattern=r"^[a-z0-9][a-z0-9/_-]*$")
    blob_read_write_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("FV_BLOB_READ_WRITE_TOKEN", "BLOB_READ_WRITE_TOKEN"),
    )
    blob_upload_grant_minutes: int = Field(default=60, ge=10, le=240)
    blob_client_token_minutes: int = Field(default=30, ge=5, le=60)
    zone_upload_max_bytes: int = Field(default=536_870_912, ge=1_048_576, le=1_073_741_824)
    zone_upload_max_unpacked_bytes: int = Field(
        default=2_147_483_648,
        ge=1_048_576,
        le=4_294_967_296,
    )
    zone_upload_max_files: int = Field(default=2_000, ge=3, le=100_000)
    zone_upload_max_manifest_bytes: int = Field(
        default=4_194_304,
        ge=1_024,
        le=67_108_864,
    )
    zone_upload_chunk_bytes: int = Field(default=1_048_576, ge=65_536, le=8_388_608)
    zone_upload_multipart_overhead_bytes: int = Field(default=65_536, ge=4_096, le=1_048_576)
    admin_asset_proxy_max_bytes: int = Field(
        default=104_857_600,
        ge=1_048_576,
        le=104_857_600,
    )
    sqlite_busy_timeout_ms: int = Field(default=10_000, ge=1_000, le=120_000)
    cors_origins: list[str] = Field(default_factory=list)
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])

    auth_mode: Literal["disabled", "jwt", "local_admin"] = "disabled"
    oidc_jwks_url: HttpUrl | None = None
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_roles_claim: str = "roles"
    oidc_algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    oidc_leeway_seconds: int = Field(default=30, ge=0, le=300)
    local_admin_username: str = "admin"
    # Format: scrypt$<base64 salt>$<base64 digest>. Generate it with the maintenance command.
    local_admin_password_hash: str | None = None
    local_admin_session_hours: int = Field(default=8, ge=1, le=24)
    local_admin_idle_minutes: int = Field(default=30, ge=5, le=240)
    local_admin_login_limit: int = Field(default=5, ge=1, le=20)

    matching_policy_id: str = "g1-default-v1"
    matching_create_below: float = Field(default=0.45, ge=0.0, le=1.0)
    matching_auto_attach_above: float = Field(default=0.80, ge=0.0, le=1.0)
    matching_min_margin: float = Field(default=0.15, ge=0.0, le=1.0)
    matching_max_candidate_distance_m: float = Field(default=20_000.0, ge=500.0, le=100_000.0)
    matching_max_incident_uncertainty_m: float = Field(default=10_000.0, ge=100.0, le=100_000.0)
    matching_max_candidates: int = Field(default=50, ge=2, le=500)
    matching_distance_scale_m: float = Field(default=250.0, ge=1.0, le=10_000.0)
    matching_active_time_decay_hours: float = Field(default=72.0, ge=1.0, le=8_760.0)
    matching_closed_time_decay_hours: float = Field(default=168.0, ge=1.0, le=17_520.0)

    idempotency_retention_hours: int = Field(default=24 * 30, ge=24, le=24 * 365)
    max_clock_skew_seconds: int = Field(default=300, ge=0, le=3_600)
    public_notice: str = (
        "Terrain daté; positions et périmètres peuvent être estimés; "
        "ce service ne remplace pas les secours."
    )
    public_report_rate_limit_per_day: int = Field(default=5, ge=1, le=25)
    public_report_hash_secret: str = "development-only-public-report-secret-change-me"  # noqa: S105
    public_contribution_rate_limit_per_day: int = Field(default=5, ge=1, le=25)
    public_contribution_max_image_bytes: int = Field(
        default=15_728_640, ge=1_048_576, le=16_777_216
    )
    corroboration_min_independent_proofs: int = Field(default=3, ge=3, le=20)
    model_generation_min_area_ha: float = Field(default=500.0, ge=1.0, le=1_000_000.0)
    raw_purge_delay_hours: int = Field(default=24, ge=1, le=24)
    unpublished_model_retention_days: int = Field(default=30, ge=1, le=365)
    agent_dispatch_enabled: bool = False
    agent_media_allowed_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    agent_runpod_transport: Literal["pod", "serverless"] = "pod"
    agent_runpod_pod_base_url: HttpUrl | None = None
    agent_runpod_pod_auth_token: SecretStr | None = Field(default=None, min_length=32)
    agent_runpod_endpoint_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{3,128}$")
    agent_runpod_api_key: SecretStr | None = None
    agent_runpod_base_url: HttpUrl = HttpUrl("https://api.runpod.ai")
    agent_expected_model_revisions: dict[str, str] = Field(default_factory=dict)
    agent_execution_timeout_ms: int = Field(default=900_000, ge=5_000, le=604_800_000)
    agent_job_ttl_ms: int = Field(default=3_600_000, ge=10_000, le=604_800_000)
    agent_poll_interval_seconds: int = Field(default=5, ge=2, le=300)
    public_contribution_agent_interval_seconds: int = Field(default=10_800, ge=900, le=86_400)
    agent_dispatch_lease_seconds: int = Field(default=90, ge=30, le=900)
    agent_dispatch_max_attempts: int = Field(default=3, ge=1, le=10)
    cron_secret: SecretStr | None = Field(
        default=None,
        min_length=32,
        validation_alias=AliasChoices("FV_CRON_SECRET", "CRON_SECRET"),
    )
    agent_source_package_max_files: int = Field(default=500, ge=1, le=5_000)
    agent_source_package_max_file_bytes: int = Field(
        default=536_870_912, ge=1_048_576, le=1_073_741_824
    )
    agent_source_package_max_total_bytes: int = Field(
        default=2_147_483_648, ge=1_048_576, le=4_294_967_296
    )
    agent_source_package_retention_days: int = Field(default=30, ge=1, le=365)
    agent_media_proxy_base_url: HttpUrl = HttpUrl("https://localhost")
    agent_media_signing_secret: SecretStr = SecretStr(
        "development-only-agent-media-signing-secret-change-me"
    )
    agent_research_enabled: bool = False
    agent_research_source_registry_version: str = Field(
        default=SOURCE_REGISTRY_VERSION, min_length=3, max_length=64
    )
    agent_research_allowed_domains: list[str] = Field(
        default_factory=lambda: list(DEFAULT_RESEARCH_ALLOWED_DOMAINS)
    )
    agent_research_search_templates: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_RESEARCH_SEARCH_TEMPLATES)
    )
    agent_research_max_fetch_bytes: int = Field(default=26_214_400, ge=65_536, le=104_857_600)
    agent_research_request_timeout_seconds: int = Field(default=20, ge=2, le=120)

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        if self.environment in {"staging", "production"} and self.auth_mode == "disabled":
            raise ValueError("Authentication cannot be disabled outside development/test")
        if self.environment in {"staging", "production"} and not self.database_url.startswith(
            ("postgresql://", "postgresql+psycopg://", "postgres://")
        ):
            raise ValueError("PostgreSQL is required outside development/test")
        if self.environment == "production" and self.object_storage_backend != "vercel_blob":
            raise ValueError("Vercel Blob private storage is required in production")
        if self.object_storage_backend == "vercel_blob" and not self.blob_read_write_token:
            raise ValueError("blob_read_write_token is required for Vercel Blob client uploads")
        if self.auth_mode == "jwt":
            missing = [
                name
                for name, value in (
                    ("oidc_jwks_url", self.oidc_jwks_url),
                    ("oidc_issuer", self.oidc_issuer),
                    ("oidc_audience", self.oidc_audience),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"Missing JWT settings: {', '.join(missing)}")
        if self.auth_mode == "local_admin" and not self.local_admin_password_hash:
            raise ValueError(
                "local_admin_password_hash is required when local_admin authentication is enabled"
            )
        if self.matching_create_below >= self.matching_auto_attach_above:
            raise ValueError("matching_create_below must be lower than auto-attach threshold")
        if self.zone_upload_max_unpacked_bytes < self.zone_upload_max_bytes:
            raise ValueError("zone_upload_max_unpacked_bytes must cover the archive size limit")
        if self.environment == "production" and "*" in self.trusted_hosts:
            raise ValueError("Wildcard trusted host is forbidden in production")
        if "*" in self.agent_media_allowed_hosts:
            raise ValueError("Wildcard agent media host is forbidden")
        normalized_research_domains = [
            domain.strip().casefold().rstrip(".") for domain in self.agent_research_allowed_domains
        ]
        if any(
            not domain or domain == "*" or "/" in domain for domain in normalized_research_domains
        ):
            raise ValueError("Research domains must be explicit host names")
        if len(normalized_research_domains) != len(set(normalized_research_domains)):
            raise ValueError("Research domains must be unique")
        unknown_research_domains = set(normalized_research_domains) - set(
            DEFAULT_RESEARCH_ALLOWED_DOMAINS
        )
        if unknown_research_domains:
            raise ValueError(
                "Research domains require an executable source policy: "
                + ", ".join(sorted(unknown_research_domains))
            )
        if self.agent_research_source_registry_version != SOURCE_REGISTRY_VERSION:
            raise ValueError("Research source registry version must match the executable registry")
        normalized_search_providers = {
            domain.strip().casefold().rstrip(".") for domain in self.agent_research_search_templates
        }
        if normalized_search_providers & set(normalized_research_domains):
            raise ValueError("Research providers must be separate from source domains")
        for domain, template in self.agent_research_search_templates.items():
            provider = domain.strip().casefold().rstrip(".")
            parts = urlsplit(template)
            if (
                parts.scheme.casefold() != "https"
                or (parts.hostname or "").casefold().rstrip(".") != provider
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
                or parts.port not in {None, 443}
                or template.count("{query}") != 1
            ):
                raise ValueError("Research search templates must be strict HTTPS templates")
        if self.agent_source_package_max_total_bytes < self.agent_source_package_max_file_bytes:
            raise ValueError("Source package total size must cover one maximum-size file")
        if self.environment in {"staging", "production"}:
            if self.agent_media_proxy_base_url.scheme != "https":
                raise ValueError("agent_media_proxy_base_url must use HTTPS")
            if len(self.agent_media_signing_secret.get_secret_value()) < 32:
                raise ValueError("agent_media_signing_secret must contain at least 32 characters")
        if self.agent_research_enabled and not normalized_research_domains:
            raise ValueError("At least one research domain is required when research is enabled")
        if self.agent_research_enabled and not normalized_search_providers:
            raise ValueError("At least one research search provider is required when enabled")
        if self.agent_dispatch_enabled:
            if self.environment in {"staging", "production"} and not self.cron_secret:
                raise ValueError("cron_secret is required for hosted agent orchestration")
            if self.agent_runpod_transport == "pod" and (
                not self.agent_runpod_pod_base_url or not self.agent_runpod_pod_auth_token
            ):
                raise ValueError(
                    "agent_runpod_pod_base_url and agent_runpod_pod_auth_token are required "
                    "for direct pod dispatch"
                )
            if (
                self.agent_runpod_transport == "pod"
                and self.agent_runpod_pod_base_url is not None
                and self.agent_runpod_pod_base_url.scheme != "https"
            ):
                raise ValueError("agent_runpod_pod_base_url must use HTTPS")
            if self.agent_runpod_transport == "serverless" and (
                not self.agent_runpod_endpoint_id or not self.agent_runpod_api_key
            ):
                raise ValueError(
                    "agent_runpod_endpoint_id and agent_runpod_api_key are required "
                    "for serverless dispatch"
                )
        if self.agent_execution_timeout_ms > self.agent_job_ttl_ms:
            raise ValueError("agent_job_ttl_ms must cover agent_execution_timeout_ms")
        if (
            self.environment in {"staging", "production"}
            and len(self.public_report_hash_secret) < 32
        ):
            raise ValueError(
                "public_report_hash_secret must be at least 32 characters outside development"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
