# Canary deployment

A **canary** release sends a small share of real production traffic to the new version. You measure that share
against the stable version and then promote it to 100% or abort it. Problems hit a few percent of users instead
of everyone.

Full explanation, diagrams and comparison: [`docs/12-deployment-strategies.md`](../../../docs/12-deployment-strategies.md).

## Files

| File | Purpose |
|---|---|
| `deployment-canary.yaml` | Deployment `scalebotai-api-canary`, 1 replica, labels `app: scalebotai-api`, `track: canary` |
| `service-canary.yaml` | Services `scalebotai-api-canary` (selects `track: canary`) and `scalebotai-api-stable` (selects `track: stable`). Variant B only |
| `ingress-canary.yaml` | ingress-nginx canary Ingress `scalebotai-ingress-canary`: `canary-weight: "10"` plus header `X-Canary: always`. Variant B only |
| `set-weight.sh <0-100>` | Variant B: applies the services, points the primary Ingress at the stable-only Service, sets the exact weight |
| `promote.sh [image]` | rolls the canary image out to `deployment/scalebotai-api`, then removes all canary objects |
| `abort.sh` | stops canary traffic, deletes the canary, restores the primary Ingress |
| `lib.sh` | shared helpers, sourced by the three scripts |

## Two ways to split traffic

**Variant A: replica ratio (default, used by Jenkins).** The canary pods carry `app: scalebotai-api`, so the
normal Service `scalebotai-api` load-balances across them too:

| stable pods | canary pods | canary share |
|---|---|---|
| 3 | 1 | 25% |
| 9 | 1 | 10% |
| 3 | 3 | 50% |

This is simple and needs no ingress features. It is coarse-grained, though, and when the HPA scales the stable
pods the canary's share changes.

**Variant B: ingress-nginx weights (exact percentages).** `set-weight.sh` routes exactly *w*% to the canary. While
it runs, the primary Ingress points at `scalebotai-api-stable`, because `scalebotai-api` also contains the canary
pod. Without that change the canary would receive w + (1 − w) × 1/4: at w = 10% that is 32.5%, not 10%.
Testers can always reach the canary with the header `X-Canary: always`.

## Progressive rollout: 10% → 25% → 50% → 100%

```bash
NS=scalebotai
IMAGE=docker.io/scalebotai/scalebotai-api:44-0a1b2c3
cd k8s/strategies/canary

# 0a. Prepare routing FIRST: users -> stable-only Service, canary reachable by header only
./set-weight.sh 0
# 0b. Start the canary with the new image. It gets NO user traffic yet.
kubectl apply -f deployment-canary.yaml
kubectl -n $NS set image deployment/scalebotai-api-canary api=$IMAGE
kubectl -n $NS rollout status deployment/scalebotai-api-canary --timeout=300s
# 0c. Smoke-test the canary only
curl -k -H "X-Canary: always" https://api.scalebotai.local/health

# 1. 10%  -> watch the gates for at least 10 minutes
./set-weight.sh 10
# 2. 25%  -> gates again
./set-weight.sh 25
# 3. 50%  -> gates again (scale the canary up so it can carry the load)
kubectl -n $NS scale deployment/scalebotai-api-canary --replicas=3
./set-weight.sh 50
# 4. 100% -> promote: rolling update of the stable Deployment, then the canary is removed
./promote.sh

# At ANY step, if a gate fails:
./abort.sh
```

Variant A (what `DEPLOY_STRATEGY=canary` in Jenkins does): skip `set-weight.sh`. Apply the canary, set its image
and wait for `rollout status`; it now takes about 25% of traffic. Watch the gates, then run `promote.sh` or
`abort.sh`. For a 50% step, scale the canary to 3 replicas.

## Gates: PromQL checks before every step

Prometheus labels every API series with `track` (from the pod label; see `k8s/monitoring/prometheus-config.yaml`).
Open `kubectl -n monitoring port-forward svc/prometheus 9090:9090` and run:

**1. Canary error rate (must be < 1% and not worse than stable):**
```promql
sum(rate(scalebot_http_requests_total{namespace="scalebotai",track="canary",status=~"5.."}[5m]))
/
sum(rate(scalebot_http_requests_total{namespace="scalebotai",track="canary"}[5m]))
```
Compare with the same query for `track="stable"`. Gate: canary < 0.01 **and** canary ≤ stable × 1.5.

**2. Canary p95 latency (must be < 300 ms and within 20% of stable):**
```promql
histogram_quantile(0.95,
  sum by (le) (rate(scalebot_http_request_duration_seconds_bucket{namespace="scalebotai",track="canary"}[5m])))
```

**3. No restarts or OOM kills of canary pods (needs kube-state-metrics):**
```promql
sum(increase(kube_pod_container_status_restarts_total{namespace="scalebotai",pod=~"scalebotai-api-canary-.*"}[10m]))
```
Gate: `== 0`.

**4. Traffic is actually reaching the canary (sanity check of the split):**
```promql
sum(rate(scalebot_http_requests_total{namespace="scalebotai",track="canary"}[5m]))
/
sum(rate(scalebot_http_requests_total{namespace="scalebotai"}[5m]))
```
Expect about 0.10 / 0.25 / 0.50 for the current step. A value near 0 means the canary isn't being tested, so
**don't** promote.

**5. Business signals:** login failures `rate(scalebot_auth_events_total{event="login_failure"}[5m])` and
chatbot fallbacks `rate(scalebot_chat_messages_total{bot_mode="rule-fallback"}[5m])` should not jump.

Scripted gate (CI-friendly; exits non-zero when the canary error rate is ≥ 1%):

```bash
PROM=http://localhost:9090
Q='sum(rate(scalebot_http_requests_total{track="canary",status=~"5.."}[5m]))/sum(rate(scalebot_http_requests_total{track="canary"}[5m]))'
ERR=$(curl -s "$PROM/api/v1/query" --data-urlencode "query=$Q" | python3 -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "0")')
python3 -c "import sys; sys.exit(0 if float('$ERR') < 0.01 else 1)" && echo "gate passed ($ERR)" || { echo "gate FAILED ($ERR)"; ./abort.sh; exit 1; }
```

Logs for the canary only, in Kibana: `kubernetes.labels.track : "canary" and log_type : "error"`.

## Rollback

`./abort.sh`. The stable Deployment was never changed during the canary, so removing the canary is the whole
rollback: it takes seconds and affected at most *w*% of requests. If promotion itself fails, `promote.sh` runs
`kubectl rollout undo` on the stable Deployment automatically.

## Gotchas

- **Database migrations** must be backward compatible: old and new versions run at the same time.
- **Low traffic means statistically weak gates.** At 10% of a few req/s, wait longer or generate synthetic
  traffic (`tests/load/k6`).
- **`kubectl apply -k k8s/base` while a weighted canary runs** resets the primary Ingress to `scalebotai-api`.
  It's harmless, but the split stops being exact until you re-run `set-weight.sh`.
- Automate it: Argo Rollouts and Flagger implement exactly these steps and gates as a controller (see docs/12).
