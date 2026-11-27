# Release notes

## Destructive-operation cutoff format

`RBAC_DESTRUCTIVE_SEEDING_ENABLED_UNTIL` and
`RBAC_DESTRUCTIVE_API_ENABLED_UNTIL` now require an ISO 8601 calendar date
(`YYYY-MM-DD`), optionally followed by a time separated by `T` or a space.
For example, use `2030-01-01T12:00:00Z`. Values without an offset use UTC.
Legacy non-ISO formats and other ISO forms, including ordinal and week dates,
are no longer accepted. Missing or invalid values fall back to the UTC epoch,
disabling the corresponding destructive operation. Update existing deployment
values before upgrading.
