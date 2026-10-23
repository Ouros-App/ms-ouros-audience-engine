CREATE TABLE IF NOT EXISTS audience_snapshot_refs (
    audience_key TEXT PRIMARY KEY,
    snapshot_version BIGINT NOT NULL DEFAULT 0 CHECK (snapshot_version >= 0),
    s3_object_key TEXT,
    s3_manifest_key TEXT,
    bitmap_sha256 CHAR(64),
    rule_version TEXT NOT NULL,
    source_watermark TIMESTAMPTZ,
    evaluated_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    CHECK ((snapshot_version = 0 AND s3_object_key IS NULL
            AND s3_manifest_key IS NULL AND bitmap_sha256 IS NULL)
        OR (snapshot_version > 0 AND s3_object_key IS NOT NULL
            AND s3_manifest_key IS NOT NULL AND bitmap_sha256 IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS tip_audience_refs (
    tip_id BIGINT NOT NULL REFERENCES tips(id) ON DELETE CASCADE,
    audience_key TEXT NOT NULL REFERENCES audience_snapshot_refs(audience_key),
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tip_id, audience_key)
);

COMMENT ON TABLE audience_snapshot_refs IS
    'Current version pointers only; audience members live in versioned Redis bitmaps.';
COMMENT ON COLUMN audience_snapshot_refs.s3_object_key IS
    'Legacy S3-era column name; stores the versioned Redis bitmap key.';
COMMENT ON COLUMN audience_snapshot_refs.s3_manifest_key IS
    'Legacy S3-era column name; stores the Redis snapshot manifest key.';
COMMENT ON TABLE tip_audience_refs IS
    'Reviewed links between existing tips and configured audiences; contains no member identities.';
