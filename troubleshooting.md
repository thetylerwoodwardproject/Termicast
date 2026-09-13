# S3 Deployment Troubleshooting — Linode `AccessDenied` on PutObject

## Symptom

`termicast deploy 001` fails:

```
s4cmd upload failed for audio/ep001.mp3: [Exception] An error occurred
(AccessDenied) when calling the PutObject operation: None
```

## Configuration

- Hosting: S3-compatible storage
- Endpoint: `https://us-ord-10.linodeobjects.com`
- Bucket: `twp`
- Asset base URL: `https://twp.us-ord-10.linodeobjects.com`
- Credentials: `~/.s3cfg` (`[default] access_key` / `secret_key`)

## Findings (in order)

| # | Test | Result | Meaning |
|---|---|---|---|
| 1 | `termicast deploy` | `AccessDenied` on `PutObject` | Linode *recognized* the key (not `InvalidAccessKeyId`) |
| 2 | `s4cmd ls s3://twp/ --endpoint-url=…` | empty (success) | `ListObjects` works |
| 3 | `s4cmd ls` (no endpoint) | `InvalidAccessKeyId` | `~/.s3cfg` holds a Linode key, not AWS |
| 4 | Cyberduck (same key) | write/delete/read all work | key *can* write via Cyberduck |
| 5 | `~/.aws/config` `addressing_style = virtual` + put | still `AccessDenied` | path vs virtual-hosted **ruled out** |
| 6 | `s4cmd mb s3://termicast-write-test` | succeeded | key can **create buckets** |
| 7 | `s4cmd put … termicast-write-test` | `AccessDenied` | key **cannot PutObject**, even to its own bucket |

## Conclusion so far

The key can **list** and **create buckets** but **cannot PutObject** — on any bucket. This is *not*:

- a read-only key (it creates buckets),
- a wrong region (fails on a fresh `us-ord-10` bucket too),
- an addressing-style problem (virtual-hosted changed nothing).

## Ruled out (2026-09-12 follow-up)

Further isolation, done live with Claude Code, eliminated one hypothesis
after another:

1. **s4cmd's `--sync-check` metadata.** A raw `boto3.put_object()` call — no
   s4cmd involved — got the identical `AccessDenied` using the exact
   `~/.s3cfg` key. Not an s4cmd-specific behavior.
2. **A stale/scoped key.** A brand-new access key, created fresh in Linode's
   dashboard, got the same `AccessDenied`.
3. **`twp`'s bucket policy.** `get_bucket_policy` showed a Linode-managed
   statement (`Sid: auto-875973-6109023`) allow-listing `s3:PutObject` to one
   specific principal. Replacing the policy with a minimal public-read-only
   policy did **not** restore write access.
4. **`twp`'s bucket ACL/ownership, and (briefly) an account-wide
   restriction.** A brand-new key creating and immediately writing to a
   brand-new bucket still got `AccessDenied` on the write (create/delete
   succeeded) — which briefly looked like an account-wide Linode restriction
   affecting every key. **That theory was wrong**: Cyberduck, using the
   *original* `~/.s3cfg` key (`RSHD4EX6...`) byte-for-byte, writes/deletes/
   reads that same bucket fine. Same key, same account, different client,
   different result — so identity/account status was never the problem.

## Actual root cause

**Newer `botocore` (1.43.93 here) defaults to adding S3 request checksums
(CRC32) on `PutObject`, which Linode Object Storage's backend rejects with a
generic `AccessDenied` instead of a checksum-specific error.** Cyberduck
doesn't send these headers, so it was never affected. `s4cmd`'s installed
build depends on `boto3`/`botocore` (confirmed: `boto3` 1.43.93 lives in the
same pipx venv as `s4cmd`), so its real uploads hit the exact same defect —
this was never an s4cmd quirk, a Linode account/key/bucket-policy problem,
or a Termicast bug at the S3-protocol level.

Confirmed fix, tested against a real deploy:

```bash
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
termicast deploy 001   # succeeded
```

**Shipped fix:** `termicast/s3deploy.py` now sets both env vars (via
`_s4cmd_env()`, `setdefault` so an operator's own explicit value always
wins) on every `s4cmd` subprocess invocation, so this is automatic — no
manual export needed. No further action required.
