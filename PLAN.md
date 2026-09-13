# S3 fail-fast validation without workflow regressions

## Implementation status

- Sections 1–8: S3 preflight improvements are **implemented and tested** (see "Implemented summary" below).
- Section 9: Host MIME detection and guided correction is **implemented and tested**.

## Implemented summary

The S3 preflight work landed as follows:

- `check_s3_destination()` (import) now reports a failed listing instead of
  ignoring it, and probes write/delete access with a unique, prefix-scoped
  object that is removed afterwards. Preflight subprocesses have bounded
  timeouts.
- `check_s3_access()` is a new non-mutating preflight for existing podcasts:
  it fails fast on missing `s4cmd` or credentials without requiring an empty
  prefix or delete permission.
- The importer runs its S3 check before `_clear_previous_import()`, so a bad
  destination no longer removes a previous local `feed.xml` first.
- The Hosting menu validates `check_s3_access()` before saving S3 settings.
- `Publisher.deploy()` and automatic publication run the access preflight only
  when there is real remote work and (for `deploy`) not in a dry run.
- No new public URL probe was added (Section 5) and no synthetic multipart
  upload (Section 6); post-upload verification remains authoritative and the
  actual upload remains the definitive write test.
- Regression tests cover listing failure, unique probe names, credential
  presence, the importer reordering, the hosting-menu gate, and preflight
  before real uploads (including dry-run and no-op behavior).

## Goal and review basis

Catch S3 connection, authentication, and relevant permission failures before lengthy import processing or real asset uploads, while preserving existing publication, recovery, and deployment controls.

This implementation plan is based on review of the current import, publishing, hosting-settings, probe, and cleanup paths. Preflight improves early detection; it cannot guarantee later requests will succeed if connectivity or permissions change. Compatibility must be verified during implementation rather than assumed.

## Current behavior

- Fresh interactive imports check S3 during hosting setup.
- `download_import()` checks again before media staging; archive imports share this path.
- The check lists the destination, uploads a small probe, and deletes it.
- Failed listing commands are currently ignored.
- Regular publishing and standalone deployment discover S3 failures through actual uploads.
- Hosting edits can be saved before remote access is validated.
- Public URL verification happens after asset uploads.
- The current probe uses a fixed object name.

## Compatibility requirements

- Existing podcasts can upload into populated prefixes.
- Local hosting performs no S3 operations.
- `deploy --dry-run` performs no remote requests or local deletion.
- `--no-verify` continues to suppress public URL verification.
- `enabled=False` prevents automatic remote operations during regeneration.
- Explicit deployment remains available regardless of `enabled`.
- Imports with automatic deployment disabled still upload once after import.
- Missing local media already stored remotely remains skippable.
- A deployment with no files to upload does not introduce remote work.
- Media uploads precede the optional feed mirror.
- The canonical local feed is retained for both `keep_local_media` settings.
- Automatic publication failures preserve dirty state and remain recoverable through `publish-due`.
- S3 network operations run outside database and output locks.
- Credentials remain outside Termicast's database and checkpoints.

## 1. Separate import validation from reusable access checks

**Primary file:** `termicast/s3deploy.py`

Keep `check_s3_destination()` as the import-specific entry point. Separate responsibilities internally:

1. Check that the import destination is empty.
2. Check relevant remote access.
3. Optionally check public delivery where compatible.

Existing-show publishing and deployment must use the reusable access check without requiring an empty destination. Keep existing upload settings, endpoint handling, executable resolution, and permission guidance.

## 2. Handle listing failures accurately

- Inspect the listing command's exit status before interpreting output.
- Report connection, authentication, and permission failures with the original diagnostic.
- Never treat a failed listing as evidence that an import destination is empty.
- Verify how the supported `s4cmd` version represents an empty prefix. Accommodate a documented no-match result explicitly if necessary.
- Remove the unconditional "listable but not writable" claim from probe failures.

For existing-show deployment, require only operations that its upload path actually needs; do not introduce an unrelated bucket-wide listing requirement.

## 3. Make probe objects collision-resistant and cleanup reliable

- Replace the fixed `.termicast-write-check.txt` name with a unique per-attempt object key within the configured show prefix.
- Delete only the exact probe object created by that invocation.
- Attempt cleanup after subsequent verification failures.
- Preserve the primary failure and append cleanup diagnostics instead of masking it.
- Report the precise remote key if cleanup fails.
- Handle interrupted or timed-out uploads as potentially having created a probe.
- Give preflight subprocesses bounded timeouts and actionable errors.

Probe objects must never enter managed asset lists, feeds, backups, or local-media deletion logic.

### Permission compatibility

Imports already require probe cleanup permission, so retain that requirement there.

Before adding temporary writes to regular publishing, verify the permissions actually required by `s4cmd` uploads. Do not make delete permission a new mandatory condition for existing upload-only credentials.

For existing deployments where safe probe cleanup cannot be established, use non-mutating access checks and retain the actual upload as the definitive write test. State clearly which capabilities were verified. Do not discover delete permission by leaving disposable objects behind on every deployment.

## 4. Integrate checks at workflow boundaries

### Imports

**Files:** `termicast/cli.py`, `termicast/importer.py`, `termicast/archive.py`

- Keep the early hosting-setup check.
- Keep the importer's authoritative check using final settings, including resumed imports and hosting edits.
- Move that check ahead of `_clear_previous_import()` so a rejected S3 destination does not first remove existing local import state.
- Preserve checkpoint progress and retry behavior.
- Preserve archive imports' shared ingestion path.

### Hosting configuration

**Files:** `termicast/prompts.py`, `termicast/cli.py`

- Validate proposed S3 settings while staged, before accepting or saving them.
- Cover both the dedicated Hosting menu and hosting edits through the show form.
- On failure, retain prior settings and allow correction.
- Use the reusable existing-destination check for existing podcasts.
- Keep import-only empty-prefix validation at the import boundary.
- Avoid placing network calls inside `Database.save_show()`.
- Coordinate form and import-wizard integration to avoid duplicate checks at the same boundary.

### Publishing and standalone deployment

**File:** `termicast/publisher.py`

- Preflight once per operation, before asset generation where practical and before real uploads.
- Determine whether actual remote work is needed before probing, accounting for assets that will be generated and an enabled feed mirror.
- Reuse the operation's successful check across media and optional feed-mirror batches.
- Avoid global or persistent success caches; later operations must recheck.
- Keep automatic-publication failures within the existing dirty-state/recovery path. If the check moves before the current dirty-state update, preserve recoverability explicitly.
- Preserve missing-feed checks and existing upload ordering.
- Retain the per-show operation lock during deployment checks while keeping network work outside database and output locks.

## 5. Improve early public-delivery checks conservatively

**Files:** `termicast/s3deploy.py`, `termicast/hosting.py`

A successful S3 API request does not establish that the public asset URL works.

- Preserve post-upload verification as the authoritative check.
- Honor `--no-verify` for all public checks.
- Prefer checking a known existing managed asset for existing shows when available. Do not block an upload intended to repair a missing or stale public asset solely because its previous public check fails.
- Use temporary public probes only where their path and format meaningfully represent the configured delivery setup.
- Do not make a root-level text probe a mandatory gate for deployments that legitimately serve only media paths.
- Where a probe is used, verify its identity through a small bounded response rather than accepting any HTTP 200 page.
- Use bounded retries for transient public-delivery failures.
- Distinguish API access failures from public URL, MIME, and CDN failures.

If a representative public probe cannot be used without new compatibility requirements, report that coverage accurately and retain existing post-upload checks.

## 6. Keep multipart and path-specific coverage explicit

- A small probe establishes access only for its key and request type.
- Do not add a large synthetic multipart upload to every operation.
- Where useful, select representative paths from planned uploads without multiplying probes per file.
- Preserve actionable errors for multipart and object-specific failures.
- Describe successful checks as specific capabilities verified rather than guaranteeing all uploads will succeed.

## 7. Regression verification

Add targeted tests covering:

1. Listing failure stops import before media staging or previous-import cleanup.
2. Supported empty-prefix responses are accepted; nonempty prefixes are rejected for imports.
3. Existing-show preflight accepts populated prefixes.
4. Probe names are unique and cleanup never targets existing objects.
5. Upload, verification, cleanup, and timeout failures preserve useful diagnostics.
6. Failed hosting validation leaves prior settings intact.
7. Final edited settings and resumed imports are checked before ingestion.
8. Automatic publishing and standalone deployment check before real uploads.
9. Media and feed mirroring share one operation-level preflight.
10. Local hosting, dry runs, disabled automatic deployment, and no-op deploys introduce no remote requests.
11. `--no-verify` suppresses public checks.
12. Existing upload-only credentials are not rejected solely by a new delete requirement, and repeated checks do not leave probes behind.
13. Public checks do not reject valid media-only delivery configurations based on a root text probe or prevent repair uploads for missing/stale assets.
14. Preflight failure preserves local media, canonical feed bytes, and automatic-publication retry state.
15. Existing import upload-once, mirror ordering, and media-retention tests continue passing.

Use mocked subprocess and HTTP boundaries; automated tests require no live S3 credentials. Extend the relevant existing suites in `tests/test_s3deploy.py`, `tests/test_cli.py`, `tests/test_importer.py`, `tests/test_publisher.py`, `tests/test_prompts.py`, and hosting/archive coverage as appropriate.

Establish a fresh baseline before implementation rather than relying on the previous plan's recorded test count. Run focused tests during implementation, followed by the repository suite:

```bash
.venv/bin/python -m pytest -q
```

## 8. Documentation and completion criteria

Update the S3 sections of `README.md` and `USER_GUIDE.md` to explain:

- When preflight runs and which capabilities it verifies.
- Differences between import and existing-show validation.
- Public verification controls.
- Probe cleanup and retry diagnostics.
- Remaining multipart, path-specific, and time-of-check limitations.

Complete the work when the regression suite passes and review confirms preserved lock ordering, publication recovery, credential handling, upload ordering, and deployment controls. Any proposed check that introduces new permission requirements or rejects a currently valid delivery setup must be revised before implementation is accepted.

## 9. Host MIME detection and guided correction

**Status: implemented and tested.** This complements the planned S3 preflight work by addressing incorrect HTTP Content-Type headers from a locally managed web server.

### Reported behavior and correction

The public server returned `text/xml; charset=utf-8` for `feed.xml`, `application/json` for chapter files, and `application/octet-stream` for PNG artwork (the site config has no `include mime.types`, so unlisted extensions fall back to octet-stream). The expected mappings are:

- `feed.xml`: `application/rss+xml`.
- Chapter JSON: `application/json+chapters`.
- PNG artwork: `image/png` (plus `image/webp`); `image/jpeg` for JPEG.

Keep these diagnostics rather than suppressing them or relaxing the expected MIME types. Offer a correction within Termicast instead of only directing the user to a snippet. Import additionally normalizes artwork to a flattened, sized RGB JPEG (quality 90, optimized) so new imports carry smaller JPEG files; `check_assets` now derives the expected artwork type from the file extension.

### Implemented workflow

- **Open podcast → Hosting → Correct host MIME types** detects installed Nginx or Apache control tools, including common system executable directories.
- Interactive Hosting checks and failed Hosting-menu deployments offer this workflow when MIME mismatches are reported.
- The user selects the server and the site configuration serving the displayed podcast URL.
- Termicast shows the MIME mapping block in a panel and applies it automatically: it backs up the file, inserts the block into the server/Directory block, validates the syntax, and asks before reloading.
- If the user declines, or no server block can be found to patch, Termicast prints the exact block in a panel to paste in with `nano`.
- Changed configuration is syntax-checked. Validation failure restores the selected file; restoration failures identify the backup and destination.
- Reload is explicitly offered after successful validation. Reload checks syntax again and uses Nginx reload or Apache graceful reload.
- Public hosting checks run after reload; any remaining problems are reported rather than declaring the MIME issue fixed prematurely.

### Compatibility and operational boundaries

- Detection establishes that control tools are installed, not that the selected server serves the public URL. The user confirms the active configuration.
- The patch inserts a clearly marked, idempotent block (searched for before re-adding) into the site's server/Directory block. Chapter JSON mappings should be scoped to `chapters/` when ordinary JSON is served elsewhere.
- Only the selected configuration file is backed up and restored. The backup has owner-only permissions and is outside directories that could load it as active configuration. Temporary backups need copying elsewhere for long-term retention.
- The current account needs file-editing and server-control permissions. Validation and reload run as root (reading Let's Encrypt TLS keys and signalling the master), so the workflow uses `sudo` when not already root.
- Validation and reload use the selected tool's default configuration. Custom instances and container-managed servers require their deployment tooling.
- S3/CDN media may require object metadata or CDN header changes. A local-server edit does not establish a fix for those URLs; the canonical web-hosted feed can still use this workflow.
- Command-line `doctor` remains read-only. No automatic edits or reloads are introduced into unattended publishing or deployment.
- No live server configuration was changed during implementation verification.

### Files and verification

- `termicast/serverfix.py`: server detection, command execution, configuration backup/edit/validation/restore, and reload.
- `termicast/prompts.py`: guided correction action and interactive offers after MIME failures.
- `termicast/hosting.py`: diagnostics link to the correction workflow.
- `tests/test_serverfix.py`: detection, backup permissions, successful edits, rollback, baseline syntax failure, reload ordering, interactive offers, and absent-server behavior.
- `README.md` and `USER_GUIDE.md`: workflow, scope, permissions, and backup documentation.

Verification completed after implementation:

```bash
.venv/bin/python -m pytest -q
# 258 passed
```

Tests use mocked server commands; this result verifies application behavior, not the live site's headers. Successful public verification after the user applies and reloads the site correction is the final confirmation for the reported URLs.
