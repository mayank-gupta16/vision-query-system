# SPDX-License-Identifier: Apache-2.0
"""Checksummed additive SQLite schema for private v0.2 perception metadata."""

from __future__ import annotations

import hashlib

SCHEMA_V2: tuple[str, ...] = (
    """CREATE TABLE observations (
        observation_id TEXT NOT NULL PRIMARY KEY CHECK (length(observation_id) = 68),
        source_id TEXT NOT NULL,
        frame_id TEXT NOT NULL,
        stream_index INTEGER NOT NULL,
        pts_value TEXT NOT NULL,
        pts_order INTEGER NOT NULL,
        pts_time_base_numerator TEXT NOT NULL,
        pts_time_base_denominator TEXT NOT NULL,
        category TEXT NOT NULL,
        confidence_millionths INTEGER NOT NULL,
        producer_name TEXT NOT NULL,
        producer_version TEXT NOT NULL,
        producer_configuration_sha256 TEXT NOT NULL
            CHECK (length(producer_configuration_sha256) = 64),
        schema_version INTEGER NOT NULL,
        identity_version INTEGER NOT NULL,
        record_json BLOB NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY (frame_id) REFERENCES frames(frame_id) ON DELETE CASCADE,
        FOREIGN KEY (source_id, stream_index)
            REFERENCES source_streams(source_id, stream_index) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE tracklets (
        tracklet_id TEXT NOT NULL PRIMARY KEY CHECK (length(tracklet_id) = 68),
        source_id TEXT NOT NULL,
        stream_index INTEGER NOT NULL,
        category TEXT NOT NULL,
        termination_reason TEXT NOT NULL,
        producer_name TEXT NOT NULL,
        producer_version TEXT NOT NULL,
        producer_configuration_sha256 TEXT NOT NULL
            CHECK (length(producer_configuration_sha256) = 64),
        start_pts_value TEXT NOT NULL,
        start_pts_order INTEGER NOT NULL,
        start_pts_time_base_numerator TEXT NOT NULL,
        start_pts_time_base_denominator TEXT NOT NULL,
        end_pts_value TEXT NOT NULL,
        end_pts_order INTEGER NOT NULL,
        end_pts_time_base_numerator TEXT NOT NULL,
        end_pts_time_base_denominator TEXT NOT NULL,
        point_count INTEGER NOT NULL CHECK (point_count > 0 AND point_count <= 64),
        schema_version INTEGER NOT NULL,
        identity_version INTEGER NOT NULL,
        record_json BLOB NOT NULL,
        record_sha256 TEXT NOT NULL CHECK (length(record_sha256) = 64),
        FOREIGN KEY (source_id, stream_index)
            REFERENCES source_streams(source_id, stream_index) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE tracklet_points (
        tracklet_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0 AND ordinal < 64),
        observation_id TEXT NOT NULL,
        frame_id TEXT NOT NULL,
        pts_value TEXT NOT NULL,
        pts_order INTEGER NOT NULL,
        PRIMARY KEY (tracklet_id, ordinal),
        UNIQUE (tracklet_id, observation_id),
        UNIQUE (tracklet_id, frame_id),
        FOREIGN KEY (tracklet_id) REFERENCES tracklets(tracklet_id) ON DELETE CASCADE,
        FOREIGN KEY (observation_id) REFERENCES observations(observation_id),
        FOREIGN KEY (frame_id) REFERENCES frames(frame_id)
    ) STRICT""",
    """CREATE TABLE perception_run_records (
        run_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_type TEXT NOT NULL CHECK (record_type IN ('observation', 'tracklet')),
        PRIMARY KEY (run_id, record_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE selected_evidence (
        run_id TEXT NOT NULL,
        tracklet_id TEXT NOT NULL,
        rank INTEGER NOT NULL CHECK (rank > 0 AND rank <= 8),
        observation_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        frame_id TEXT NOT NULL,
        stream_index INTEGER NOT NULL,
        selector_name TEXT NOT NULL,
        selector_version TEXT NOT NULL,
        selector_configuration_sha256 TEXT NOT NULL
            CHECK (length(selector_configuration_sha256) = 64),
        intent_json BLOB NOT NULL,
        intent_sha256 TEXT NOT NULL CHECK (length(intent_sha256) = 64),
        evidence_id TEXT,
        PRIMARY KEY (run_id, tracklet_id, rank),
        UNIQUE (run_id, tracklet_id, observation_id),
        UNIQUE (run_id, evidence_id),
        FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY (tracklet_id) REFERENCES tracklets(tracklet_id) ON DELETE CASCADE,
        FOREIGN KEY (observation_id) REFERENCES observations(observation_id),
        FOREIGN KEY (frame_id) REFERENCES frames(frame_id),
        FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id),
        FOREIGN KEY (source_id, stream_index)
            REFERENCES source_streams(source_id, stream_index) ON DELETE CASCADE
    ) STRICT""",
    """CREATE TABLE perception_deletion_closure (
        deletion_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        record_type TEXT NOT NULL CHECK (record_type IN ('observation', 'tracklet')),
        PRIMARY KEY (deletion_id, record_id),
        FOREIGN KEY (deletion_id) REFERENCES deletion_jobs(deletion_id) ON DELETE CASCADE
    ) STRICT""",
    """CREATE INDEX observations_source_time
        ON observations(source_id, stream_index, pts_order, observation_id)""",
    """CREATE INDEX observations_frame_category
        ON observations(frame_id, category, observation_id)""",
    """CREATE INDEX tracklets_source_start
        ON tracklets(source_id, stream_index, start_pts_order, tracklet_id)""",
    """CREATE INDEX tracklets_source_category_termination
        ON tracklets(source_id, stream_index, category, termination_reason, tracklet_id)""",
    """CREATE INDEX tracklet_points_observation
        ON tracklet_points(observation_id, tracklet_id, ordinal)""",
    """CREATE INDEX perception_run_records_record
        ON perception_run_records(record_id, run_id)""",
    """CREATE INDEX selected_evidence_tracklet
        ON selected_evidence(tracklet_id, run_id, rank)""",
    """CREATE INDEX selected_evidence_observation
        ON selected_evidence(observation_id, run_id, tracklet_id)""",
    """CREATE INDEX selected_evidence_evidence
        ON selected_evidence(evidence_id, run_id)""",
    """CREATE INDEX perception_deletion_closure_record
        ON perception_deletion_closure(record_id, deletion_id)""",
)

MIGRATION_V2_NAME = "v2_perception_metadata"
MIGRATION_V2_CHECKSUM = hashlib.sha256("\0".join(SCHEMA_V2).encode("utf-8")).hexdigest()

__all__ = ["MIGRATION_V2_CHECKSUM", "MIGRATION_V2_NAME", "SCHEMA_V2"]
