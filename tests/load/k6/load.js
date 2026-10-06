/*
 * ScaleBotAI - LOAD test (phase 3 of 6)
 * ---------------------------------------------------------------------------
 * Goal: verify the platform meets the SLOs at high expected load while the HPA
 * scales the API out (3 -> up to 20 pods). 10,000 VUs is ~4,000 req/s - far above
 * the 1M requests/day design point (~116 req/s peak) - so it needs distributed load
 * generators (k6-operator, see README) and a raised rate limit.
 *   0:00-5:00   ramp 0 -> 2,500 VUs
 *   5:00-10:00  ramp -> 5,000 VUs
 *   10:00-15:00 ramp -> 10,000 VUs
 *   15:00-25:00 hold 10,000 VUs
 *   25:00-30:00 ramp down                          total 30 minutes
 * Pass: avg < 300 ms, p95 < 500 ms, errors < 1 %, HPA scales out without pod
 * restarts, CPU/memory per pod < 80 %.
 *
 *   k6 run tests/load/k6/load.js                       # single machine (only if it can host 10k VUs)
 *   k6 run -e SCALE=0.1 tests/load/k6/load.js          # same shape at 1,000 VUs (laptop)
 *   kubectl apply -f tests/load/k6/k6-operator-testrun.yaml   # distributed
 */
import {
  SLO_THRESHOLDS, SUMMARY_TREND_STATS, buildPlan, buildSummary, setupTest, tagPhase, userJourney,
} from './lib/common.js';

const PEAK = parseInt(__ENV.VUS || '10000', 10);

const PLAN = buildPlan([
  { duration: '5m', target: Math.round(PEAK * 0.25), phase: 'ramp-25' },
  { duration: '5m', target: Math.round(PEAK * 0.5), phase: 'ramp-50' },
  { duration: '5m', target: PEAK, phase: 'ramp-100' },
  { duration: '10m', target: PEAK, phase: 'hold' },
  { duration: '5m', target: 0, phase: 'ramp-down' },
]);

export const options = {
  insecureSkipTLSVerify: true,
  setupTimeout: '10m',
  scenarios: {
    load: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: PLAN.stages,
      gracefulRampDown: '1m',
    },
  },
  thresholds: Object.assign({}, SLO_THRESHOLDS, {
    // the steady-state hold at full load must meet the SLO on its own
    'http_req_duration{phase:hold}': ['avg<300', 'p(95)<500'],
    'http_req_failed{phase:hold}': ['rate<0.01'],
  }),
  summaryTrendStats: SUMMARY_TREND_STATS,
  tags: { test_type: 'load' },
};

export function setup() {
  return setupTest('load');
}

export default function (data) {
  tagPhase(data, PLAN.phases);
  userJourney(data);
}

export function handleSummary(data) {
  return buildSummary(data, 'load');
}
