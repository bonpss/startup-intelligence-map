## Deferred from: code review of story 1-1-create-the-quality-review-log-table-via-migration (2026-08-10)

- Index on `(review_type, subject)` for `quality_review_log` [migrations/001_create_quality_review_log.sql] — pre-existing scale doesn't warrant it yet (low hundreds of rows expected); revisit if the table grows large enough for sequential scans on FR-3's lookup query to matter.
