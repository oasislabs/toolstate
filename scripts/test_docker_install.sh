#!/bin/bash

cd $(git rev-parse --show-toplevel)/.circleci/docker
docker build . -t installer_tester:latest
cd ../../
docker_extra_cmd="exit 0"
if [ $1 == "-i" ]; then
    docker_extra_cmd="bash"
fi
docker run -it --rm -v $(pwd):/mnt installer_tester python /mnt/installer.py && $docker_extra_cmd
