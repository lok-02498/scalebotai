/*
 * ScaleBotAI - STRESS test (phase 4 of 6)
 * ---------------------------------------------------------------------------
 * Goal: find the breaking point and the first bottleneck (DB connections, CPU,
 * NGINX, Redis ...) by increasing load in steps up to 25,000 VUs. Failing the SLO
 * at some step is EXPECTED - the result is "the system holds the SLO up to step N
 * (~X req/s with Y pods) and then degrades because of Z".
 *   step     ramp  hold   VUs        (~req/s at ~0.43 req/s per VU)
 *   1        2m    3m     2,500      ~1,100
 *   2        2m    3m     5,000      ~2,200
 *   3        2m    3m     10,000     ~4,300
 *   4        2m    3m     15,000     ~6,500
 *   5        2m    3m     20,000     ~8,600
 *   6        2m    3m     25,000     ~10,800
 *   then 5m ramp down (observe recovery)                      total 35 minutes
 * Per-step thresholds (phase:step-N) show in the summary exactly which step broke
 * the SLO. Safety stop: the run aborts when >10 % of requests fail
 * (set ABORT_ON_BREAK=false to keep pushing past the break).
 *
 *   k6 run tests/load/k6/stress.js                   # needs a very large generator
 *   k6 run -e SCALE=0.04 tests/load/k6/stress.js     # same shape, 100..1,000 VUs
 *   kubectl apply -f tests/load/k6/k6-operator-testrun.yaml   (script: stress.js)
 */
import {
  SUMMARY_TREND_STATS, buildPlan, buildSummary, scaleTarget, setupTest, tagPhase, userJourney,
} from './lib/common.js';

const MAX_VUS = parseInt(__ENV.VUS || '25000', 10);
const ABORT_ON_BREAK = (__ENV.ABORT_ON_BREAK || 'true') === 'true';
const STEP_FRACTIONS = [0.1, 0.2, 0.4, 0.6, 0.8, 1.0];

const planSteps = [];
STEP_FRACTIONS.forEach((f) => {
  const target = Math.round(MAX_VUS * f);
  const name = `step-${scaleTarget(target)}`;
  planSteps.push({ duration: '2m', target, phase: name });
  planSteps.push({ duration: '3m', target, phase: name });
});
planSteps.push({ duration: '5m', target: 0, phase: 'recovery' });
const PLAN = buildPlan(planSteps);

// SLO thresholds per step: the first failing step is the breaking point.
const thresholds = {
  http_req_failed: [{ threshold: 'rate<0.10', abortOnFail: ABORT_ON_BREAK, delayAbortEval: '1m' }],
  checks: ['rate>0.90'],
};
PLAN.phases.forEach((p) => {
  thresholds[`http_req_duration{phase:${p.name}}`] = ['avg<300', 'p(95)<500'];
  thresholds[`http_req_failed{phase:${p.name}}`] = ['rate<0.01'];
});

export const options = {
  insecureSkipTLSVerify: true,
  setupTimeout: '10m',
  scenarios: {
    stress: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: PLAN.stages,
      gracefulRampDown: '1m',
    },
  },
  thresholds,
  summaryTrendStats: SUMMARY_TREND_STATS,
  tags: { test_type: 'stress' },
};

export function setup() {
  return setupTest('stress');
}

export default function (data) {
  tagPhase(data, PLAN.phases);
  userJourney(data);
}

export function handleSummary(data) {
  return buildSummary(data, 'stress');
}
