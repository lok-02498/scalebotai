/*
 * ScaleBotAI - SOAK (endurance) test (phase 6 of 6)
 * ---------------------------------------------------------------------------
 * Goal: find problems that only appear over TIME, not under peak load: memory
 * leaks (RSS creeping toward the 512Mi limit), connection leaks (DB pool /
 * pg_stat_activity growing, "idle in transaction"), Redis memory growth/evictions,
 * log-volume and disk growth (Elasticsearch, Postgres), token expiry handling,
 * slow latency drift, pod restarts.
 *
 * Why realistic and not peak load: a soak test must run long enough for slow
 * leaks to show, and the system must be comfortably *within* its capacity so that
 * any degradation can be attributed to time (leaks, exhaustion, fragmentation)
 * rather than to overload. 500 VUs with 3-10 s think time (real users read the
 * answer) is ~70-75 req/s: between the busy-hour average (~23 req/s) and the
 * design peak (~116 req/s). Over 24 h that is ~6 million requests, i.e. about
 * six days of production traffic compressed into one day.
 *   0:00-0:05      ramp 0 -> 500 VUs
 *   0:05-23:55     hold 500 VUs
 *   23:55-24:00    ramp down
 * Pass: SLOs hold for the whole run, no upward trend in RSS / DB connections /
 * latency, zero pod restarts, Redis without evictions.
 * Tokens expire after 30 minutes - the library refreshes them every 25 minutes.
 *
 *   k6 run -o experimental-prometheus-rw tests/load/k6/soak.js
 *   k6 run -e DURATION=2h tests/load/k6/soak.js        # shorter rehearsal
 */
import { SLO_THRESHOLDS, SUMMARY_TREND_STATS, buildSummary, scaleTarget, setupTest, userJourney } from './lib/common.js';

const VUS = scaleTarget(parseInt(__ENV.VUS || '500', 10));
const HOLD = __ENV.DURATION || '23h50m';
const THINK_MIN = parseFloat(__ENV.THINK_MIN || '3');
const THINK_MAX = parseFloat(__ENV.THINK_MAX || '10');

export const options = {
  insecureSkipTLSVerify: true,
  setupTimeout: '10m',
  scenarios: {
    soak: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '5m', target: VUS },
        { duration: HOLD, target: VUS },
        { duration: '5m', target: 0 },
      ],
      gracefulRampDown: '1m',
    },
  },
  thresholds: Object.assign({}, SLO_THRESHOLDS),
  summaryTrendStats: SUMMARY_TREND_STATS,
  tags: { test_type: 'soak' },
};

export function setup() {
  return setupTest('soak');
}

export default function (data) {
  userJourney(data, THINK_MIN, THINK_MAX);
}

export function handleSummary(data) {
  return buildSummary(data, 'soak');
}
