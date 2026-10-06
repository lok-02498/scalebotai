/*
 * ScaleBotAI - BASELINE test (phase 2 of 6)
 * ---------------------------------------------------------------------------
 * Goal: measure steady-state performance at a known, sustainable load and record
 * the reference numbers (RPS, P50/P95/P99, CPU and memory per pod, DB connections,
 * cache hit ratio) that every later test is compared with. Also validates the
 * per-pod capacity estimate used for HPA sizing (docs/11-load-testing-strategy.md).
 *   0:00-2:00   ramp 0 -> 1,000 VUs
 *   2:00-12:00  hold 1,000 VUs for 10 minutes (~430 req/s, close to the 500 req/s design headroom)
 *   12:00-13:00 ramp down
 * Pass: avg < 300 ms, p95 < 500 ms, errors < 1 %, checks > 99 %, CPU/memory < 80 % (Grafana).
 *
 *   k6 run tests/load/k6/baseline.js
 *   k6 run -o experimental-prometheus-rw tests/load/k6/baseline.js   # live k6 metrics in Grafana
 */
import { SLO_THRESHOLDS, SUMMARY_TREND_STATS, buildSummary, scaleTarget, setupTest, userJourney } from './lib/common.js';

const VUS = scaleTarget(parseInt(__ENV.VUS || '1000', 10));
const HOLD = __ENV.DURATION || '10m';

export const options = {
  insecureSkipTLSVerify: true,
  setupTimeout: '10m',
  scenarios: {
    baseline: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '2m', target: VUS },
        { duration: HOLD, target: VUS },
        { duration: '1m', target: 0 },
      ],
      gracefulRampDown: '30s',
    },
  },
  thresholds: Object.assign({}, SLO_THRESHOLDS, {
    // the steady-state numbers are what we report: keep the chat tail in check too
    chat_duration: ['avg<300', 'p(95)<500', 'p(99)<1000'],
  }),
  summaryTrendStats: SUMMARY_TREND_STATS,
  tags: { test_type: 'baseline' },
};

export function setup() {
  return setupTest('baseline');
}

export default function (data) {
  userJourney(data);
}

export function handleSummary(data) {
  return buildSummary(data, 'baseline');
}
