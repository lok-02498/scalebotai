# Jenkins setup for ScaleBotAI

This folder contains everything needed to run the ScaleBotAI CI/CD pipeline:

| File | Purpose |
|---|---|
| `Jenkinsfile` | Declarative pipeline: Checkout, Build, Unit Test, Lint, Docker Build, Security Scan, Docker Push, Kubernetes Deploy, Smoke Test, Rollback |
| `Dockerfile` | Jenkins controller image (`jenkins/jenkins:lts-jdk17`) with python3.12, docker CLI + buildx, kubectl, trivy, curl, jq and the plugins in `plugins.txt` |
| `docker-compose.jenkins.yml` | Runs that image with a persistent `jenkins_home` and the host Docker socket |
| `plugins.txt` | Plugin IDs installed by `jenkins-plugin-cli` at image build time |

The pipeline design (stages, quality gates, rollback, DORA metrics) is explained in
[`docs/08-cicd-jenkins.md`](../docs/08-cicd-jenkins.md). This file covers the setup only.

---

## 1. Prerequisites

| Tool | Why |
|---|---|
| Docker Engine / Docker Desktop | runs Jenkins and builds images |
| minikube + kubectl | target cluster (`scripts/bootstrap-minikube.sh`) |
| GitHub repository | source + webhooks + branch protection |
| Docker Hub account | image registry (`<DOCKERHUB_USER>/scalebotai-api`) |

---

## 2. Run Jenkins in Docker

```bash
# From the repository root
# Linux hosts only: let the jenkins user use the Docker socket
export DOCKER_GID=$(stat -c '%g' /var/run/docker.sock)      # Docker Desktop: leave unset (0)
export MINIKUBE_IP=$(minikube ip)                            # default 192.168.49.2

docker compose -f jenkins/docker-compose.jenkins.yml up -d --build
docker logs -f scalebotai-jenkins                            # wait for "Jenkins is fully up and running"
docker exec scalebotai-jenkins cat /var/jenkins_home/secrets/initialAdminPassword
```

Open <http://localhost:8080>, paste the initial password, choose **Select plugins to install → None**
(the image already contains every plugin from `plugins.txt`), and create the admin user.

Check the toolchain inside the container:

```bash
docker exec scalebotai-jenkins bash -c 'python3.12 --version && docker version && kubectl version --client && trivy --version'
```

### Reaching minikube from the Jenkins container

With the minikube **docker driver**, the cluster runs in a container on the `minikube` Docker network.
Attach Jenkins to it so `kubectl` (API server `https://<minikube-ip>:8443`) and the smoke test
(`https://api.scalebotai.local` → Ingress) work:

```bash
docker network connect minikube scalebotai-jenkins
docker exec scalebotai-jenkins curl -sk https://api.scalebotai.local/health
```

`docker-compose.jenkins.yml` maps `api.scalebotai.local` to `MINIKUBE_IP` through `extra_hosts`.

### Security trade-off: mounting `/var/run/docker.sock`

Mounting the host socket lets pipelines build and scan images with the host daemon (fast, shared
layer cache, no privileged Docker-in-Docker). The cost: **anyone who can run or edit a pipeline
controls the host Docker daemon, which is equivalent to root on the host.** This is acceptable for a
single-user lab only. For production:

- set the controller's executors to **0** and run builds on dedicated, ephemeral agents
  (VMs or Kubernetes pods via the `kubernetes` plugin) - change `agent any` to `agent { label '...' }`;
- build images without a privileged daemon (rootless BuildKit, Kaniko, or TLS-protected DinD);
- restrict who can create jobs / modify `Jenkinsfile` (matrix-auth, branch protection, PR review);
- never let pull requests from forks run with credentials (see section 6).

---

## 3. Plugins

Installed from `plugins.txt` (dependencies resolved automatically):

| Plugin ID | Used for |
|---|---|
| `workflow-aggregator`, `pipeline-model-definition` | Pipeline + Declarative syntax |
| `pipeline-stage-view`, `pipeline-graph-view` | stage visualisation (Blue Ocean optional, commented out) |
| `pipeline-utility-steps` | helper steps (readJSON, etc.) |
| `git`, `github`, `github-branch-source`, `branch-api` | Multibranch Pipeline from GitHub, webhooks, commit statuses |
| `credentials`, `credentials-binding`, `plain-credentials`, `ssh-credentials` | `withCredentials` for Docker Hub and kubeconfig |
| `docker-workflow` | Docker pipeline integration |
| `kubernetes-cli` | `withKubeConfig` (optional alternative to the secret-file binding) |
| `junit`, `coverage` | test results and Cobertura coverage (`recordCoverage`) |
| `timestamper`, `ws-cleanup`, `build-timeout`, `ansicolor` | `timestamps()`, `cleanWs()`, timeouts, colour output |
| `email-ext`, `mailer`, `slack` | notifications (placeholders in `post {}`) |
| `matrix-auth`, `configuration-as-code` | authorisation, optional JCasC |

To add a plugin later: append it to `plugins.txt` and run
`docker compose -f jenkins/docker-compose.jenkins.yml up -d --build`.

---

## 4. Credentials

**Manage Jenkins → Credentials → System → Global credentials → Add Credentials**

| ID (exact) | Kind | Content |
|---|---|---|
| `dockerhub-credentials` | Username with password | Docker Hub username + **access token** (not the account password) |
| `kubeconfig-scalebotai` | Secret file | kubeconfig for ServiceAccount `jenkins-deployer` (section 5) |
| `github-credentials` | Username with password | GitHub username + fine-grained PAT, used by the Multibranch branch source (or use a GitHub App) |

Docker Hub token: **Docker Hub → Account settings → Personal access tokens → Generate**, scope
*Read & Write*. GitHub PAT (fine-grained, this repository only): *Contents: read*, *Metadata: read*,
*Commit statuses: read & write*, *Pull requests: read*, *Webhooks: read & write* (only if Jenkins should
manage the webhook itself).

---

## 5. kubeconfig for the `jenkins-deployer` ServiceAccount

`k8s/base/11-rbac.yaml` creates ServiceAccount `jenkins-deployer` with a namespaced Role/RoleBinding in
`scalebotai`. Jenkins never uses your admin kubeconfig. Run in Git Bash / a Linux shell with your
admin context pointing at the cluster:

```bash
kubectl apply -k k8s/base                                     # creates SA + Role + RoleBinding

# 1. Token for the ServiceAccount (1 year; the API server may cap the duration)
TOKEN=$(kubectl -n scalebotai create token jenkins-deployer --duration=8760h)
#    If 11-rbac.yaml also defines a long-lived token Secret for the SA, you can use it instead:
#    TOKEN=$(kubectl -n scalebotai get secret jenkins-deployer-token -o jsonpath='{.data.token}' | base64 -d)

# 2. API server address reachable FROM the Jenkins container, and the cluster CA
SERVER="https://$(minikube ip):8443"
CA_DATA=$(kubectl config view --raw --flatten --minify \
          -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

# 3. Write a minimal kubeconfig
cat > kubeconfig-scalebotai.yaml <<EOF
apiVersion: v1
kind: Config
clusters:
- name: scalebotai
  cluster:
    server: ${SERVER}
    certificate-authority-data: ${CA_DATA}
users:
- name: jenkins-deployer
  user:
    token: ${TOKEN}
contexts:
- name: jenkins-deployer@scalebotai
  context:
    cluster: scalebotai
    user: jenkins-deployer
    namespace: scalebotai
current-context: jenkins-deployer@scalebotai
EOF

# 4. Verify least privilege
export KUBECONFIG=$PWD/kubeconfig-scalebotai.yaml
kubectl auth can-i patch deployments -n scalebotai          # yes
kubectl auth can-i create pods/portforward -n scalebotai    # yes
kubectl auth can-i get secrets -n scalebotai                # no (expected)
kubectl auth can-i delete namespaces                        # no (expected)
unset KUBECONFIG
```

Upload `kubeconfig-scalebotai.yaml` as the **Secret file** credential `kubeconfig-scalebotai`, then delete
the local file (`rm kubeconfig-scalebotai.yaml`). Rotate the token before it expires.

Permissions the deploy/rollback scripts need in namespace `scalebotai` (what the Role must grant):

| API group | Resources | Verbs | Used by |
|---|---|---|---|
| `apps` | deployments | get, list, watch, create, update, patch, delete | set image, rollout status/undo, scale, canary apply/delete |
| `apps` | replicasets | get, list, watch | rollout status / history / undo |
| `""` | pods, pods/log | get, list, watch | diagnostics, canary restart checks |
| `""` | pods/portforward | create | in-cluster smoke tests (canary, blue-green preview) |
| `""` | services | get, list, create, update, patch | blue-green selector switch |
| `""` / `discovery.k8s.io` | endpoints / endpointslices | get, list | blue-green endpoint verification |
| `""` | events | list | rollout failure diagnostics |

---

## 6. Multibranch Pipeline job

1. **New Item** → name `scalebotai` → **Multibranch Pipeline** → OK.
2. **Branch Sources → Add source → GitHub**
   - Credentials: `github-credentials`
   - Repository HTTPS URL: `https://github.com/<you>/scalebotai`
   - Behaviours:
     - *Discover branches*: All branches
     - *Discover pull requests from origin*: Merging the pull request with the current target branch revision
     - *Discover pull requests from forks*: Trust → **Nobody** (fork PRs must not run with credentials)
3. **Build Configuration** → Mode *by Jenkinsfile* → **Script Path: `jenkins/Jenkinsfile`**
4. **Scan Multibranch Pipeline Triggers** → *Periodically if not otherwise run*: 1 day (safety net if a webhook is missed)
5. **Orphaned Item Strategy** → discard old items: 7 days, max 20.
6. Save. Jenkins scans the repository and creates one job per branch / PR.

The first build of each branch registers the `parameters {}` (DOCKERHUB_USER, DEPLOY_STRATEGY,
SKIP_DEPLOY); afterwards **Build with Parameters** is available. Push builds use the defaults.

| Parameter | Default | Meaning |
|---|---|---|
| `DOCKERHUB_USER` | `scalebotai` | Docker Hub namespace of `<user>/scalebotai-api` |
| `DEPLOY_STRATEGY` | `rolling` | `rolling`, `canary` or `blue-green` (passed to `scripts/deploy.sh`) |
| `SKIP_DEPLOY` | `false` | build + push without deploying |

Optional global variables (**Manage Jenkins → System → Global properties → Environment variables**)
picked up by `scripts/deploy.sh`: `PROMETHEUS_URL` (enables the canary error-rate / p95 gate, e.g.
`http://prometheus.monitoring.svc:9090` when reachable), `CANARY_BAKE_SECONDS`, `ROLLOUT_TIMEOUT`.

---

## 7. GitHub webhook

**GitHub repo → Settings → Webhooks → Add webhook**

| Field | Value |
|---|---|
| Payload URL | `http://<jenkins-host>:8080/github-webhook/` (trailing slash required) |
| Content type | `application/json` |
| Events | *Just the push event* + *Pull requests* (or "Send me everything") |
| Active | yes |

GitHub must be able to reach Jenkins. For a laptop setup expose it with a tunnel
(`ngrok http 8080` or a <https://smee.io> channel + `smee -u <channel> -t http://localhost:8080/github-webhook/`).
Check **Recent Deliveries** for HTTP 200. Alternatively let Jenkins manage hooks:
**Manage Jenkins → System → GitHub → Add GitHub Server** (credential with a PAT as *Secret text*) → *Manage hooks*.

---

## 8. Branch protection (GitHub → Settings → Branches → Add rule)

**`main`**

- Require a pull request before merging; **1 approval**; dismiss stale approvals on new commits
- Require review from Code Owners (`.github/CODEOWNERS`)
- **Require status checks to pass**: `continuous-integration/jenkins/pr-merge`
  (and `continuous-integration/jenkins/branch` for direct branch builds); require branches to be up to date
- Require conversation resolution
- Require linear history (squash or rebase merges)
- Do not allow force pushes or deletions; include administrators

**`develop`**: require pull request + the same Jenkins status check; force pushes disabled.

The status check names appear in the list only after Jenkins has reported at least one build for a PR.

---

## 9. Docker Hub repository

1. Docker Hub → **Create repository** → name `scalebotai-api` under your namespace
   (`<DOCKERHUB_USER>/scalebotai-api`), visibility *Public* (minikube pulls without an imagePullSecret).
2. Create the access token (section 4) and store it as `dockerhub-credentials`.
3. If your namespace is not `scalebotai`, change the `DOCKERHUB_USER` parameter default (or set it per build)
   **and** the image reference in `k8s/base` so the cluster pulls from the same repository.

Tags pushed by the pipeline: `<BUILD_NUMBER>-<git short sha>` (immutable, every main/develop build)
and `latest` (main only, convenience - deployments always use the immutable tag).

---

## 10. Verification checklist

- [ ] `docker exec scalebotai-jenkins docker ps` works (socket permissions OK)
- [ ] `docker exec scalebotai-jenkins kubectl --kubeconfig /dev/null version --client` prints the pinned version
- [ ] A feature-branch push runs Checkout → Security Scan; Docker Push / Deploy are *skipped*
- [ ] A `develop` push also pushes `<user>/scalebotai-api:<N>-<sha>` to Docker Hub
- [ ] A `main` merge deploys, passes the smoke test, and the Rollback stage shows as *skipped*
- [ ] A deliberately broken image (e.g. failing readiness probe) on `main` triggers the Rollback stage
- [ ] The PR on GitHub shows the Jenkins status check and blocks merging while it is red

## 11. Troubleshooting (setup)

| Symptom | Fix |
|---|---|
| `permission denied ... /var/run/docker.sock` | set `DOCKER_GID` to the socket's group id and recreate the container |
| `ERROR: BuildKit is enabled but the buildx component is missing` | rebuild the image (installs `docker-buildx-plugin`) |
| `python3.12 not found on this agent` | builds are running on an agent without the toolchain - use this image or install python3.12 |
| `kubectl ... dial tcp 192.168.49.2:8443: i/o timeout` | `docker network connect minikube scalebotai-jenkins` |
| `x509: certificate signed by unknown authority` | regenerate the kubeconfig with `--raw --flatten` so the CA data is embedded |
| `Forbidden: ... cannot patch resource "deployments"` | the Role in `k8s/base/11-rbac.yaml` lacks a verb from section 5 |
| Webhook delivery 403/404 | URL must end with `/github-webhook/`; Jenkins must be reachable from GitHub |
| `/usr/bin/env: 'bash\r'` | scripts committed with CRLF - add `*.sh text eol=lf` to `.gitattributes`, re-checkout |
