/*
 * ScaleBotAI - SMOKE test (phase 1 of 6)
 * ---------------------------------------------------------------------------
 * Goal: prove the system and the test script work end to end under a small load
 * before spending money/time on bigger tests. 100 VUs, 2 minutes.
 *   0:00-0:30  ramp 0 -> 100 VUs
 *   0:30-1:30  hold 100 VUs  (~40-45 req/s with 1-3 s think time)
 *   1:30-2:00  ramp down
 * Pass: avg < 300 ms, p95 < 500 ms, http errors < 1 %, checks > 99 %.
 *
 * Run (from the repo root):
 *   k6 run tests/load/k6/smoke.js
 *   k6 run -e BASE_URL=https://api.scalebotai.local tests/load/k6/smoke.js
 */
import { SLO_THRESHOLDS, SUMMARY_TREND_STATS, buildSummary, scaleTarget, setupTest, userJourney } from './lib/common.js';

const VUS = scaleTarget(parseInt(__ENV.VUS || '100', 10));

export const options = {
  insecureSkipTLSVerify: true, // self-signed certificate on NGINX / Ingress in the lab
  setupTimeout: '10m',
  scenarios: {
    smoke: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: VUS },
        { duration: '1m', target: VUS },
        { duration: '30s', target: 0 },
      ],
      gracefulRampDown: '30s',
    },
  },
  thresholds: Object.assign({}, SLO_THRESHOLDS),
  summaryTrendStats: SUMMARY_TREND_STATS,
  tags: { test_type: 'smoke' },
};

export function setup() {
  return setupTest('smoke');
}

export default function (data) {
  userJourney(data);
}

export function handleSummary(data) {
  return buildSummary(data, 'smoke');
}
