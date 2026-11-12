# Releasing the receiver

A release is trustworthy only when three things agree: the catalog pin, the tag
commit, and the bytes committed in source. This document is the release
checklist; it is also the compatibility contract that installers rely on.

## Invariants

1. **One source of truth for digests.** `hermes_teleoperator/version.py`
   contains `RECEIVER_SHA256`, one digest for every asset in
   `hermes_teleoperator/deploy.py:ASSETS`. `install.sh` renders the same table
   into its marked block for the standalone installer. `deploy.py` and
   `install.sh` compare downloaded bytes to those pins. `SHA256SUMS` is fetched
   only to reject a release that rewrote both the asset and its manifest.
2. **The tag is the pinned commit.** The marketplace/plugin catalog entry pins
   a commit with `sha:`. The release tag `v{VERSION}` must point at that exact
   commit (`git rev-parse v{VERSION}^{commit}` equals the catalog `sha`). Do not
   publish a tag from a branch tip that differs from the pinned commit, and do
   not move a tag after publication.
3. **Assets are built where provenance can be verified.** Linux and macOS
   receiver assets are built on GitHub-hosted runners. Every asset is attested
   with `actions/attest-build-provenance`; the publish job also attests
   `SHA256SUMS`. Self-hosted runners are not used for release assets because
   their environment is not attested by GitHub.
4. **A failed pin stops the release.** The publish job runs
   `scripts/verify_release_digests.py` before uploading. If any built asset
   differs from the source pin, the release fails. Never update the pins to
   match an unexplained build; investigate toolchain or environment drift.

## Release process

1. **Bump the version** in `hermes_teleoperator/version.py`, `pyproject.toml`,
   `receiver/Cargo.toml`, `receiver/Cargo.lock`, `plugin.yaml`, and
   `install.sh`. `tests/test_version.py` enforces parity.
2. **Build a release candidate.** Run the `Receiver release` workflow with
   `workflow_dispatch` on the release-candidate commit. It builds and attests
   the assets but does not publish. Download the `receiver-*` artifacts and
   produce a manifest:

   ```sh
   sha256sum hermes-teleoperator-receiver-* > SHA256SUMS
   ```

3. **Pin the digests.** On top of that commit, run:

   ```sh
   python scripts/sync_receiver_digests.py --from-sums SHA256SUMS
   ```

   This updates `RECEIVER_SHA256` and regenerates the marked block in
   `install.sh`. Commit the result. Only digest data and installer text may
   change after the candidate build; changing `receiver/` source invalidates
   the build.
4. **Tag the pinned commit** (the commit that contains the pins):

   ```sh
   git tag -a v<VERSION> -m 'v<VERSION>' <PINNED_COMMIT>
   git push origin v<VERSION>
   ```

   Creating the tag triggers the release workflow. It rebuilds from that exact
   commit, verifies every built asset against the pins in the same commit,
   attests the assets, and opens a draft release.
5. **Publish the draft** only after checking the workflow's provenance
   summary and the generated `SHA256SUMS`. Then set the catalog `sha:` to the
   same commit as the tag. If the catalog pin and tag commit differ, stop and
   fix the catalog, not the tag.

### If the tag build differs from the pinned bytes

The publish job fails closed with the mismatched assets listed. Do not edit the
pins to make it pass. Common causes are an unpinned toolchain, a different
runner image, or changed receiver source between the candidate build and the
pinned commit. Pinning `dtolnay/rust-toolchain` and `cargo-zigbuild` in the
workflow is deliberate: keep them pinned and rebuild the candidate from the
pinned commit. If the mismatch remains unexplained, delete the unpublished
draft/tag and start the candidate step again.

## Verify a published release

```sh
TAG=v1.1.2
git rev-parse "$TAG^{commit}"                    # must equal the catalog sha:
gh attestation verify hermes-teleoperator-receiver-x86_64-unknown-linux-musl \
  --repo Tsurgcom/hermes-teleoperator
curl -fsSL "https://github.com/Tsurgcom/hermes-teleoperator/releases/download/$TAG/SHA256SUMS"
```

The checked-in verifier can be run in CI against downloaded artifacts:

```sh
python scripts/verify_release_digests.py artifacts
python scripts/sync_receiver_digests.py --check
```

## Current release state

`v1.1.2` was tagged before source-pinned digests existed; its assets are
unchanged and its published manifest matches the pins now committed here. The
marketplace catalog must pin the commit that contains this digest table, and the
next release cut from this process must tag that same commit. Do not rewrite or
move the existing `v1.1.2` tag.

`VERSION` is `1.3.0`, but `RECEIVER_PINS_VERSION` is still `1.2.0`. Those
digests attest the v1.2.0 binaries, not this tree. `deploy.py` and
`scripts/verify_release_digests.py` refuse to deploy or publish until a v1.3.0
candidate build is pinned with `sync_receiver_digests.py --from-sums`. Do not
tag `v1.3.0` before that. `--from-sums` also sets `RECEIVER_PINS_VERSION` to
the package version.
