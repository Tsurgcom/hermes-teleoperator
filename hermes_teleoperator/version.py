"""Gateway/receiver release identity; kept in sync with package manifests."""

VERSION = '1.2.1'

# The digests below are still the attested pins for the v1.2.0 release
# artifacts. They are not the bytes of this tree. Regenerate them with
# ``python scripts/sync_receiver_digests.py --from-sums SHA256SUMS`` from a
# v1.2.1 candidate build before tagging. deploy.py and install.sh verify
# downloads against this table; the publish job refuses a tag whose rebuilt
# assets do not match it. SHA256SUMS is cross-checked and is never the trust root.
RECEIVER_SHA256 = {
    'hermes-teleoperator-receiver-aarch64-apple-darwin':
        '6cb7f126fc9d08a5457b868534267c7cac1098ac0fcea50224e2db1948d9fa41',
    'hermes-teleoperator-receiver-aarch64-unknown-linux-gnu':
        'f4f298474b491a587c4be3e201318be8c23c12effb3baa3949b084b281c4e704',
    'hermes-teleoperator-receiver-aarch64-unknown-linux-musl':
        '1483f57d4e54410d6b13964a62d9563cee8efffc2b5d08dc0fe29f3643e2bfdd',
    'hermes-teleoperator-receiver-armv7-unknown-linux-gnueabihf':
        '63ba98b87292f992b8ac2fca9b83494f5073bcc2234dd798da9d1338e8e914a6',
    'hermes-teleoperator-receiver-armv7-unknown-linux-musleabihf':
        '3230e864e7be4e1d938619d607953e1d02fa44a873c204af4a99149fb3cd3b38',
    'hermes-teleoperator-receiver-i686-unknown-linux-gnu':
        '34b4d5297340adf95560797b9bb853efc09f1f4420cdcf3a1b4c6ac54606173b',
    'hermes-teleoperator-receiver-i686-unknown-linux-musl':
        'b4f6b37aa8a6e5c547651db441346bb0a00aafa2f502f97b85db2321c5ea93b6',
    'hermes-teleoperator-receiver-powerpc64le-unknown-linux-gnu':
        'dbf82b36e5cad55bbd696a7e0086bfe66360e081ab737eaf6a7ec5ea3a888a9a',
    'hermes-teleoperator-receiver-riscv64gc-unknown-linux-musl':
        'b8e9518acc40987ef998786652571189fb6ed84cbb718291962f519ddf5b696c',
    'hermes-teleoperator-receiver-s390x-unknown-linux-gnu':
        '731b57110263d3b0e9a4db00108e2dec51bd315bdc79d8d1af9203499f6a16fd',
    'hermes-teleoperator-receiver-x86_64-apple-darwin':
        '8f3180c6f3f03533c079e556d39531158a2a1c965fe0d8889fea25bb0c930e6a',
    'hermes-teleoperator-receiver-x86_64-unknown-linux-gnu':
        '7223d86340447fb7d378021a10e324c95db79ade2be87e4dce2f937cb1b34786',
    'hermes-teleoperator-receiver-x86_64-unknown-linux-musl':
        '2013038cc40dd0068a149a02bf53ba5240bfab9e6a086a9e62717136e6919110',
}
