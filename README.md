# Oasis Toolstate

![.github/workflows/toolstate.yml](https://github.com/oasislabs/toolstate/workflows/.github/workflows/toolstate.yml/badge.svg)

This repo contains utilities for updating and publishing the status of the Oasis SDK.

If you're a developer, you're probably here because you downloaded [installer.py](installer.py) and saw that it points here.

The other file here, [update_toolstate.py](update_toolstate.py), runs periodically and tests the latest tools.
Green builds are published as `unstable` and can be downloaded using `oasis set-toolchain unstable` or by passing `--toolchain unstable` to the installer.
