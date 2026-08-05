from enum import StrEnum


class SourceType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    SENSOR = "sensor"
    OPERATOR = "operator"
    INSTITUTIONAL = "institutional"


class SourceTrust(StrEnum):
    UNVERIFIED = "unverified"
    PARTNER = "partner"
    INSTITUTIONAL = "institutional"
    OPERATOR = "operator"


class IncidentStatus(StrEnum):
    CANDIDATE = "CANDIDATE"
    UNDER_REVIEW = "UNDER_REVIEW"
    ACTIVE_CONFIRMED = "ACTIVE_CONFIRMED"
    MONITORING = "MONITORING"
    EXTINGUISHED = "EXTINGUISHED"
    CLOSED = "CLOSED"
    SUSPENDED = "SUSPENDED"
    REJECTED = "REJECTED"


class PublicVisibility(StrEnum):
    PUBLIC = "PUBLIC"
    LIMITED = "LIMITED"
    SUSPENDED = "SUSPENDED"
    TOMBSTONED = "TOMBSTONED"


class MatchDecision(StrEnum):
    CREATE = "create"
    ATTACH = "attach"
    REVIEW = "review"


class VerificationState(StrEnum):
    UNVERIFIED = "UNVERIFIED"
    PENDING_REVIEW = "PENDING_REVIEW"
    CORROBORATED = "CORROBORATED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class EvidenceSpatialMode(StrEnum):
    WITHHELD = "WITHHELD"
    GENERALIZED = "GENERALIZED"
    EXACT = "EXACT"


class AssetState(StrEnum):
    GENERATED = "GENERATED"
    VALIDATED = "VALIDATED"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    QUARANTINED = "QUARANTINED"
    DELETED_TOMBSTONE = "DELETED_TOMBSTONE"


class SpatialPackageState(StrEnum):
    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"
    PREVIEWABLE = "PREVIEWABLE"
    PUBLISHED = "PUBLISHED"
    WITHDRAWN = "WITHDRAWN"
    REVOKED = "REVOKED"
    ARCHIVED = "ARCHIVED"


class ZonePublicationState(StrEnum):
    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"
    PREVIEWABLE = "PREVIEWABLE"
    PUBLISHED = "PUBLISHED"
    WITHDRAWN = "WITHDRAWN"
    REVOKED = "REVOKED"
    ARCHIVED = "ARCHIVED"


class SpatialPackageFileKind(StrEnum):
    COG = "COG"
    JPEG = "JPEG"
    PNG = "PNG"
    GLB = "GLB"
    FWTILE = "FWTILE"
    FWTERRAIN = "FWTERRAIN"
    JSON = "JSON"
    OPENUSD = "OPENUSD"
    AUXILIARY = "AUXILIARY"


class ZoneUploadState(StrEnum):
    RECEIVED = "RECEIVED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


class ZoneInformationState(StrEnum):
    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    PUBLISHED = "PUBLISHED"
    HIDDEN = "HIDDEN"
    REJECTED = "REJECTED"


class ZoneVisibility(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    HIDDEN = "HIDDEN"
    ARCHIVED = "ARCHIVED"


class ZoneContributionState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AssetLod(StrEnum):
    MOBILE = "mobile"
    DESKTOP = "desktop"
    CLOSE = "close"
    LOCAL = "local"
    EXTENDED = "extended"


class JobKind(StrEnum):
    TERRAIN_BAKE = "TERRAIN_BAKE"
    ASSET_PUBLICATION = "ASSET_PUBLICATION"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    UPLOADING = "UPLOADING"
    VERIFYING = "VERIFYING"
    PUBLISHING = "PUBLISHING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    QUARANTINED = "QUARANTINED"
    CANCELLED = "CANCELLED"


class AgentBatchType(StrEnum):
    USER_MEDIA = "user_media"
    EXTERNAL_MEDIA = "external_media"
    SATELLITE_MEDIA = "satellite_media"


class AgentBatchPriority(StrEnum):
    USER_DEADLINE = "user_deadline"
    SCHEDULED_COMBINED = "scheduled_combined"
    SCHEDULED = "scheduled"


class AgentBatchState(StrEnum):
    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class AgentMediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    ARTICLE = "article"
    SATELLITE_IMAGE = "satellite_image"
    SATELLITE_DATA = "satellite_data"


class AgentConsentBasis(StrEnum):
    EXPLICIT_UPLOAD = "explicit_upload"
    SOURCE_LICENSE = "source_license"
    INSTITUTIONAL_MANDATE = "institutional_mandate"
    PUBLIC_SOURCE_ANALYSIS = "public_source_analysis"


class AgentConsentState(StrEnum):
    GRANTED = "GRANTED"
    WITHDRAWN = "WITHDRAWN"
    EXPIRED = "EXPIRED"


class AgentDispatchState(StrEnum):
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    AWAITING_REMOTE = "AWAITING_REMOTE"
    POLL_WAIT = "POLL_WAIT"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class AgentDeadLetterState(StrEnum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REPLAYED = "REPLAYED"


class AgentModelRunState(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentReviewState(StrEnum):
    PENDING = "PENDING"
    IN_REVIEW = "IN_REVIEW"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"


class AgentAnalysisState(StrEnum):
    COLLECTING = "COLLECTING"
    PROCESSING = "PROCESSING"
    REVIEW_PENDING = "REVIEW_PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class AgentValidationCampaignDayState(StrEnum):
    LOCKED = "locked"
    READY = "ready"
    RUNNING = "running"
    REVIEW = "review"
    PUBLISHED = "published"
    FAILED = "failed"


class AgentSourcePackageState(StrEnum):
    OPEN = "OPEN"
    FINALIZING = "FINALIZING"
    CONVERTED = "CONVERTED"
    FAILED = "FAILED"
    PURGED = "PURGED"


class AgentSourcePackageKind(StrEnum):
    USER_SOURCES = "USER_SOURCES"
    ADMIN_SOURCES = "ADMIN_SOURCES"
    ADMIN_SATELLITE = "ADMIN_SATELLITE"


class AgentSourceResearchState(StrEnum):
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    DEAD_LETTER = "DEAD_LETTER"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class AgentSourceCandidateState(StrEnum):
    DISCOVERED = "DISCOVERED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class AgentProposalReviewState(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"


class AgentReportReviewState(StrEnum):
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"


class IncidentMarkerReviewState(StrEnum):
    PENDING = "PENDING"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


class ActiveFireZoneReviewState(StrEnum):
    DRAFT = "DRAFT"
    READY_FOR_PUBLICATION = "READY_FOR_PUBLICATION"
    REJECTED = "REJECTED"


class ActorType(StrEnum):
    PUBLIC_SOURCE = "public_source"
    OPERATOR = "operator"
    SERVICE = "service"
    SYSTEM = "system"


class PublicReportCategory(StrEnum):
    INFORMATION_OBSOLETE = "information_obsolete"
    LOCATION = "location"
    SOURCE = "source"
    PRIVACY = "privacy"
    ACCESSIBILITY = "accessibility"


class PublicReportState(StrEnum):
    PENDING = "PENDING"
    CORRECTED = "CORRECTED"
    REJECTED = "REJECTED"


class PublicContributionKind(StrEnum):
    NEW_FIRE = "new_fire"
    INCIDENT_EVIDENCE = "incident_evidence"


class PublicContributionState(StrEnum):
    OPEN = "OPEN"
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"


class ReviewResolutionAction(StrEnum):
    ATTACH = "attach"
    CREATE = "create"
    REJECT = "reject"


class IncidentCandidateState(StrEnum):
    PRIVATE_MATCHING = "PRIVATE_MATCHING"
    CONFIRMED = "CONFIRMED"
    MERGED = "MERGED"
    REJECTED = "REJECTED"


class EventCandidateState(StrEnum):
    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    ANALYZING = "ANALYZING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    ABSTAINED = "ABSTAINED"
    FAILED = "FAILED"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"


class EventAnalysisJobState(StrEnum):
    QUEUED = "QUEUED"
    SUBMITTING = "SUBMITTING"
    AWAITING_REMOTE = "AWAITING_REMOTE"
    COMPLETED = "COMPLETED"
    ABSTAINED = "ABSTAINED"
    FAILED = "FAILED"


class FireActivityEventState(StrEnum):
    DRAFT = "DRAFT"
    ANALYST_VALIDATED = "ANALYST_VALIDATED"
    EDITOR_PUBLISHED = "EDITOR_PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    RETRACTED = "RETRACTED"


class ViewpointOrigin(StrEnum):
    USER_PLACED = "USER_PLACED"
    DEVICE_GPS = "DEVICE_GPS"
    NAMED_PLACE = "NAMED_PLACE"
    OFFICIAL_SOURCE = "OFFICIAL_SOURCE"


class EvidenceAssetState(StrEnum):
    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"
    QUARANTINED = "QUARANTINED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    PURGED = "PURGED"


class MalwareScanState(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    FAILED = "FAILED"


class LocalizationAttemptState(StrEnum):
    PROPOSED = "PROPOSED"
    SHADOW = "SHADOW"
    SECTOR = "SECTOR"
    ABSTAINED = "ABSTAINED"
    FAILED = "FAILED"
    REVIEWED = "REVIEWED"


class ExternalArtifactStatus(StrEnum):
    PROVISIONAL = "PROVISIONAL"
    VALIDATED = "VALIDATED"
    CORRECTED = "CORRECTED"
    RETRACTED = "RETRACTED"


class ExternalSemanticRole(StrEnum):
    RAW_EARTH_OBSERVATION = "raw_earth_observation"
    SENSOR_DETECTION = "sensor_detection"
    INTERPRETED_OBSERVATION = "interpreted_observation"
    OFFICIAL_INCIDENT_STATEMENT = "official_incident_statement"
    WEATHER_OBSERVATION = "weather_observation"
    WEATHER_FORECAST = "weather_forecast"
    GEOSPATIAL_REFERENCE = "geospatial_reference"
    HISTORICAL_REGISTRY = "historical_registry"
    SIMULATION = "simulation"


class ExternalLineageRelation(StrEnum):
    DERIVED_FROM = "derived_from"
    SAME_ACQUISITION_AS = "same_acquisition_as"
    SUPERSEDES = "supersedes"
    RETRACTS = "retracts"
    MIRRORS = "mirrors"
    CONFLICTS_WITH = "conflicts_with"
    USES_RESTRICTED_ASSET = "uses_restricted_asset"


class EventRelationKind(StrEnum):
    IDENTITY = "identity"
    SUCCESSION = "succession"
    CONTRADICTION = "contradiction"
    MERGE = "merge"
    SPLIT = "split"


class ProgressionDeltaKind(StrEnum):
    APPEARANCE = "appearance"
    EXTENSION = "extension"
    MOVEMENT = "movement"
    CONTRACTION = "contraction"
    REACTIVATION = "reactivation"
    EXTINCTION = "extinction"
