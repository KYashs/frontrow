# Deploying FrontRow

This guide takes you from an empty AWS account to a live FrontRow site, then back to zero. After a one-time setup, deploys run from GitHub Actions and Argo CD, never from your laptop.

| | |
|---|---|
| **Region** | `us-east-1` (N. Virginia), the cheapest |
| **First deploy** | About 30–40 minutes, of which EKS takes about 10 and RDS about 10 |
| **Later deploys** | A few minutes. Only what changed is applied. |
| **Cost while running** | About $10 per day ([breakdown](ARCHITECTURE.md#7-cost)). Destroy the stack when you're done. |
| **Cost when destroyed** | A few cents a month for the state bucket, ECR images and IAM |

## How it fits together

```
your laptop ──(once)──► terraform/bootstrap   state bucket + GitHub OIDC roles
                                │
git push main ──► GitHub Actions ──► terraform/shared   ECR, team IAM
                                 ├─► docker build/push  API image (only if changed)
                                 └─► terraform/         VPC, EKS, RDS, DynamoDB, ALB, Lambdas…
                                                         └─► installs Argo CD
                                     Argo CD ◄── watches deploy/ on main
                                        └─► LB controller, Prometheus, Grafana, Loki, API pods
```

Terraform owns AWS. Argo CD owns everything inside the cluster. The `main` branch is the source of truth for both.

---

## 1. Prerequisites

### Accounts

- **An AWS account.** A new account on the AWS **Free plan** works with this repo's settings. See [Free plan limits](#free-plan-limits).
- **A GitHub account** and a repository holding this code: a fork, or a new repository you push to. A **public** repository is simplest, because Argo CD then reads `deploy/` with no credentials.

### AWS account setup

Do this once, in the AWS console:

1. **Secure the root user.** Turn on MFA, then stop using the root user.
2. **Create an admin IAM user** for yourself (for example `yourname-admin`) with `AdministratorAccess` and MFA. Create an access key for it for the CLI.
3. **Set a budget.** Under *Billing > Budgets*, create a monthly cost budget (for example $25) with an email alert at 80%.
4. **Configure the CLI** and check that it works:

   ```bash
   aws configure            # access key, secret, region us-east-1, output json
   aws sts get-caller-identity
   ```

### Tools on your machine

| Tool | Version | Needed for |
|---|---|---|
| [git](https://git-scm.com/) | any recent | Pushing the code |
| [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | v2 | Bootstrap, kubeconfig, checks |
| [Terraform](https://developer.hashicorp.com/terraform/install) | **1.10 or later** (CI uses 1.17.0) | The one-time bootstrap |
| [GitHub CLI](https://cli.github.com/) (`gh`) | any recent | Reading the OIDC subject, setting variables |
| [kubectl](https://kubernetes.io/docs/tasks/tools/) | within one minor version of EKS 1.37 | Opening Grafana and Argo CD |
| Python | 3.12 or later (CI uses 3.14) | Running tests and demo scripts. Optional. |
| Docker, Helm | any recent | Local image builds and `scripts/check-deploy.sh` only. Optional; CI does both. |

Sign the GitHub CLI in with the `workflow` scope, because pushing files under `.github/workflows/` needs it:

```bash
gh auth login --web --scopes workflow
gh auth setup-git
```

### An email address for SES

FrontRow emails tickets through Amazon SES. Pick an address you can open, for example your own. It is used as `SENDER_EMAIL`, and you'll click a verification link sent to it during the first deploy.

---

## 2. Get the code into your repository

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
```

Then edit [terraform/environments/prod.tfvars](../terraform/environments/prod.tfvars):

- **`cluster_admin_arns`:** replace the ARN with your own admin user's ARN, from `aws sts get-caller-identity --query Arn --output text`. This lets your laptop's `kubectl` reach the cluster. Without it, only the pipeline can.
- **`cloudfront_enabled`:** keep it `false` until AWS has verified your account for CloudFront. See [Free plan limits](#free-plan-limits).
- **`analytics_enabled`:** keep it `false` on the Free plan.

Leave the rest as it is for a first deploy. Commit and push these edits to `main`. The deploy won't start yet, because it skips until the repository variables exist.

---

## 3. Bootstrap: the only step run from your laptop

This creates the S3 bucket for Terraform state and two IAM roles that GitHub Actions can assume over OIDC: a read-only **plan** role for pull requests and a **deploy** role for `main`. No AWS keys are ever stored in GitHub.

```bash
cd terraform/bootstrap
terraform init
terraform apply \
  -var github_repository=<owner>/<repo> \
  -var github_subject_prefix="$(gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix)"
```

- **`github_subject_prefix`:** newer GitHub repositories identify themselves to AWS by immutable IDs (`repo:owner@123/name@456`). The roles must trust that exact prefix, or every workflow fails with *Not authorized to perform sts:AssumeRoleWithWebIdentity*. If the `gh api` call prints nothing, your repository uses the classic form, and an empty value is fine.
- **An existing OIDC provider:** if your account already trusts `token.actions.githubusercontent.com`, add `-var create_oidc_provider=false`.
- **The local state file:** keep `terraform/bootstrap/terraform.tfstate`. It's gitignored, and it's the only record of these resources.

The apply prints `github_variables`. You need them in the next step.

---

## 4. Configure the GitHub repository

On GitHub, open *Settings*.

**Variables** (*Secrets and variables > Actions > Variables*). Add the four values from `github_variables`:

| Name | Example |
|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::<account>:role/frontrow-github-deploy` |
| `AWS_PLAN_ROLE_ARN` | `arn:aws:iam::<account>:role/frontrow-github-plan` |
| `TF_STATE_BUCKET` | `frontrow-tfstate-<account>-us-east-1` |
| `TF_STATE_REGION` | `us-east-1` |

You can also add them from the terminal:

```bash
terraform output -json github_variables | python3 -c 'import json,sys; [print(k, v) for k, v in json.load(sys.stdin).items()]' |
  while read -r k v; do gh variable set "$k" --body "$v" --repo <owner>/<repo>; done
```

**Secrets** (*Secrets and variables > Actions > Secrets*). Secrets stay masked in public logs.

| Name | Required | Used for |
|---|---|---|
| `SENDER_EMAIL` | Yes | The SES address tickets are sent from |
| `ALERT_EMAIL` | No | CloudWatch alarm notifications through SNS |

```bash
gh secret set SENDER_EMAIL --repo <owner>/<repo>     # paste when prompted
gh secret set ALERT_EMAIL  --repo <owner>/<repo>
```

**Environment** (*Environments > New environment*): name it `production`.

- Under *Deployment branches and tags*, choose **Selected branches** and add `main`. This is required: the deploy role only trusts jobs running in this environment from `main`.
- Optional: add yourself as a **required reviewer** to approve each deploy by hand.

**Branch protection** (*Branches*), recommended: on `main`, require a pull request and the *Test* check.

---

## 5. Deploy

Push a commit to `main`, or open *Actions > Deploy > Run workflow*.

The Deploy workflow runs:

1. **Tests.** Unit and end-to-end tests against mocked AWS, the reproducible Lambda build, manifest validation, and Terraform format and validate.
2. **Shared resources.** `terraform/shared` creates the ECR repository and team IAM.
3. **The API image.** It is built and pushed to ECR, unless an image with the same content-hash tag is already there.
4. **The stack.** `terraform/` creates the VPC, NAT, EKS, RDS, DynamoDB, SQS, Lambdas, API Gateway WebSocket, ALB, SES identity and alarms. It then installs Argo CD and seeds every seat into DynamoDB.
5. **Outputs.** The run summary prints the site URL and the `kubectl` commands.

Argo CD then syncs `deploy/` from `main`. It installs the AWS Load Balancer Controller, the metrics server, Prometheus, Grafana, Loki and Alloy, then the API pods. This takes another 5–10 minutes after the workflow finishes.

### Verify it

1. **Confirm SES.** Click the verification link SES sent to `SENDER_EMAIL`. If you set `ALERT_EMAIL`, confirm the SNS subscription email too. While your account is in the SES sandbox, tickets can only go to verified addresses, so buy test tickets with the sender address.
2. **Check the site.** Use the URL from the run summary, `http://<alb-dns-name>` when CloudFront is off:

   ```bash
   export URL=http://<alb-dns-name>
   curl -s -o /dev/null -w '%{http_code}\n' "$URL/"                 # 200
   curl -s "$URL/api/health/deep"                                   # every dependency "ok"
   curl -s -o /dev/null -w '%{http_code}\n' "$URL/metrics"          # 404: hidden from the internet
   ```

   A `503` in the first few minutes means Argo CD hasn't finished rolling out the API yet.

3. **Open Argo CD and Grafana.** Neither has a public endpoint, so reach them through a port-forward:

   ```bash
   aws eks update-kubeconfig --region us-east-1 --name frontrow

   # Argo CD: http://localhost:8080, user admin
   kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
   kubectl -n argocd port-forward svc/argocd-server 8080:80

   # Grafana: http://localhost:3000, user admin
   kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d; echo
   kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80
   ```

   Every Application in Argo CD should be **Synced** and **Healthy**.

4. **Run the race test.** 100 buyers try to hold the same seat, and exactly one should win:

   ```bash
   python3 scripts/race_test.py --url "$URL" --event kendrick-lamar-metlife-2027 --section 113 --seat "R10#S07" --buyers 100
   ```

   Expect `PASS`, with one `201` and 99 `409`s. The [README's demo section](../README.md#demo) covers the waiting room, the load test and the AWS path drawer.

---

## 6. Make changes

Open a pull request for every change. Pull requests run the tests and post the Terraform plans as a comment, using the read-only role. Merging to `main` applies the change.

| To change | Edit |
|---|---|
| Node size, WAF limits, hold time, feature switches | [terraform/environments/prod.tfvars](../terraform/environments/prod.tfvars) |
| AWS resources | `terraform/*.tf` |
| Anything in the cluster, including chart versions and replicas | [deploy/](../deploy/) (Argo CD syncs it) |
| API code | `app/`, `core/` (a new image tag rolls out on merge) |
| Team members | [terraform/environments/shared.tfvars](../terraform/environments/shared.tfvars) |

A daily **Drift check** workflow compares AWS with `main` and opens an issue if someone changed AWS by hand.

---

## 7. Tear it down

Run *Actions > Destroy > Run workflow*, and type `destroy frontrow` to confirm. It takes about 15–20 minutes and runs in this order:

1. It deletes the Argo CD Applications, so the API's targets are deregistered from the ALB.
2. It deletes every PersistentVolumeClaim, then waits until the EBS disks for Prometheus and Loki are gone.
3. It runs `terraform destroy` on the stack.
4. As a safety net, it deletes any detached disk still tagged for the cluster.

Check that nothing billable is left:

```bash
aws eks list-clusters --region us-east-1
aws ec2 describe-nat-gateways --region us-east-1 --filter Name=state,Values=available --query 'NatGateways[].NatGatewayId'
aws ec2 describe-volumes --region us-east-1 --filters Name=tag:kubernetes.io/cluster/frontrow,Values=owned --query 'Volumes[].VolumeId'
aws elbv2 describe-load-balancers --region us-east-1 --query 'LoadBalancers[].LoadBalancerName'
aws rds describe-db-instances --region us-east-1 --query 'DBInstances[].DBInstanceIdentifier'
```

All five should return empty lists.

**What's kept, on purpose:** the state bucket, the ECR repository and its images, team IAM, and the GitHub OIDC roles. Together they cost a few cents a month, and they make the next deploy a single push. Re-run *Deploy* to bring the stack back.

**Removing everything** is optional, and only if you're done with the project for good. Do it after the Destroy workflow, with your admin credentials:

```bash
# 1. ECR and team IAM. The repository refuses to delete while it holds images, so empty it first.
aws ecr batch-delete-image --region us-east-1 --repository-name frontrow-api \
  --image-ids "$(aws ecr list-images --region us-east-1 --repository-name frontrow-api --query imageIds --output json)"
cd terraform/shared
terraform init -backend-config="bucket=<TF_STATE_BUCKET>" -backend-config="region=us-east-1"
terraform destroy -var-file=../environments/shared.tfvars

# 2. Bootstrap. The state bucket has prevent_destroy, so delete the GitHub roles and the
#    OIDC provider with Terraform, then empty and delete the versioned bucket in the S3 console.
cd ../bootstrap
terraform state rm aws_s3_bucket.state
terraform destroy -var github_repository=<owner>/<repo>
```

---

## Free plan limits

New AWS accounts start on the Free plan, which rejects some resources. This repository is already set up to work within it:

| Limit | Symptom | Setting |
|---|---|---|
| Only some EC2 types allowed | The node group fails: *instance type is not eligible for Free Tier* | `node_instance_type = "m7i-flex.large"` (2 vCPU, 8 GiB) |
| No Kinesis Firehose | `SubscriptionRequiredException` | `analytics_enabled = false` turns off the order data lake (Firehose, S3 lake, Athena) |
| CloudFront needs account verification | *Your account must be verified before you can add new CloudFront resources* | `cloudfront_enabled = false` |

With `cloudfront_enabled = false`:

- The ALB is public over plain HTTP.
- The API pods also serve the static site and `/config.json`.
- The ALB returns 404 for `/metrics`.
- WAF and edge caching are off, because the WAF is CloudFront-scoped.

The site URL becomes `http://<alb-dns-name>`. See [ADR-17](ARCHITECTURE.md#adr-17-a-switch-to-run-without-cloudfront).

**To turn the features back on:**

- **CloudFront:** open an AWS Support case asking to enable CloudFront. Once it's approved, set `cloudfront_enabled = true` in a pull request.
- **Analytics:** set `analytics_enabled = true` after upgrading to a paid plan.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| *Not authorized to perform sts:AssumeRoleWithWebIdentity* | The roles trust a different OIDC subject than GitHub sends | Re-run the bootstrap apply with `github_subject_prefix` from the `gh api` command in step 3 |
| Deploy job is skipped | Repository variables aren't set | Add the four variables from step 4 |
| *Deployment branch is not allowed* | The `production` environment doesn't allow `main` | Add `main` under the environment's deployment branches |
| `git push` rejected for workflow files | The GitHub token lacks the `workflow` scope | Run `gh auth login --web --scopes workflow` |
| `aws_eks_cluster.main: Still creating... [10m]` | Normal. EKS takes 10–12 minutes. | Wait. The job has a 120-minute timeout. |
| Site returns 503 for more than 15 minutes, and the Argo CD API app shows *x509: certificate signed by unknown authority* on the TargetGroupBinding | The load balancer controller's webhook certificate and its caBundle were rendered at different times | `ignoreDifferences` in `deploy/root` prevents this. To recover once: delete the `aws-load-balancer-tls` Secret in `kube-system` and both `aws-load-balancer-webhook` webhook configurations, sync the app in Argo CD, then run `kubectl -n kube-system rollout restart deploy/aws-load-balancer-controller` |
| `kubectl`: *the server has asked for the client to provide credentials* | Your IAM user has no EKS access entry | Add your ARN to `cluster_admin_arns` and merge |
| Race test shows `503 Busy, please retry` | DynamoDB `TransactionConflict` under contention. It's safe, and no seat is double-sold. | Clients retry. `race_test.py` already retries with backoff. |
| No ticket email arrives | SES sandbox, or the sender isn't verified | Click the SES verification link, and send to a verified address |
| EBS volumes left after destroy | Disks were still being deleted when the CSI driver was removed | The Destroy workflow now waits and cleans up. Delete any leftover `available` volume with `aws ec2 delete-volume`. |
