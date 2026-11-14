# FrontRow architecture

How FrontRow sells 114,000 individually-numbered seats and 75,000 general-admission places across four stadiums, while a crowd of buyers clicks at once, without ever selling the same seat twice.

This document is the reasoning behind the design: requirements, the request paths, the decisions and their trade-offs, capacity math, failure modes, security, cost, and what changes at Ticketmaster or BookMyShow scale.

---

## 1. Requirements

**Functional**
- Browse shows, venues and price tiers; see a live seat map of every section.
- Pick specific seats (reserved sections) or a quantity (standing / GA zones), up to 8 tickets.
- Hold them for 8 minutes while paying; get them back on sale automatically if the buyer walks away.
- Pay, receive a confirmation email with a ticket code.
- Queue fairly when demand exceeds what the system will admit.

**Non-functional**
| Property | Target |
|---|---|
| Correctness | A seat is sold to at most one order. GA zones never oversell. Zero tolerance. |
| Hold latency | p95 under 300 ms at the API (single DynamoDB transaction). |
| Read scale | Browse traffic absorbed at the edge. The origin sees roughly one request per POP per 2 s per hot URL. |
| Burst | 0 to thousands of hold attempts/s within seconds, no pre-warming. |
| Availability | Survive the loss of one AZ. Checkout survives a worker outage (queue buffers). |
| Fairness | First come, first served at the waiting room; admission rate matched to back-end capacity. |
| Security | Nothing but the ALB (only from CloudFront) and the IAM-authenticated EKS API endpoint is internet-reachable. Least-privilege IAM per component and per pod. |

---

## 2. The request paths

```
Browse    Browser -> CloudFront -> WAF -> edge cache -> ALB -> API pod (EKS) -> RDS / DynamoDB
Queue     Browser -> ... -> API pod -> DynamoDB sharded counter; status checks are HMAC only
Hold      Browser -> ... -> API pod -> DynamoDB TransactWriteItems (the correctness boundary)
Checkout  Browser -> ... -> API pod -> lock hold -> SQS FIFO -> Lambda -> DynamoDB transaction -> EventBridge
Fan-out   EventBridge -> Lambda -> SES (email)
          EventBridge -> Firehose -> S3 -> Glue/Athena (analytics)
Live map  DynamoDB Streams -> Lambda -> API Gateway WebSocket -> every open seat map
Cleanup   EventBridge schedule (1 min) -> Lambda sweeper -> expired holds back on sale
Metrics   API pods /metrics -> Prometheus -> Grafana (alerts via PrometheusRule)
Logs      every pod -> Alloy -> Loki (S3) -> Grafana
Deploy    merge to main -> GitHub Actions: Terraform (AWS) + image to ECR -> Argo CD syncs deploy/ into EKS
```

---

## 3. Architecture decision records

### ADR-1: Seats are DynamoDB items; RDS keeps the catalogue
**Context.** Two very different access patterns. The catalogue (venues, events, price tiers, which section has which price) is relational, small and read-mostly. Seat state is huge (114k items) and written thousands of times a second during an on-sale, always by exact key.
**Decision.** RDS PostgreSQL for the catalogue; DynamoDB for seats, holds, orders, counters.
**Why not one database?** A single Postgres primary would serialise every hold through one writer and one connection pool. `SELECT ... FOR UPDATE` works, but at ticket-drop concurrency the pool, not the disk, is the bottleneck. DynamoDB conditional writes give the same per-seat atomicity with horizontal partitioning and no connections to exhaust.
**Trade-off.** Two stores to operate; price lookups at hold time read RDS (one indexed query), so RDS stays on the write path's critical dependency list. Acceptable: catalogue reads are tiny and cacheable, and RDS is in two subnets with an optional Multi-AZ standby.

### ADR-2: Partition seats by event **and section**
**Context.** DynamoDB caps a single partition key at about 1,000 writes/s. Partitioning seats by `event_id` alone would funnel an entire on-sale into one key.
**Decision.** `pk = "<event>#<section>"`, `sk = "R05#S12"`. A section's grid is one Query; an on-sale spreads across 33 to 215 partition keys per show.
**Trade-off.** "Every seat in the event" needs many queries. Nothing in the product needs that; the map uses per-section counters (ADR-6).

### ADR-3: The hold is the correctness boundary
**Context.** The original design (one queue message per purchase attempt) resolves contention after the buyer has already filled in payment, which is the worst moment to say "sorry, it's gone".
**Decision.** Contention is resolved synchronously when the buyer clicks *Hold*. One `TransactWriteItems` updates every requested seat with the condition `status = available OR (status = held AND hold_expires < now)` and writes the hold record. All seats or none. Payment only starts for seats you already own.
**Consequences.** Two buyers racing for seat 14 are told within milliseconds. A 2-seat hold can never end up as 1 seat. An expired hold is purchasable immediately, even before the sweeper runs, because the condition itself treats it as free.
**Verified by.** `tests/test_inventory.py` (25 buyers, one seat, exactly one winner; all-or-nothing multi-seat) and `scripts/race_test.py` against the deployed stack.

### ADR-4: SQS FIFO for checkout, grouped by hold
**Context.** Payment is slow and fallible, and a payment provider outage must not take the shop down.
**Decision.** `POST /api/orders` locks the hold (`active -> checkout`, seats' expiry extended by the payment grace period) in one transaction, enqueues to SQS FIFO and returns `202` in milliseconds. A Lambda worker pays and confirms.
**Why FIFO, and why group by hold rather than by seat?** Once seats are held there is no contention left to order. FIFO now provides exactly-once enqueue (dedup on `order_id`) and in-order processing per hold, while high-throughput mode (`perMessageGroupId`) keeps different holds fully parallel. Grouping by seat, as in the original design, would only matter if the queue were still the place where contention is decided. It isn't.
**Idempotency.** SQS delivers at least once, so the confirm transaction includes `attribute_not_exists(order_id)` on the order record. A redelivered message finds its order and stops.

### ADR-5: GA zones use sharded counters
**Context.** A 15,000-capacity field is one number. One DynamoDB item caps out around 1,000 writes/s, and every buyer would contend on it.
**Decision.** Each GA zone is split into 8 counter items (`GA#FIELD#0..7`). A hold picks shards in random order and decrements one with `available >= qty`. Availability is the sum.
**Trade-off.** A request for 4 can fail when no single shard has 4 left even though the zone total does (fragmentation near sell-out). Fix if it matters: split the request across shards in one transaction.

### ADR-6: Live seat map = Streams + WebSocket; counters are derived
**Decision.** DynamoDB Streams on `seats` feed a Lambda that (a) aggregates status changes per section into one counter `ADD` per batch, and (b) pushes seat-level changes to every WebSocket connection watching that event. A second stream on `inventory` pushes counter deltas (including GA) to the same clients.
**Why derived counters?** Updating a section counter inside every hold transaction would make each section counter a hot item and add an item to every transaction. Deriving it from the stream batches up to 500 changes into one write and keeps the hold path short. The cost is that counters are eventually consistent, typically within about a second. That is fine because counters are display-only; the hold condition is the authority.
**Filtering.** The event source mapping filters to `MODIFY` events, so seeding 114k seats does not invoke the function 114k times.

### ADR-7: The waiting room is arithmetic, not a worker
**Context.** Classic waiting rooms run an admission worker and a database of queued sessions.
**Decision.** Joining takes a number from one of 10 sharded counters. Admission is computed: `admitted_upto = initial + rate x (now - opens_at)`. Tickets and access passes are HMAC-signed, so "is it my turn?" is one cached config read and a signature check, with no writes. A new on-sale bumps an epoch, which invalidates every old pass.
**Choosing the rate.** Set `rate` to what the hold path can absorb (see §4). The waiting room turns an unbounded spike into a flat line the rest of the system was sized for.
**Trade-off.** Positions from sharded counters are interleaved: two people who join in the same millisecond may be one position apart in either order. Acceptable for fairness at human time scales.

### ADR-8: The edge is the first scaling tier
**Decision.** CloudFront serves the site and the venue geometry from S3. API responses carry their own `Cache-Control` (catalogue 30 s, availability and seat grids 2 s, writes `no-store`) and a custom cache policy honours it. AWS WAF on the distribution applies AWS managed rules, a per-IP rate limit on `/api/*` and a much tighter one on `/api/holds` (the endpoint bots target).
**Effect.** During a stampede, a million identical "what's left in section 113?" requests become about one origin request per POP every 2 seconds. Stale-by-2-seconds is acceptable because the hold is authoritative and the WebSocket corrects the map.

### ADR-9: EventBridge decouples everything after the sale
**Decision.** The checkout worker publishes `OrderConfirmed` / `OrderFailed` to a custom bus and knows nothing about consumers. Today: SES email (Lambda) and the analytics lake (Firehose with dynamic partitioning by event and day, a Glue table with partition projection, Athena named queries). Tomorrow: fraud scoring, loyalty points, CRM sync, all without redeploying checkout.
**Privacy.** No email or other PII on the bus. The notifier reads the address from the orders table; the lake holds no personal data.

### ADR-10: EKS for the API, Lambda for event-driven work
**Decision.** The API runs as pods on Amazon EKS (Kubernetes 1.37, a managed node group across two AZs). Pods keep long-lived connection pools to RDS and give predictable latency; a HorizontalPodAutoscaler adds pods on CPU, one uvicorn worker each, so scaling is Kubernetes' job rather than a process manager's. Everything triggered by an event (queue message, stream record, schedule, bus event, WebSocket route) stays Lambda on Graviton, outside the VPC: there is nothing to keep warm and nothing to patch.
**How it joins the existing edge.** CloudFront, WAF and the ALB stay in Terraform, unchanged. The AWS Load Balancer Controller registers pod IPs into the Terraform-owned target group through a `TargetGroupBinding`, so the controller never creates load balancers and its IAM role only needs to register targets in that one group.
**Why EKS over the EC2 Auto Scaling group it replaced.** Rolling deploys with readiness gates and a PodDisruptionBudget instead of instance refreshes; seconds to add capacity instead of minutes; one place to run the observability stack; and the platform that ticketing companies run at scale.
**Trade-off.** About $5/day more than the EC2 design (control plane, larger nodes for Prometheus and Loki, a NAT gateway, minus the interface endpoints it no longer needs), and a cluster to upgrade. `upgrade_policy = STANDARD` makes EKS upgrade the cluster rather than silently move it to extended support at 6x the control-plane price.
**Alternatives.** ECS Fargate is a simpler drop-in for the API (stateless, configured by environment). API Gateway + Lambda for the whole API would also work, with RDS Proxy for connections.

### ADR-11: No NAT gateway (superseded by ADR-13)
*Kept for the record: this was the EC2 design.*
**Decision.** Private subnets reach S3 and DynamoDB through gateway endpoints, and SQS, Secrets Manager and SSM through interface endpoints. Python dependencies are vendored into the bundle at build time. Instances cannot reach the internet at all.
**Effect.** A smaller attack surface, no NAT hourly or data-processing charges, and SSM Session Manager instead of SSH or a bastion.

### ADR-12: Git is the source of truth (GitOps)
**Context.** A stack changed from laptops drifts: nobody can say what is running, or why.
**Decision.** `main` is what runs in AWS. Pull requests get a Terraform plan posted for review; merging applies it from GitHub Actions; a daily drift check opens an issue when AWS differs from `main`. Workflows assume IAM roles over OIDC, so no AWS keys live in GitHub. A read-only role plans; an admin-equivalent deploy role is assumable only from the `production` environment, which is restricted to `main`. State is in S3 with versioning and S3-native locking. Production settings live in `terraform/environments/prod.tfvars`; personal addresses come from repository variables.
**Prerequisite: reproducible builds.** Terraform deploys Lambda zips by hash, and `scripts/build.sh` writes them with sorted entries and fixed timestamps, so the same commit always produces the same bytes. The API image is tagged with a hash of its inputs (`scripts/image-tag.sh`), with every dependency pinned in `app/constraints.txt`, so an unchanged API is never rebuilt or redeployed. Without that, every run would redeploy every Lambda, roll every pod, and report drift.
**Trade-off.** Apply runs only on merge, so a hotfix also goes through a pull request. The deploy role is effectively an administrator, because the stack creates IAM roles; the guard is who can assume it, not what it can do. Runtime data (seat inventory, waiting-room settings changed with `scripts/onsale.py`) is not infrastructure and stays outside GitOps.

### ADR-13: One NAT gateway for the cluster
**Context.** ADR-11 kept private subnets off the internet. EKS nodes must pull public images (Argo CD, Prometheus, Grafana, Loki, Alloy) and reach several AWS APIs.
**Decision.** One NAT gateway in one public subnet. S3 and DynamoDB keep their free gateway endpoints, so seat traffic and ECR image layers (served from S3) skip the NAT's per-GB charge. The interface endpoints for SQS, Secrets Manager and SSM are removed: through the NAT they cost nothing extra, against ~$2.60/day. The database subnets get their own route table with no internet route.
**Trade-off.** Outbound traffic now leaves the VPC (still TLS, still IAM-signed). One NAT is a single-AZ dependency: if its AZ fails, running pods keep serving but nodes in the other AZ can't pull new images until it recovers. Production would run one NAT per AZ (+$1.35/day each). The alternative, ECR pull-through caches plus about six more interface endpoints, keeps ADR-11 but needs Docker Hub and GHCR credentials and costs more.

### ADR-14: Two GitOps loops: Terraform for AWS, Argo CD for the cluster
**Decision.** GitHub Actions applies Terraform for everything in AWS, as in ADR-12. Inside the cluster, Argo CD pulls `deploy/` from `main` and keeps the cluster equal to it: self-heal reverts manual `kubectl` changes, prune removes what was deleted from git. Terraform installs Argo CD and a single root Application; that root renders one Application per component (platform base, load balancer controller, metrics-server, kube-prometheus-stack, Loki, Alloy, the API) with sync waves so CRDs and controllers exist before what uses them.
**Where values come from.** Git owns chart versions and settings (`deploy/root/values.yaml`, `deploy/platform/*.yaml`). Terraform passes only what it alone knows (cluster name, VPC, target group ARN, table names, image tag) as values on the root Application. The image tag is a content hash, so no bot has to commit tag bumps to `main`.
**Trade-off.** Two reconcilers to understand. Deleting the stack needs Argo CD to clean up first (the Destroy workflow does this), or EBS volumes from Prometheus and Loki would outlive the cluster. Argo CD has no public endpoint; its UI is a `kubectl port-forward`.

### ADR-15: Observability: Prometheus, Grafana and Loki in the cluster, CloudWatch for serverless
**Decision.** kube-prometheus-stack runs Prometheus (7-day retention on a gp3 volume), Alertmanager and Grafana. The API exposes RED metrics on `/metrics` (labelled by route template, never raw IDs), scraped through a ServiceMonitor; a PrometheusRule alerts on hold p95 above 300 ms (the requirement in §1), 5xx ratio above 2%, and fewer than two ready pods. Alloy ships every pod's logs and Kubernetes events to Loki, which stores chunks in S3. Grafana ships a FrontRow dashboard from git and has three data sources: Prometheus, Loki and CloudWatch, so pods, Lambdas, SQS and DynamoDB are on one screen.
**Why keep CloudWatch.** Lambdas, SQS, DynamoDB and the ALB already publish there, the existing alarms page through SNS, and running exporters for them would duplicate it. `/metrics` is outside `/api`, so CloudFront never routes it.
**Trade-off.** Prometheus and Loki are single replicas: fine for one demo cluster, not for production (use Amazon Managed Prometheus or Thanos, and Loki in simple-scalable mode).

### ADR-16: IAM for people and pods
**Pods.** EKS Pod Identity maps each service account to its own role: the API (the same least-privilege policy the EC2 role had), the load balancer controller (register targets in one target group), Loki (one bucket), Grafana (read CloudWatch), and the EBS CSI driver. Nodes use IMDSv2 with a hop limit of 1, so pods can't fall back to the node's role.
**People.** Users get no permissions directly. Each is in one group (`frontrow-admins`, `frontrow-developers`, `frontrow-readonly`) that may only assume the matching role, and the roles require MFA. EKS access entries trust those same roles, so one identity covers the console, the CLI and `kubectl`: admins are cluster admins; developers can edit the `frontrow` namespace and view the rest; read-only can view. Developers and read-only get `ViewOnlyAccess`, which shows configuration but not data, so order emails stay private; developers add only what the on-sale scripts need. Membership is a map in `terraform/environments/shared.tfvars`, changed by pull request.
**Lifecycle.** Team IAM and ECR live in `terraform/shared`, which the Destroy workflow never touches, so tearing down the stack doesn't delete people or images.

---

## 4. Capacity math (back of the envelope)

Assume a 60,000-seat show and 500,000 people arriving in the first 10 minutes.

- **Waiting room joins:** 500k / 600 s ≈ 830 joins/s, spread over 10 counter shards ≈ 83 writes/s per item. Far below the per-item limit.
- **Admission rate:** if the average buyer makes 1.5 hold attempts and 1 checkout, admitting 50 people/s gives about 75 hold transactions/s and 50 checkouts/s. A 60k show sells out in roughly 60,000 / (50 x 2.5 tickets) ≈ 8 minutes.
- **Hold transactions:** 75/s x (3 seats + 1 hold item) ≈ 300 item writes/s, spread across about 150 section partitions. Trivial for on-demand DynamoDB; the same design at 50x the rate is still within per-partition limits.
- **Checkout:** 50 messages/s; the worker runs at up to 50 concurrent invocations, each about 100 ms, so it can absorb about 500/s before the queue backs up. The backlog alarm fires at 60 s of age.
- **Browse:** the 2-second edge cache bounds origin reads per hot URL to (number of POPs) / 2 s, independent of crowd size.

The waiting-room rate is the single knob that keeps every downstream number in its comfort zone.

---

## 5. Failure modes

| Failure | What happens | Recovery |
|---|---|---|
| One AZ down | The ALB routes to pods in the other AZ (pods are spread across zones); the HPA and node group add capacity there. DynamoDB, SQS and Lambda are regional. If it is the NAT gateway's AZ, nodes can't pull new images until it recovers. | Automatic. RDS Multi-AZ fails over if `db_multi_az = true`. |
| API pod dies mid-hold | The transaction either committed or didn't; there is no partial state. | The client retries; the readiness probe and target group health check have already removed the pod. |
| Node lost or drained | The PodDisruptionBudget keeps at least one API pod serving; pods reschedule on the other node. | Automatic; the managed node group replaces the node. |
| Argo CD down | Nothing running changes: Argo CD only applies changes. Deploys pause. | It is a Deployment; Kubernetes restarts it. |
| Prometheus or Loki down | Dashboards and in-cluster alerts go blank; the shop is unaffected. CloudWatch alarms still page. | Kubernetes restarts the pod; data is on its volume (Prometheus) or in S3 (Loki). |
| Checkout worker failing | Messages stay in SQS; holds stay locked for the 5-min checkout grace. After 3 attempts the message goes to the DLQ (alarmed). | Fix and redrive. If the grace expires first, the sweeper returns the seats and the worker later records `HOLD_EXPIRED`. |
| Payment declined | The worker releases the hold (seats available, GA counter refunded) in one transaction and records `PAYMENT_DECLINED`. | The buyer re-picks. |
| SES rejects the email | The order is already confirmed; the failure is logged and alarmed (`EmailFailures`). EventBridge retries other errors, then DLQ. | Resend from the order record. |
| Stream processor lagging | The live map is a few seconds behind; holds are still correct (the condition is authoritative). | `IteratorAge` alarm; scale parallelization factor. |
| Duplicate delivery | Confirm transaction is conditional on the order not existing. | None needed. |
| Bot swarm on holds | WAF rate rule on `/api/holds` blocks per IP; the waiting room bounds total admission. | Tune limits; add Bot Control / CAPTCHA for real deployments. |
| RDS unavailable | Catalogue reads served from edge cache for up to 30 s; new holds fail (price lookup). | Multi-AZ, or cache prices in DynamoDB (a "next step" below). |

---

## 6. Security model

- **Network.** Only CloudFront can reach the ALB (managed prefix list plus a secret origin header). Pods accept the API port only from the ALB; RDS accepts Postgres only from the cluster. Nodes reach the internet outbound through one NAT gateway; the database subnets have no internet route. The EKS API endpoint is public so the pipeline can reach it, and every call is IAM-authenticated.
- **Identity.** Each Lambda has its own role scoped to specific tables, indexes, queues, the bus and the WebSocket management API. Each pod gets its own role through EKS Pod Identity (ADR-16); the API cannot change seat status outside the hold/checkout rules encoded in its code, cannot delete anything, and cannot touch the analytics lake. Nodes block pods from the instance metadata service.
- **Secrets.** RDS master password generated and rotated by RDS in Secrets Manager. The HMAC signing key is a separate secret. Nothing secret is in Terraform outputs or user data.
- **Edge.** WAF managed rules, rate limits, HSTS, CSP (script-src 'self'), frame-ancestors none, nosniff.
- **Data.** All tables, queues and buckets encrypted at rest; point-in-time recovery on orders; no PII on the event bus or in the lake.
- **Access.** People assume MFA-protected team roles; EKS access entries map those roles to cluster permissions, and control-plane audit logs go to CloudWatch. Grafana and Argo CD have no public endpoint (port-forward only). No SSH keys, no bastion: Session Manager onto nodes; session starts are recorded in CloudTrail event history (90 days by default); full session logging to S3/CloudWatch is a next step.
- **Containers.** The API runs as a non-root user with a read-only root filesystem, no privilege escalation and all capabilities dropped. ECR scans every image on push, and tags are immutable.

---

## 7. Cost

On-demand, `ap-south-1`, idle-to-light demo traffic. Check the AWS Pricing Calculator for your region.

| Item | Approx. per day |
|---|---|
| 2 x EKS nodes, t3.large | $4.30 |
| EKS control plane | $2.40 |
| NAT gateway (plus a few cents of data) | $1.35 |
| ALB | $0.60 |
| RDS db.t4g.micro single-AZ | $0.50 |
| WAF (web ACL + 5 rules, prorated) | $0.35 |
| Public IPv4 addresses (ALB, NAT) | $0.35 |
| EBS gp3: node disks, Prometheus and Loki volumes (~90 GB) | $0.27 |
| DynamoDB storage + seeding (one-off ~$0.20) | $0.05 |
| Lambda, SQS, EventBridge, API Gateway WS, Firehose, S3, CloudFront, Athena, ECR, control-plane logs | cents |
| **Total** | **≈ $10–11/day** |

The EC2 design cost ≈ $5–6/day: the cluster adds the control plane, bigger nodes for the observability stack, and the NAT gateway, and drops the interface endpoints. Deploy, demo, record, then run the Destroy workflow. ECR images and team IAM (`terraform/shared`) cost a few cents a month and are kept.

---

## 8. What changes at Ticketmaster or BookMyShow scale

- **Cell-based architecture.** One cell (this whole stack) per region or per large on-sale, with a thin global router. Blast radius becomes one cell.
- **Multi-region.** DynamoDB Global Tables for orders and catalogue. Seat inventory stays single-region per event (home region), because cross-region conditional writes would weaken the correctness guarantee.
- **Waiting room at the edge.** Move admission checks into CloudFront Functions (verify the HMAC pass at the edge) so non-admitted traffic never reaches the origin.
- **Bot defence.** AWS WAF Bot Control, CAPTCHA / challenge actions on hold, device fingerprinting, per-account purchase limits.
- **Payments.** Real provider (Razorpay / Stripe) with webhooks; a Step Functions saga for authorise, confirm and capture with compensation.
- **Read scaling.** DAX or ElastiCache in front of seat grids for very hot sections; RDS read replicas or caching prices in DynamoDB to drop RDS from the hold path.
- **Observability.** Amazon Managed Prometheus and Grafana, or Thanos and Loki in simple-scalable mode, instead of single replicas; OpenTelemetry traces from pods joined to the Lambdas' X-Ray traces; CloudWatch RUM for real-user page speed.
- **Compute.** Karpenter for node autoscaling (Spot for stateless pods), one NAT gateway per AZ, and the cluster split into cells.

## 9. Next steps (good follow-up commits)

1. HTTPS between CloudFront and the ALB (ACM certificate on the ALB with a custom domain, `https-only` origin policy).
2. Cognito sign-in and an order-history page.
3. A Step Functions checkout saga with a real payment sandbox.
4. Static analysis in the pipeline: `tflint` and `checkov` on every pull request, and actions pinned to commit SHAs.
5. QR-code tickets generated in the notifier and stored in S3 behind signed URLs.
6. Full Session Manager session logging to S3/CloudWatch. Today, session starts are recorded in CloudTrail event history (90 days by default).
7. Kubernetes NetworkPolicies (enable network policy in the VPC CNI add-on) so only the ALB path and Prometheus can reach API pods.
8. Alertmanager routing to SNS or Slack; today in-cluster alerts show in Grafana and Alertmanager only, while CloudWatch alarms page through SNS.
9. Node autoscaling with Karpenter, and Argo CD sign-in through the team's identity provider instead of the initial admin password.
