# Catalog submission and re-pinning

The Hermes plugin catalog identifies a plugin by repository and an exact commit
(`sha:`), never by a branch or `latest`. Teleoperator's receiver download URL is
derived from `v{VERSION}`, so the catalog commit and the release tag must agree:
the tag must be created at the commit the catalog pins.

## Entry shape

```yaml
name: hermes-teleoperator
repo: https://github.com/Tsurgcom/hermes-teleoperator
sha: <40-character commit SHA>
title: Hermes Teleoperator
version: "1.1.2"
requires_hermes: ">=0.21.3"
```

`sha:` is the full, reviewed commit SHA. Do not shorten it, point it at a branch
tip that you have not reviewed, or update it independently of the source.

## Re-pinning after a security fix

1. Land the fix and its tests.
2. Confirm `python scripts/sync_receiver_digests.py --check` passes and the
   release assets' published `SHA256SUMS` matches `RECEIVER_SHA256` in
   `hermes_teleoperator/version.py`.
3. Set `sha:` to the fix commit. Record the same commit in the review comment.
4. If a release is being cut for this version, tag `v{VERSION}` at that exact
   commit and let the release workflow verify the built assets against the pins
   in that commit before publishing. See [RELEASING.md](../../../RELEASING.md).

## Verify the relationship

```sh
git rev-parse v1.1.2^{commit}   # must equal catalog sha:
python scripts/verify_release_digests.py artifacts
gh attestation verify hermes-teleoperator-receiver-x86_64-unknown-linux-musl \
  --repo Tsurgcom/hermes-teleoperator
```

If the tag commit and the catalog `sha:` differ, fix the catalog entry (or cut
the release from the pinned commit); never move an existing release tag.

## Current note

`v1.1.2` predates source-pinned digests. Its assets are unchanged and their
published manifest matches the pins now committed in `version.py`. The catalog
must pin the commit that contains that table; the next release cut through
`RELEASING.md` must tag that same commit.
