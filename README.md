# Oasis Toolstate

This repo contains utilities for updating and publishing the status of the Oasis developer toolchain.

If you're a developer, you're probably here because you downloaded [installer.py](installer.py) and saw that it points here.

The other file here, [update_toolstate.py](update_toolstate.py), runs periodically and tests the latest tools.
Green builds are published as `latest-unstable`.

"Wait, what about [promote_current.sh](promote_current.sh)?" you ask.
Pay no attention to the person behind the curtain!
This will be replaced with an automated continuous deployer.
