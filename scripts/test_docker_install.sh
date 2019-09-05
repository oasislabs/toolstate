#!/bin/bash

cd $(git rev-parse --show-toplevel)/.circleci/docker
docker build . -t installer_tester:latest
cd ../../
docker run -it --rm -v $(pwd):/mnt installer_tester python /mnt/installer.py
