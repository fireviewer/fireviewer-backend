"""SQLite triggers that form part of the persistent Fire Viewer contract."""

SQLITE_CRITICAL_TRIGGERS = frozenset(
    {
        "audit_event_no_update",
        "audit_event_no_delete",
        "incident_series_rtree_insert",
        "incident_series_rtree_update",
        "incident_series_rtree_delete",
        "spatial_zone_zone_id_immutable",
        "spatial_zone_revision_no_update",
        "spatial_zone_revision_no_delete",
        "spatial_package_identity_immutable",
        "spatial_package_initial_state_draft",
        "spatial_package_revision_link_once",
        "spatial_package_verification_immutable",
        "spatial_package_verification_requires_transition",
        "spatial_package_state_transition",
        "spatial_package_no_withdraw_while_active",
        "spatial_package_no_delete",
        "spatial_package_file_no_update",
        "spatial_package_file_no_delete",
        "zone_publication_insert_valid",
        "zone_publication_identity_immutable",
        "zone_publication_no_delete",
        "zone_publication_state_transition",
        "zone_publication_reason_actor_requires_transition",
        "zone_publication_event_transition_valid",
        "zone_publication_state_update_requires_event",
        "zone_publication_event_no_update",
        "zone_publication_event_no_delete",
        "model_asset_zone_revision_immutable",
        "model_asset_sha256_immutable",
        "model_asset_validate_legacy_link_insert",
        "model_asset_validate_legacy_link_update",
        "model_asset_legacy_provenance_immutable",
        "model_asset_validate_package_file_insert",
        "model_asset_validate_package_file_update",
        "manifest_revision_validate_episode_insert",
        "manifest_revision_validate_episode_update",
        "manifest_revision_validate_spatial_link_insert",
        "manifest_revision_validate_spatial_link_update",
        "manifest_revision_validate_package_insert",
        "manifest_revision_validate_package_update",
        "zone_archive_snapshot_requires_closed_episode",
        "zone_archive_snapshot_validate_source_insert",
        "zone_archive_snapshot_no_update",
        "zone_archive_snapshot_no_delete",
        "episode_incident_immutable",
        "observation_validate_pairs_insert",
        "observation_validate_pairs_update",
        "observation_validate_episode_ownership_insert",
        "observation_validate_episode_ownership_update",
    }
)


# The recovery path compares these fingerprints with sqlite_master so a trigger
# cannot be silently retained under its expected name with a weakened body.
# Values are SHA-256 digests of the normalized CREATE TRIGGER statements.
SQLITE_CRITICAL_TRIGGER_HASHES: dict[str, str] = {
    "spatial_package_identity_immutable": (
        "6542bdcfe92180afe6a4c2f26f8d3c9b9ccfc3880e93ae02add3890279e2b886"
    ),
    "spatial_package_initial_state_draft": (
        "63dbf0f6a78f41ec02795841f334004f872bc1dbe1d68081da2f6e2d7cb804a3"
    ),
    "spatial_package_revision_link_once": (
        "dda85389bc51cab22661e9f92f403c1c6b448ddce07d5b176664f27e0cd20843"
    ),
    "spatial_package_verification_immutable": (
        "f35b0584131133c4b23ecf18c641a176f4d2ea0a667a8b7ae3d1d6707519d3d4"
    ),
    "spatial_package_verification_requires_transition": (
        "f69c2cb7cba29ebdb23d6f76cddf1e34b8975096b601528c3ee008b4b6c55ea1"
    ),
    "spatial_package_state_transition": (
        "b9d7d9299c4e4897a2efc833c4a55be12f8f7de9526bc6a630cb31d30fd2a7d0"
    ),
    "spatial_package_no_withdraw_while_active": (
        "e32457d39885b954d470255f4192149a9f2a9127f596274fdb768361c1ca885d"
    ),
    "spatial_package_no_delete": (
        "4b08068fed8b0a5c442917da9c953ee014ebd0c5a67b4356047b142afea3d903"
    ),
    "zone_publication_insert_valid": (
        "58d537cdfb71d3bc83dc4c052d50c9ae4f176a014572d98f04a42c5a14dfb368"
    ),
    "zone_publication_identity_immutable": (
        "b9962730e971711fe282cf14ce02b710618ad1975c516735528dfeb58d0b2bf4"
    ),
    "zone_publication_no_delete": (
        "4f389c35109d6c9fdb12b1751867019270dccaf5d4348e0a2568c6fcdf51943d"
    ),
    "zone_publication_state_transition": (
        "58097918fdd4700653552c0d4549994ebd470b25905a27ec9f1f5a9111471651"
    ),
    "zone_publication_reason_actor_requires_transition": (
        "08bb0d642b3a6d733914563a76b66f4a50049c2f3210d14580077b7c81db2bc0"
    ),
    "zone_publication_event_transition_valid": (
        "950099e58a356c265b47f48265ed70d484a28da7946bcffd270ce36c848ea28e"
    ),
    "zone_publication_state_update_requires_event": (
        "3b3269e6d1396db645d44378964deb6213d3c1a9129a6bca56eb389737e9d446"
    ),
}
