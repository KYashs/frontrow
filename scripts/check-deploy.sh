#!/usr/bin/env bash
# Renders every chart Argo CD will sync, with this repo's values, and validates
# the result against Kubernetes and CRD schemas. Runs in CI (.github/workflows/test.yml)
# and locally. Needs helm, python3 with PyYAML, and kubeconform (or docker).
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

api=(--set image.repository=123456789012.dkr.ecr.ap-south-1.amazonaws.com/frontrow-api --set image.tag=0123456789abcdef
     --set targetGroupArn=arn:aws:elasticloadbalancing:ap-south-1:123456789012:targetgroup/frontrow-tg/0123456789abcdef
     --set env.QUEUE_URL=https://sqs.ap-south-1.amazonaws.com/123456789012/frontrow-checkout.fifo)

echo "== lint"
helm lint --quiet deploy/root
helm lint --quiet deploy/charts/platform-base
helm lint --quiet deploy/charts/frontrow-api "${api[@]}"

echo "== render this repo's charts"
helm template root deploy/root --namespace argocd > "$OUT/root.yaml"
helm template frontrow-api deploy/charts/frontrow-api --namespace frontrow "${api[@]}" > "$OUT/api.yaml"
helm template platform-base deploy/charts/platform-base > "$OUT/platform-base.yaml"

echo "== render third-party charts at the pinned versions, with deploy/platform values"
chart() { python3 -c 'import sys, yaml; c = yaml.safe_load(open("deploy/root/values.yaml"))["charts"][sys.argv[1]]; print(c[sys.argv[2]])' "$1" "$2"; }
# Charts are downloaded into CHART_CACHE (default: a temp dir); set it to reuse downloads.
CACHE="${CHART_CACHE:-$OUT/charts}"
mkdir -p "$CACHE"
third() { # name, values key, chart, namespace, extra args...
  local name=$1 key=$2 ch=$3 ns=$4; shift 4
  local version dir
  version="$(chart "$key" version)"
  dir="$CACHE/$ch-$version"
  for attempt in 1 2 3; do
    [ -f "$dir/$ch/Chart.yaml" ] && break
    rm -rf "$dir"; mkdir -p "$dir"
    helm pull "$ch" --repo "$(chart "$key" repo)" --version "$version" --untar --untardir "$dir" > /dev/null 2>&1 \
      || { echo "   download of $ch $version failed (attempt $attempt)"; sleep $((attempt * 5)); }
  done
  grep -q "^version: $version$" "$dir/$ch/Chart.yaml"   # the cache must hold the pinned version
  helm template "$name" "$dir/$ch" --namespace "$ns" -f "deploy/platform/$name.yaml" "$@" > "$OUT/$name.yaml"
  echo "   $name $version: $(grep -c '^kind:' "$OUT/$name.yaml") objects"
}
third aws-load-balancer-controller awsLoadBalancerController aws-load-balancer-controller kube-system \
  --set clusterName=frontrow --set region=ap-south-1 --set vpcId=vpc-0123456789abcdef0
third metrics-server metricsServer metrics-server kube-system
third kube-prometheus-stack kubePrometheusStack kube-prometheus-stack monitoring --kube-version 1.37.0
third loki loki loki monitoring \
  --set loki.storage.bucketNames.chunks=b --set loki.storage.bucketNames.ruler=b \
  --set loki.storage.bucketNames.admin=b --set loki.storage.s3.region=ap-south-1
third alloy alloy alloy monitoring

echo "== validate against schemas"
catalog='https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
args=(-strict -summary -schema-location default -schema-location "$catalog" -skip CustomResourceDefinition)
if command -v kubeconform > /dev/null; then
  kubeconform "${args[@]}" "$OUT"/*.yaml
else
  docker run --rm -v "$OUT:/m:ro" ghcr.io/yannh/kubeconform:v0.7.0 "${args[@]}" /m
fi
