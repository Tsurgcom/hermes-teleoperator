"""Gateway/receiver release identity; kept in sync with package manifests."""

VERSION = '1.1.2'

# Per-asset SHA256 for the exact release tag ``v{VERSION}``. This table is the
# trust root for receiver downloads: deploy.py and install.sh verify downloaded
# bytes against these values. SHA256SUMS is still fetched and cross-checked so a
# rewritten release is detected, but it is never trusted on its own.
# Regenerate with ``python scripts/sync_receiver_digests.py`` after a release
# build, then commit before tagging that release.
RECEIVER_SHA256 = {
    'hermes-teleoperator-receiver-aarch64-apple-darwin':
        '6ebdbc07ca7cc59c59c2cbeb59fc3ab5c0fe37e0f74e7750039b355f20e49ad5',
    'hermes-teleoperator-receiver-aarch64-unknown-linux-gnu':
        '486cb6589495af5c3ba05584deef6187da41f97e20fdcb6e95aeddd2211747f7',
    'hermes-teleoperator-receiver-aarch64-unknown-linux-musl':
        '9d1546d1754d75017d40542018167b765407f5ecc7d2b2ecf7071414ace1ec82',
    'hermes-teleoperator-receiver-armv7-unknown-linux-gnueabihf':
        '2a1d6c1e304a67d73c13a7762aee3b0bdc683a28595b88fcd9873bfaa10054fb',
    'hermes-teleoperator-receiver-armv7-unknown-linux-musleabihf':
        '130a39607f532e648471e3ae00ec8ca26972431aef71210f3b324065a44047ee',
    'hermes-teleoperator-receiver-i686-unknown-linux-gnu':
        'a94134bf3a017dae6a5a58a612d2cf8f4fdb99cf5c298d6c9a3ad5ef1e12dfc8',
    'hermes-teleoperator-receiver-i686-unknown-linux-musl':
        'ceb0e0c7154c97a6ad9de879d13f77f40c62eb3e6e845e91815f8de9b12f2100',
    'hermes-teleoperator-receiver-powerpc64le-unknown-linux-gnu':
        '2d0b155f09954f81a6706b4de80a0f97d97421239927b811f55138bd8d3c21fb',
    'hermes-teleoperator-receiver-riscv64gc-unknown-linux-musl':
        '540c333c328aa81685fdef0a9d685cd9ee887a07152b92bbd8d41863fc743211',
    'hermes-teleoperator-receiver-s390x-unknown-linux-gnu':
        '08dfd11de53e096ab088cc28da3b678cccf0cc64f8012040eee6ae398f761cc9',
    'hermes-teleoperator-receiver-x86_64-apple-darwin':
        'fcc3542810d5fb2fdfa63b891f67016500901214c79db0536bfba8c93401edc7',
    'hermes-teleoperator-receiver-x86_64-unknown-linux-gnu':
        'd1dbad5d26fd86ea91fa79c3f2327073a055737f3a26cadc4dcfa15719f8c359',
    'hermes-teleoperator-receiver-x86_64-unknown-linux-musl':
        'd639b8f89309f0c362aefbc26a311d0f305fe5ed9de0cc636e84c1367cc16861',
}
