#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

git pull

# --provenance=false --sbom=false avoids a BuildKit gotcha: without them,
# attestation metadata turns the image into a multi-manifest index that
# doesn't cleanly retag on a repeat `ctr images import` of the same tag.
docker build --provenance=false --sbom=false -t codebase-searcher:latest .
docker save codebase-searcher:latest -o codebase-searcher.tar

sudo k3s ctr images rm docker.io/library/codebase-searcher:latest || true
sudo k3s ctr images import codebase-searcher.tar
rm -f codebase-searcher.tar

kubectl rollout restart deployment codebase-searcher-api
kubectl rollout status deployment codebase-searcher-api
