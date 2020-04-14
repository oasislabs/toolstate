#!/bin/bash
# This script should go away when we have automated deployment.

set -euo pipefail

tools_url=s3://tools.oasis.dev

year=$(date +"%y")
week=$(date +"%V")
release="$year.$week"

confirmation="Do it!"

read -r -p "Promote \`current\` to release $release? Type '$confirmation' to confirm: " response
if [[ "$response" = "$confirmation" ]]; then
    aws s3 sync $tools_url/linux/current $tools_url/linux/release/$release
    aws s3 sync $tools_url/darwin/current $tools_url/darwin/release/$release
    echo "Congrats on release $release! 🎉"
else
    echo "Maybe next time.."
fi
