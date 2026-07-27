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


class AgentConsentBasis(StrEnum):
    EXPLICIT_UPLOAD = "explicit_upload"
    SOURCE_LICENSE = "source_license"
    INSTITUTIONAL_MANDATE = "institutional_mandate"


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


class ReviewResolutionAction(StrEnum):
    ATTACH = "attach"
    CREATE = "create"
    REJECT = "reject"
