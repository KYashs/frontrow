#!/usr/bin/env bash
# Prints the API image tag: a hash of every committed file that goes into the
# image. Same inputs, same tag, so CI skips the build and Argo CD changes
# nothing when a commit doesn't touch the API.
set -euo pipefail
cd "$(dirname "$0")/.."
git ls-files -z -- Dockerfile .dockerignore 'app/*.py' app/requirements.txt app/constraints.txt \
    'core/*.py' catalog/events.json 'venues/*.json' \
  | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-16
