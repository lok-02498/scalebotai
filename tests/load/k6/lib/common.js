/*
 * ScaleBotAI - shared k6 library
 * ---------------------------------------------------------------------------
 * Used by smoke.js, baseline.js, load.js, stress.js, spike.js and soak.js.
 *
 * Environment variables (all optional):
 *   BASE_URL               target, default https://api.scalebotai.local (NGINX / Ingress)
 *   AUTH_MODE              per-vu (default) = every VU registers + logs in its own user
 *                          pool              = setup() pre-creates POOL_SIZE users and VUs
 *                                              share their tokens (avoids a bcrypt "login
 *                                              storm" when tens of thousands of VUs start)
 *   POOL_SIZE              accounts created in pool mode (default 200)
 *   LOADTEST_PASSWORD      password for generated users (default LoadTest123!)
 *   EMAIL_DOMAIN           e-mail domain for generated users (default scalebotai.dev;
 *                          reserved TLDs such as .local/.test are rejected by EmailStr)
 *   THINK_MIN / THINK_MAX  think time in seconds between actions (default 1 / 3)
 *   UNIQUE_QUESTION_RATIO  share of chat messages made unique (cache misses), default 0.2
 *   TOKEN_REFRESH_SECONDS  refresh the access token after this age (default 1500 s; the
 *                          API issues 30-minute tokens - matters for load/soak tests)
 *   RESULTS_DIR            where handleSummary writes files (default tests/load/k6/results,
 *                          relative to the directory k6 is started from = repo root)
 *
 * NOTE: the API rate limits each user/IP (RATE_LIMIT_PER_MINUTE=120 by default) and
 * NGINX applies limit_req. For load tests raise RATE_LIMIT_PER_MINUTE (or set
 * RATE_LIMIT_ENABLED=false) in the test environment - see tests/load/k6/README.md.
 */
import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';
import { textSummary } from 'https://jslib.k6.io/k6-summary/0.0.2/index.js';

// ----------------------------------------------------------------------------- config
export const BASE_URL = (__ENV.BASE_URL || 'https://api.scalebotai.local').replace(/\/+$/, '');
export const API = `${BASE_URL}/api/v1`;

const PASSWORD = __ENV.LOADTEST_PASSWORD || 'LoadTest123!';
const EMAIL_DOMAIN = __ENV.EMAIL_DOMAIN || 'scalebotai.dev';
const AUTH_MODE = (__ENV.AUTH_MODE || 'per-vu').toLowerCase();
const POOL_SIZE = parseInt(__ENV.POOL_SIZE || '200', 10);
const TOKEN_REFRESH_SECONDS = parseInt(__ENV.TOKEN_REFRESH_SECONDS || '1500', 10);
const UNIQUE_QUESTION_RATIO = parseFloat(__ENV.UNIQUE_QUESTION_RATIO || '0.2');
const DEFAULT_THINK_MIN = parseFloat(__ENV.THINK_MIN || '1');
const DEFAULT_THINK_MAX = parseFloat(__ENV.THINK_MAX || '3');
const CONTINUE_CONVERSATION_RATIO = 0.7;
const MAX_MESSAGES_PER_CONVERSATION = 10;
const RESULTS_DIR = (__ENV.RESULTS_DIR || 'tests/load/k6/results').replace(/\/+$/, '');

export const JSON_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json' };

// ----------------------------------------------------------------------------- SLO thresholds
// Project success criteria: avg < 300 ms, error rate < 1 %, (checks as functional proof).
export const SLO_THRESHOLDS = {
  http_req_duration: ['avg<300', 'p(95)<500'],
  http_req_failed: ['rate<0.01'],
  checks: ['rate>0.99'],
  chat_duration: ['avg<300', 'p(95)<500'],
  chat_success: ['rate>0.99'],
};

export const SUMMARY_TREND_STATS = ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'];

// ----------------------------------------------------------------------------- custom metrics
export const chatDuration = new Trend('chat_duration', true);
export const historyDuration = new Trend('history_duration', true);
export const loginDuration = new Trend('login_duration', true);
export const registerDuration = new Trend('register_duration', true);
export const chatSuccess = new Rate('chat_success');
export const chatCached = new Rate('chat_cached');
export const authFailures = new Counter('auth_failures');
export const rateLimited = new Counter('rate_limited_responses');
export const serverErrors = new Counter('server_errors');

// ----------------------------------------------------------------------------- test data
// Realistic questions for the "DevOps/Cloud learning assistant" (rule engine intents).
// Repeated questions hit the Redis response cache, like real users asking FAQs.
export const QUESTIONS = [
  'Hello!',
  'Hi, what can you help me with?',
  'help',
  'What is Docker and why should I use containers?',
  'How do I write a multi-stage Dockerfile for a Python app?',
  'What is the difference between a Docker image and a container?',
  'How do I reduce the size of my Docker image?',
  'Explain Kubernetes pods, deployments and services',
  'How does a Kubernetes HorizontalPodAutoscaler work?',
  'What is the difference between liveness and readiness probes?',
  'How do I do a rolling update in Kubernetes with zero downtime?',
  'What is a Kubernetes Ingress controller?',
  'How do I roll back a failed deployment with kubectl?',
  'What is a Jenkins pipeline and what is a Jenkinsfile?',
  'Explain CI/CD in simple words',
  'What stages should a CI/CD pipeline have?',
  'What is the difference between git merge and git rebase?',
  'How do I undo my last git commit?',
  'Explain git branching strategies like GitFlow',
  'How do I configure NGINX as a reverse proxy and load balancer?',
  'What is rate limiting in NGINX?',
  'When should I use Redis as a cache?',
  'What is a cache TTL and how do I choose it?',
  'How do I optimize slow PostgreSQL queries?',
  'What are database indexes and when do they help?',
  'What is connection pooling and why does it matter?',
  'What is Prometheus and how does it scrape metrics?',
  'How do I write a PromQL query for p95 latency?',
  'How do I create a Grafana dashboard?',
  'What is the ELK stack used for?',
  'What is the difference between Logstash and Filebeat?',
  'What is cloud computing? Explain IaaS, PaaS and SaaS',
  'What is the difference between horizontal and vertical scaling?',
  'How do I design a system for one million requests per day?',
  'What is blue-green deployment?',
  'What is a canary release?',
  'What is infrastructure as code?',
  'How do I secure secrets in Kubernetes?',
  'What does 99.9% availability mean in downtime per month?',
  'Thanks, that was helpful!',
];

// ----------------------------------------------------------------------------- helpers
const CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789';

export function randomString(length) {
  let out = '';
  for (let i = 0; i < length; i++) out += CHARS.charAt(Math.floor(Math.random() * CHARS.length));
  return out;
}

export function randomItem(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

/** Think time: real users read the answer before the next action. */
export function thinkTime(min = DEFAULT_THINK_MIN, max = DEFAULT_THINK_MAX) {
  sleep(min + Math.random() * Math.max(0, max - min));
}

export function randomQuestion() {
  const q = randomItem(QUESTIONS);
  // A share of unique questions models the long tail that misses the cache.
  return Math.random() < UNIQUE_QUESTION_RATIO ? `${q} (context ${randomString(6)})` : q;
}

function safeJson(res) {
  try {
    return res.json();
  } catch (e) {
    return null;
  }
}

function authParams(token, name) {
  return {
    headers: Object.assign({}, JSON_HEADERS, { Authorization: `Bearer ${token}` }),
    tags: { name },
    timeout: '30s',
  };
}

function trackStatus(res) {
  if (res.status === 429) rateLimited.add(1);
  else if (res.status >= 500 || res.status === 0) serverErrors.add(1);
}

/** Tag every metric emitted by this VU from now on (k6 >= 0.45). */
export function setVuTag(key, value) {
  if (exec.vu.metrics && exec.vu.metrics.tags) exec.vu.metrics.tags[key] = value;
}

// ----------------------------------------------------------------------------- stage helpers
// SCALE multiplies every VU target, e.g. SCALE=0.1 runs the 10,000-VU load profile with
// 1,000 VUs on a laptop. Durations are never scaled.
const SCALE = parseFloat(__ENV.SCALE || '1');

export function scaleTarget(n) {
  return n === 0 ? 0 : Math.max(1, Math.round(n * SCALE));
}

/** '1h2m30s' -> 3750 */
export function durationToSeconds(d) {
  const re = /(\d+(?:\.\d+)?)(h|m|s)/g;
  let total = 0;
  let m;
  while ((m = re.exec(d)) !== null) {
    total += parseFloat(m[1]) * ({ h: 3600, m: 60, s: 1 })[m[2]];
  }
  return total;
}

/**
 * plan = [{ duration: '2m', target: 1000, phase: 'warmup' }, ...]
 * returns { stages, phases } - stages for the ramping-vus executor (targets scaled),
 * phases = [{ name, end }] with cumulative end times in seconds.
 */
export function buildPlan(plan) {
  let t = 0;
  const phases = [];
  const stages = plan.map((s) => {
    t += durationToSeconds(s.duration);
    phases.push({ name: s.phase, end: t });
    return { duration: s.duration, target: scaleTarget(s.target) };
  });
  return { stages, phases };
}

/** Tags this VU's metrics with phase=<name> based on time since setup() finished. */
export function tagPhase(data, phases, tagName = 'phase') {
  const elapsed = (Date.now() - data.startedAt) / 1000;
  let name = phases[phases.length - 1].name;
  for (let i = 0; i < phases.length; i++) {
    if (elapsed < phases[i].end) {
      name = phases[i].name;
      break;
    }
  }
  setVuTag(tagName, name);
  return name;
}

// ----------------------------------------------------------------------------- ops endpoints
/** Called from setup(): aborts the whole test if the API is not healthy. */
export function healthCheck() {
  const res = http.get(`${BASE_URL}/health`, { tags: { name: 'GET /health' }, timeout: '10s' });
  const body = safeJson(res);
  const ok = check(res, {
    'health: status 200': (r) => r.status === 200,
    'health: status field present': () => body !== null && body.status !== undefined,
  });
  if (!ok) {
    fail(`Health check failed: GET ${BASE_URL}/health -> ${res.status} ${String(res.body).slice(0, 200)}`);
  }
  console.log(`Target ${BASE_URL} healthy: status=${body.status} version=${body.version}`);
  return body;
}

// ----------------------------------------------------------------------------- auth
export function register(username) {
  const payload = JSON.stringify({
    username,
    email: `${username}@${EMAIL_DOMAIN}`,
    password: PASSWORD,
    full_name: `Load Test ${username}`,
  });
  const res = http.post(`${API}/auth/register`, payload, {
    headers: JSON_HEADERS,
    tags: { name: 'POST /api/v1/auth/register' },
    timeout: '30s',
  });
  registerDuration.add(res.timings.duration);
  trackStatus(res);
  const ok = check(res, { 'register: status 201': (r) => r.status === 201 });
  if (!ok) authFailures.add(1, { step: 'register' });
  return ok;
}

export function login(username) {
  const res = http.post(`${API}/auth/login`, JSON.stringify({ username, password: PASSWORD }), {
    headers: JSON_HEADERS,
    tags: { name: 'POST /api/v1/auth/login' },
    timeout: '30s',
  });
  loginDuration.add(res.timings.duration);
  trackStatus(res);
  const body = safeJson(res);
  const ok = check(res, {
    'login: status 200': (r) => r.status === 200,
    'login: has access_token': () => body !== null && typeof body.access_token === 'string',
  });
  if (!ok) {
    authFailures.add(1, { step: 'login' });
    return null;
  }
  return newSession(username, body);
}

function newSession(username, tokenBody) {
  return {
    username,
    token: tokenBody.access_token,
    refreshToken: tokenBody.refresh_token,
    obtainedAt: Date.now(),
    conversationId: null,
    messagesInConversation: 0,
  };
}

/** Registers a unique user (unique per VU/iteration + random suffix) and logs in. */
export function registerAndLogin() {
  // e.g. loadtest_42_0_k3j9x2ab  (3-50 chars, ^[a-zA-Z0-9_.-]+$)
  const username = `loadtest_${__VU}_${__ITER}_${randomString(8)}`;
  if (!register(username)) return null;
  return login(username);
}

export function refresh(session) {
  const res = http.post(`${API}/auth/refresh`, JSON.stringify({ refresh_token: session.refreshToken }), {
    headers: JSON_HEADERS,
    tags: { name: 'POST /api/v1/auth/refresh' },
    timeout: '30s',
  });
  trackStatus(res);
  const body = safeJson(res);
  const ok = check(res, {
    'refresh: status 200': (r) => r.status === 200,
    'refresh: has access_token': () => body !== null && typeof body.access_token === 'string',
  });
  if (!ok) return login(session.username); // refresh token revoked/expired -> log in again
  const s = newSession(session.username, body);
  s.conversationId = session.conversationId;
  s.messagesInConversation = session.messagesInConversation;
  return s;
}

// ----------------------------------------------------------------------------- business endpoints
/** POST /api/v1/chat - conversationId null/undefined starts a new conversation. */
export function chat(token, conversationId) {
  const payload = { message: randomQuestion() };
  if (conversationId) payload.conversation_id = conversationId;
  const res = http.post(`${API}/chat`, JSON.stringify(payload), authParams(token, 'POST /api/v1/chat'));
  chatDuration.add(res.timings.duration);
  trackStatus(res);
  const body = safeJson(res);
  const ok = check(res, {
    'chat: status 200': (r) => r.status === 200,
    'chat: has response text': () => body !== null && typeof body.response === 'string' && body.response.length > 0,
    'chat: has conversation_id': () => body !== null && typeof body.conversation_id === 'string',
  });
  chatSuccess.add(ok);
  if (ok) chatCached.add(body.cached === true);
  return { status: res.status, body: ok ? body : null };
}

/** GET /api/v1/chat/history (paginated). */
export function history(token, conversationId) {
  const qs = conversationId ? `?page=1&size=20&conversation_id=${conversationId}` : '?page=1&size=20';
  const res = http.get(`${API}/chat/history${qs}`, authParams(token, 'GET /api/v1/chat/history'));
  historyDuration.add(res.timings.duration);
  trackStatus(res);
  const body = safeJson(res);
  check(res, {
    'history: status 200': (r) => r.status === 200,
    'history: has items array': () => body !== null && Array.isArray(body.items),
  });
  return { status: res.status, body };
}

export function conversations(token) {
  const res = http.get(`${API}/chat/conversations?page=1&size=20`, authParams(token, 'GET /api/v1/chat/conversations'));
  trackStatus(res);
  check(res, { 'conversations: status 200': (r) => r.status === 200 });
  return { status: res.status };
}

export function me(token) {
  const res = http.get(`${API}/users/me`, authParams(token, 'GET /api/v1/users/me'));
  trackStatus(res);
  check(res, { 'me: status 200': (r) => r.status === 200 });
  return { status: res.status };
}

// ----------------------------------------------------------------------------- setup / session
/** Pool mode: register + log in `size` users in parallel batches (runs once, in setup()). */
export function createUserPool(size) {
  const pool = [];
  const batchSize = 20;
  for (let start = 0; start < size; start += batchSize) {
    const names = [];
    for (let i = start; i < Math.min(size, start + batchSize); i++) {
      names.push(`loadtest_pool_${i}_${randomString(8)}`);
    }
    const regs = http.batch(names.map((u) => ['POST', `${API}/auth/register`, JSON.stringify({
      username: u, email: `${u}@${EMAIL_DOMAIN}`, password: PASSWORD, full_name: `Load Test ${u}`,
    }), { headers: JSON_HEADERS, tags: { name: 'POST /api/v1/auth/register' }, timeout: '60s' }]));
    const ok = names.filter((u, i) => regs[i].status === 201);
    const logins = http.batch(ok.map((u) => ['POST', `${API}/auth/login`,
      JSON.stringify({ username: u, password: PASSWORD }),
      { headers: JSON_HEADERS, tags: { name: 'POST /api/v1/auth/login' }, timeout: '60s' }]));
    logins.forEach((res, i) => {
      const body = safeJson(res);
      if (res.status === 200 && body && body.access_token) {
        pool.push({ username: ok[i], access_token: body.access_token, refresh_token: body.refresh_token });
      }
    });
  }
  if (pool.length === 0) fail('Could not create any load-test user (check rate limits / registration).');
  console.log(`User pool ready: ${pool.length}/${size} accounts`);
  return pool;
}

/** Standard setup(): health gate + optional user pool. */
export function setupTest(testName) {
  healthCheck();
  const pool = AUTH_MODE === 'pool' ? createUserPool(POOL_SIZE) : [];
  console.log(`Starting ${testName} test against ${BASE_URL} (auth mode: ${AUTH_MODE})`);
  // startedAt is taken last: scenarios start right after setup() returns, and
  // tagPhase() measures phase boundaries from this moment.
  return { testName, startedAt: Date.now(), baseUrl: BASE_URL, authMode: AUTH_MODE, pool };
}

// One session per VU: module state is per VU in k6 (each VU has its own JS runtime).
let session = null;

export function getSession(data) {
  if (session) {
    if ((Date.now() - session.obtainedAt) / 1000 > TOKEN_REFRESH_SECONDS) {
      session = session.refreshToken ? refresh(session) : login(session.username);
    }
    return session;
  }
  if (data && data.pool && data.pool.length > 0) {
    const acct = data.pool[(exec.vu.idInTest - 1) % data.pool.length];
    session = {
      username: acct.username,
      token: acct.access_token,
      refreshToken: null, // shared tokens: never rotate a refresh token other VUs use
      obtainedAt: data.startedAt,
      conversationId: null,
      messagesInConversation: 0,
    };
  } else {
    session = registerAndLogin();
  }
  return session;
}

function handleAuthError(status) {
  if (status === 401 && session) {
    // token expired or revoked -> log in again with the same credentials next iteration
    session = login(session.username);
  }
}

// ----------------------------------------------------------------------------- user journey
/**
 * One iteration of a realistic user: mostly chatting, sometimes browsing history.
 * Weights match the Locust file: chat 6, history 2, conversations 1, me 1.
 */
export function userJourney(data, thinkMin = DEFAULT_THINK_MIN, thinkMax = DEFAULT_THINK_MAX) {
  const s = getSession(data);
  if (!s) {
    thinkTime(thinkMin, thinkMax); // auth failed (already recorded); back off and retry next iteration
    return;
  }

  const r = Math.random();
  let status;
  if (r < 0.6) {
    const keep = s.conversationId && s.messagesInConversation < MAX_MESSAGES_PER_CONVERSATION
      && Math.random() < CONTINUE_CONVERSATION_RATIO;
    const res = chat(s.token, keep ? s.conversationId : null);
    status = res.status;
    if (res.body) {
      if (res.body.conversation_id === s.conversationId) {
        s.messagesInConversation += 1;
      } else {
        s.conversationId = res.body.conversation_id;
        s.messagesInConversation = 1;
      }
    }
  } else if (r < 0.8) {
    status = history(s.token, Math.random() < 0.5 ? s.conversationId : null).status;
  } else if (r < 0.9) {
    status = conversations(s.token).status;
  } else {
    status = me(s.token).status;
  }
  handleAuthError(status);
  thinkTime(thinkMin, thinkMax);
}

// ----------------------------------------------------------------------------- summary
/** handleSummary helper: console summary + JSON and text files in RESULTS_DIR. */
export function buildSummary(data, testName) {
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const base = `${RESULTS_DIR}/${testName}-${stamp}`;
  const text = textSummary(data, { indent: '  ', enableColors: false });
  const out = {
    stdout: textSummary(data, { indent: '  ', enableColors: true }),
  };
  out[`${base}.json`] = JSON.stringify(data, null, 2);
  out[`${base}.txt`] = text;
  out[`${RESULTS_DIR}/${testName}-latest.json`] = JSON.stringify(data, null, 2);
  return out;
}
