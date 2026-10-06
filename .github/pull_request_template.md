## Summary

<!-- What does this PR change and why? Link the issue: "Closes #123". -->

## Type of change

- [ ] `feat` - new feature
- [ ] `fix` - bug fix
- [ ] `perf` / `refactor` - no behaviour change
- [ ] `ci` / `build` - Jenkins, Docker, scripts
- [ ] `deploy` - Kubernetes manifests / release strategy
- [ ] `docs` / `test` / `chore`

## How was this tested?

- [ ] `pytest` passes locally and coverage stays >= 70 %
- [ ] `ruff check src tests` and `ruff format --check src tests` are clean
- [ ] `bandit -r src/app -ll` reports no MEDIUM/HIGH findings
- [ ] `docker build -f docker/api/Dockerfile .` succeeds
- [ ] `scripts/smoke_test.sh <url>` passes against a local stack (compose or minikube)
- [ ] New tests cover the change (unit and/or integration)

## Deployment impact

- [ ] No impact
- [ ] New/changed environment variables (updated `.env.example`, ConfigMap `scalebotai-config` / Secret `scalebotai-secrets`)
- [ ] Database schema change (tables are created by the app; describe migration/backfill)
- [ ] Kubernetes manifest change (`k8s/`) - recommended `DEPLOY_STRATEGY`: rolling / canary / blue-green
- [ ] Monitoring change (metrics, alerts, dashboards)

**Rollback plan:** <!-- default: Jenkins auto-rollback / `scripts/rollback.sh <strategy>` -->

## Checklist

- [ ] PR title follows Conventional Commits (`feat(chat): add conversation rename`)
- [ ] Jenkins status check is green
- [ ] Docs updated (`docs/`, README) where behaviour changed
- [ ] No secrets, tokens or real credentials committed
