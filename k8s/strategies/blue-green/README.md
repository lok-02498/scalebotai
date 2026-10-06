# Blue-green deployment

Two complete production environments run side by side: **blue** (`scalebotai-api-blue`) and **green**
(`scalebotai-api-green`). Users only reach the colour that the Service `scalebotai-api-active` selects. A release
goes to the idle colour and is tested through `scalebotai-api-preview`. `switch.sh` then moves user traffic by
changing one label in the active Service's selector. Rolling back is the same operation in reverse and takes seconds.

Full explanation, diagrams and comparison: [`docs/12-deployment-strategies.md`](../../../docs/12-deployment-strategies.md).

> **Use blue-green instead of the base Deployment, not alongside it.** Blue/green pods carry
> `app: scalebotai-api` so that NetworkPolicies, the PDB, Prometheus and Filebeat treat them like any API pod.
> The public Ingress is re-pointed at `scalebotai-api-active`. Do not also run `deployment/scalebotai-api`
> behind the same Service. When you switch modes, scale the base Deployment away (step 2 below).

## Files

| File | Object | Purpose |
|---|---|---|
| `deployment-blue.yaml` | Deployment `scalebotai-api-blue` (labels `app: scalebotai-api`, `color: blue`) | blue environment, 3 replicas |
| `deployment-green.yaml` | Deployment `scalebotai-api-green` (`color: green`) | green environment, 3 replicas |
| `service-active.yaml` | Service `scalebotai-api-active`, selector `app` + `color: blue` | user traffic; `switch.sh` changes the colour |
| `service-preview.yaml` | Service `scalebotai-api-preview`, selector `app` + `color: green` | always the idle colour, used for testing |
| `ingress-blue-green.yaml` | Ingress `scalebotai-ingress` → `scalebotai-api-active`, plus `scalebotai-ingress-preview` | replaces the base Ingress backend; preview host for private networks |
| `switch.sh` | | pre-flight checks, switch, verification, automatic rollback |

## One-time setup (switch from rolling mode to blue-green mode)

```bash
NS=scalebotai
# 1. Both environments, both services, the blue-green ingress
kubectl apply -f k8s/strategies/blue-green/deployment-blue.yaml \
              -f k8s/strategies/blue-green/deployment-green.yaml \
              -f k8s/strategies/blue-green/service-active.yaml \
              -f k8s/strategies/blue-green/service-preview.yaml
kubectl -n $NS rollout status deployment/scalebotai-api-blue  --timeout=300s
kubectl -n $NS rollout status deployment/scalebotai-api-green --timeout=300s
kubectl apply -f k8s/strategies/blue-green/ingress-blue-green.yaml   # users -> active (blue)

# 2. Retire the rolling-mode Deployment. Delete its HPA first, otherwise the HPA
#    (minReplicas 3) scales it straight back up.
kubectl -n $NS delete hpa scalebotai-api
kubectl -n $NS scale deployment scalebotai-api --replicas=0
```

## Every release

```bash
NS=scalebotai
IMAGE=docker.io/scalebotai/scalebotai-api:43-def5678

# 1. Find the idle colour (the one preview points at)
IDLE=$(kubectl -n $NS get svc scalebotai-api-preview -o jsonpath='{.spec.selector.color}')   # e.g. green

# 2. Deploy the new version to the idle colour. Users are not affected.
kubectl -n $NS set image deployment/scalebotai-api-$IDLE api=$IMAGE
kubectl -n $NS annotate deployment/scalebotai-api-$IDLE kubernetes.io/change-cause="build 43 (def5678)" --overwrite
kubectl -n $NS rollout status deployment/scalebotai-api-$IDLE --timeout=300s

# 3. Smoke-test the idle colour through the preview Service
kubectl -n $NS port-forward svc/scalebotai-api-preview 8080:80 &
./scripts/smoke_test.sh http://localhost:8080
kill %1
#    (or: ./scripts/smoke_test.sh https://preview.api.scalebotai.local)

# 4. Switch user traffic. switch.sh rolls back by itself if verification fails.
VERIFY_URL=https://api.scalebotai.local/health/ready ./k8s/strategies/blue-green/switch.sh $IDLE
```

## Rollback

```bash
./k8s/strategies/blue-green/switch.sh          # no argument = toggle back to the other colour
# or explicitly:  ./k8s/strategies/blue-green/switch.sh blue
```

The previous version is still running and Ready, so rollback is just a selector change: no pod starts and no
image is pulled. Keep the old colour running until the new one has soaked (e.g. 30–60 min of clean metrics).
After that you may scale it down to save cost (`kubectl scale deployment/scalebotai-api-<old> --replicas=0`), but
then rollback means scaling it up first.

## Back to rolling mode

```bash
NS=scalebotai
LIVE=$(kubectl -n $NS get svc scalebotai-api-active -o jsonpath='{.spec.selector.color}')
IMAGE=$(kubectl -n $NS get deploy scalebotai-api-$LIVE -o jsonpath='{.spec.template.spec.containers[0].image}')

# 1. Bring the base Deployment back up with the live version
kubectl -n $NS set image deployment/scalebotai-api api=$IMAGE
kubectl -n $NS scale deployment scalebotai-api --replicas=3
kubectl -n $NS rollout status deployment/scalebotai-api --timeout=300s

# 2. Point the public Ingress back at scalebotai-api and restore the HPA.
#    Apply single files, NOT `apply -k k8s/base`: that would also reset the image to `latest`.
kubectl apply -f k8s/base/08-ingress.yaml -f k8s/base/09-hpa.yaml

# 3. Remove the blue-green objects BY NAME. Don't use `delete -f` on the directory:
#    ingress-blue-green.yaml contains scalebotai-ingress, the Ingress you just restored.
kubectl -n $NS delete deployment scalebotai-api-blue scalebotai-api-green --ignore-not-found
kubectl -n $NS delete service scalebotai-api-active scalebotai-api-preview --ignore-not-found
kubectl -n $NS delete ingress scalebotai-ingress-preview --ignore-not-found
```

## Gotchas

- **Database schema:** both colours share the same Postgres. The new version's schema changes must still work
  with the old version, because the old colour stays live as the rollback target. Use expand/contract migrations.
- **Capacity:** blue-green needs 2× the pods during a release (and permanently, if you keep the idle colour warm).
- **Re-applying `service-active.yaml`** resets the selector to blue. After the first switch, use `switch.sh`.
- **Long-lived connections** (keep-alive or streaming) to the old colour finish normally. The old pods keep running,
  they just stop receiving new requests.
