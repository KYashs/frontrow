# deploy/

Everything Argo CD runs in the EKS cluster. Git is the source of truth: Argo CD
syncs this folder from `main` and reverts manual changes made with `kubectl`.

| Path | What it is |
|---|---|
| `argocd/values.yaml` | Settings for Argo CD itself, installed by Terraform (`terraform/argocd.tf`) |
| `root/` | The root "app of apps": one Argo CD Application per component, with sync waves |
| `charts/frontrow-api/` | The API: Deployment, HPA, PDB, TargetGroupBinding to the ALB, ServiceMonitor, alerts, Grafana dashboard |
| `charts/platform-base/` | Cluster basics: the default gp3 StorageClass |
| `platform/*.yaml` | Pinned settings for third-party charts (load balancer controller, metrics-server, Prometheus/Grafana, Loki, Alloy) |

Terraform passes in only what it alone knows (cluster name, VPC, target group,
table names, image tag) as values on the root Application. Chart versions live
in `root/values.yaml`, so upgrading one is a pull request.

Sync order (waves): platform-base, then the load balancer controller and
metrics-server, then monitoring, then the API.
