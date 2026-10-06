# Rolling update (default strategy)

ScaleBotAI deploys with a rolling update by default (Jenkins `DEPLOY_STRATEGY=rolling`). The Deployment
`scalebotai-api` replaces pods a few at a time. Old pods keep serving until their replacements are Ready, so a
release causes no downtime.

Full explanation, diagrams and comparison: [`docs/12-deployment-strategies.md`](../../../docs/12-deployment-strategies.md).

## How zero downtime is achieved

| Setting (base Deployment) | Value | Effect |
|---|---|---|
| `strategy.rollingUpdate.maxSurge` | `25%` | 1 extra pod (of 3) may exist during the update |
| `strategy.rollingUpdate.maxUnavailable` | `0` | an old pod is removed only after a new one is Ready, so capacity never drops |
| `minReadySeconds` | `10` | a new pod must stay Ready for 10 s before the rollout continues |
| `readinessProbe` `/health/ready` | 200 only when the DB answers | traffic only reaches pods that can serve it |
| `preStop: sleep 5` + `terminationGracePeriodSeconds: 30` | | in-flight requests finish while endpoints update (connection draining) |
| `progressDeadlineSeconds` | `600` | a stuck rollout is marked failed, so `rollout status` exits non-zero |
| `revisionHistoryLimit` | `10` | old ReplicaSets are kept for instant rollback |
| PDB `minAvailable: 2` | | node drains during a release can't drop below 2 pods |

`deployment-patch.yaml` holds exactly these rollout knobs, so you can re-apply them or experiment:

```bash
kubectl -n scalebotai patch deployment scalebotai-api \
  --type strategic --patch-file k8s/strategies/rolling-update/deployment-patch.yaml
```

## Release commands (what Jenkins runs)

```bash
NS=scalebotai
IMAGE=docker.io/scalebotai/scalebotai-api:42-abc1234

# 1. Change the image. This creates a new ReplicaSet and the rollout starts.
kubectl -n $NS set image deployment/scalebotai-api api=$IMAGE
kubectl -n $NS annotate deployment/scalebotai-api kubernetes.io/change-cause="build 42 (abc1234)" --overwrite

# 2. Wait. Exits 0 on success and non-zero on failure or timeout.
kubectl -n $NS rollout status deployment/scalebotai-api --timeout=300s

# 3. Watch it happen (second terminal)
kubectl -n $NS get rs -l app=scalebotai-api -w        # new RS scales up, old RS scales down
kubectl -n $NS get pods -l app=scalebotai-api -o wide -w
```

## Inspect, pause, resume

```bash
kubectl -n $NS rollout history deployment/scalebotai-api              # revisions + CHANGE-CAUSE
kubectl -n $NS rollout history deployment/scalebotai-api --revision=3 # pod template of revision 3

# Pause: batch several changes (image + env + resources) into ONE rollout,
# or freeze a rollout halfway to observe the new pods.
kubectl -n $NS rollout pause deployment/scalebotai-api
kubectl -n $NS set image deployment/scalebotai-api api=$IMAGE
kubectl -n $NS set resources deployment/scalebotai-api -c api --limits=cpu=1,memory=512Mi
kubectl -n $NS rollout resume deployment/scalebotai-api

# Restart all pods without changing anything, e.g. after a ConfigMap or Secret change
kubectl -n $NS rollout restart deployment/scalebotai-api
```

## Rollback

```bash
kubectl -n $NS rollout undo deployment/scalebotai-api                  # back to the previous revision
kubectl -n $NS rollout undo deployment/scalebotai-api --to-revision=3  # back to a specific revision
kubectl -n $NS rollout status deployment/scalebotai-api --timeout=300s
```

A rollback is itself a rolling update back to the old ReplicaSet's template, so it is also zero-downtime. The
old ReplicaSet still exists and its image is usually still cached on the nodes, which makes the rollback fast.
`scripts/rollback.sh` (CI/CD) wraps these commands.

## Things that can go wrong

| Symptom | Cause | Fix |
|---|---|---|
| `rollout status` hangs, new pod `0/1 Running` | readiness failing (DB unreachable, bad config or secret) | `kubectl logs`, `kubectl describe pod`; then `rollout undo` |
| `ImagePullBackOff` | wrong tag or registry credentials | check the tag; old pods keep serving because `maxUnavailable: 0` |
| `ProgressDeadlineExceeded` | no progress for 600 s | Jenkins rollback stage runs `rollout undo` |
| Mixed versions answer during rollout | normal for rolling updates | APIs and DB schema must stay backward compatible for one release |
