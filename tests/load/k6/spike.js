/*
 * ScaleBotAI - SPIKE test (phase 5 of 6)
 * ---------------------------------------------------------------------------
 * Goal: a sudden, extreme burst (viral post, exam night, marketing push): jump from
 * a 1,000-VU baseline to 50,000 VUs in 30 s, hold 2 minutes, drop back. We check
 * that the platform degrades gracefully (429/503 instead of crashes, no data loss,
 * no pod crash-loops), that the HPA reacts, and - most important - that it RECOVERS
 * to SLO latency once the spike is gone.
 *   0:00-2:00    ramp 0 -> 1,000 VUs              phase warmup
 *   2:00-4:00    hold 1,000                       phase baseline
 *   4:00-4:30    1,000 -> 50,000 VUs (sudden)     phase spike
 *   4:30-6:30    hold 50,000 for 2 minutes        phase spike
 *   6:30-7:00    50,000 -> 1,000                  phase spike-down
 *   7:00-12:00   hold 1,000                       phase recovery
 *   12:00-13:00  ramp down                        phase ramp-down
 * Pass: baseline and recovery phases meet the SLO; during the spike < 10 % errors
 * and no pod restarts (check Grafana / kubectl).
 *
 * Tip: with 49,000 new VUs each registering + logging in (2 bcrypt hashes each) the
 * spike is also a "login storm". Use AUTH_MODE=pool to measure the chat path only.
 *
 *   k6 run -e AUTH_MODE=pool -e POOL_SIZE=500 tests/load/k6/spike.js
 *   k6 run -e SCALE=0.02 tests/load/k6/spike.js      # 20 -> 1,000 VUs (laptop)
 */
import {
  SUMMARY_TREND_STATS, buildPlan, buildSummary, setupTest, tagPhase, userJourney,
} from './lib/common.js';

const BASE_VUS = parseInt(__ENV.BASE_VUS || '1000', 10);
const SPIKE_VUS = parseInt(__ENV.VUS || '50000', 10);

const PLAN = buildPlan([
  { duration: '2m', target: BASE_VUS, phase: 'warmup' },
  { duration: '2m', target: BASE_VUS, phase: 'baseline' },
  { duration: '30s', target: SPIKE_VUS, phase: 'spike' },
  { duration: '2m', target: SPIKE_VUS, phase: 'spike' },
  { duration: '30s', target: BASE_VUS, phase: 'spike-down' },
  { duration: '5m', target: BASE_VUS, phase: 'recovery' },
  { duration: '1m', target: 0, phase: 'ramp-down' },
]);

export const options = {
  insecureSkipTLSVerify: true,
  setupTimeout: '10m',
  scenarios: {
    spike: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: PLAN.stages,
      gracefulRampDown: '30s',
    },
  },
  thresholds: {
    // before the spike: normal SLO
    'http_req_duration{phase:baseline}': ['avg<300', 'p(95)<500'],
    'http_req_failed{phase:baseline}': ['rate<0.01'],
    // during the spike: graceful degradation, not collapse
    'http_req_failed{phase:spike}': ['rate<0.10'],
    // after the spike: back to SLO within the recovery window
    'http_req_duration{phase:recovery}': ['avg<300', 'p(95)<500'],
    'http_req_failed{phase:recovery}': ['rate<0.01'],
    checks: ['rate>0.90'],
  },
  summaryTrendStats: SUMMARY_TREND_STATS,
  tags: { test_type: 'spike' },
};

export function setup() {
  return setupTest('spike');
}

export default function (data) {
  tagPhase(data, PLAN.phases);
  userJourney(data);
}

export function handleSummary(data) {
  return buildSummary(data, 'spike');
}
