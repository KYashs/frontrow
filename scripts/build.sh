#!/usr/bin/env bash
# Build the Lambda zips Terraform uploads:
#   build/functions/<name>.zip     one zip per Lambda: handler + core (+ catalogue for the seeder)
# The API runs on EKS as a container image (see Dockerfile), not from this script.
#
# Builds are reproducible: the same commit always produces byte-identical zips.
# Terraform hashes them, so a rebuild with no source change deploys nothing and
# the drift check stays quiet. Zips list files in sorted order with fixed
# timestamps and permissions, and contain no .pyc files.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/build"
rm -rf "$BUILD"
mkdir -p "$BUILD/functions"

zipdir() { python3 - "$1" "$2.zip" <<'PY'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
paths = []
for d, dirs, files in os.walk(src):
    dirs[:] = [x for x in dirs if x != "__pycache__"]
    paths += [os.path.join(d, f) for f in files if not f.endswith(".pyc")]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for p in sorted(paths, key=lambda p: os.path.relpath(p, src)):
        info = zipfile.ZipInfo(os.path.relpath(p, src).replace(os.sep, "/"), date_time=(1980, 1, 1, 0, 0, 0))
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        with open(p, "rb") as f:
            z.writestr(info, f.read(), compresslevel=9)
PY
}

copy_core() {
  mkdir -p "$1/core"
  cp "$ROOT"/core/*.py "$1/core/"
}

copy_catalog() {
  mkdir -p "$1/catalog" "$1/venues"
  cp "$ROOT"/catalog/events.json "$1/catalog/"
  cp "$ROOT"/venues/*.json "$1/venues/"
}

# ---- Lambda zips (boto3 comes with the Lambda runtime)
for dir in "$ROOT"/functions/*/; do
  name="$(basename "$dir")"
  stage="$BUILD/stage/$name"
  mkdir -p "$stage"
  cp "$dir/handler.py" "$stage/"
  copy_core "$stage"
  if [ "$name" = "inventory_seeder" ]; then copy_catalog "$stage"; fi
  zipdir "$stage" "$BUILD/functions/$name"
  echo "built build/functions/$name.zip"
done

rm -rf "$BUILD/stage"
