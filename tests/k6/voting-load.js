import http from 'k6/http';
import { check, fail, sleep } from 'k6';
import { Rate } from 'k6/metrics';
import exec from 'k6/execution';

const BASE_URL = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const EVENT_ID = __ENV.EVENT_ID;
const MODE = __ENV.MODE || 'steady';
const VUS = Number(__ENV.VUS || 40);
if (!EVENT_ID || __ENV.ALLOW_TEST_VOTES !== '1') {
  throw new Error('Provide a dedicated EVENT_ID and ALLOW_TEST_VOTES=1 (creates test votes).');
}
if (!['steady', 'burst'].includes(MODE) || !Number.isInteger(VUS) || VUS < 1 || VUS > 60) {
  throw new Error('MODE must be steady/burst; VUS must be an integer from 1 to 60.');
}

const serverErrors = new Rate('server_errors');
export const options = {
  scenarios: MODE === 'burst' ? {
    voting: { executor: 'per-vu-iterations', vus: VUS, iterations: 1, maxDuration: '2m' },
  } : {
    voting: {
      executor: 'ramping-vus', startVUs: 0,
      stages: [
        { duration: '30s', target: Math.min(10, VUS) },
        { duration: '30s', target: Math.min(20, VUS) },
        { duration: '30s', target: VUS },
        { duration: __ENV.HOLD_DURATION || '10m', target: VUS },
        { duration: '15s', target: 0 },
      ],
      gracefulRampDown: '10s',
    },
  },
  thresholds: {
    checks: ['rate==1'],
    http_req_failed: [{ threshold: 'rate<0.01', abortOnFail: true, delayAbortEval: '30s' }],
    server_errors: [{ threshold: 'rate==0', abortOnFail: true, delayAbortEval: '10s' }],
    'http_req_duration{endpoint:vote}': ['p(95)<3000'],
    ...(MODE === 'steady' ? { 'http_req_duration{endpoint:detail}': ['p(95)<1000'] } : {}),
    'http_req_duration{endpoint:poll}': ['p(95)<1000'],
  },
  setupTimeout: '30s', teardownTimeout: '30s',
};

const eventUrl = `${BASE_URL}/api/events/${encodeURIComponent(EVENT_ID)}/`;
const params = endpoint => ({ timeout: '20s', tags: { endpoint } });

export function setup() {
  const response = http.get(eventUrl, params('preflight'));
  if (response.status !== 200) fail(`Event preflight returned HTTP ${response.status}.`);
  const event = response.json();
  if (event.status !== 'active' || Date.parse(event.responseDeadline) <= Date.now()) {
    fail('Test event must be active and voting deadline must be in the future.');
  }
  if (!Array.isArray(event.slots) || event.slots.length === 0) fail('Test event has no slots.');
  const prefix = `load-${Date.now().toString(36)}-${MODE}-`;
  console.log(`Target ${BASE_URL}, event ${EVENT_ID}, mode ${MODE}, ${VUS} users; nickname prefix ${prefix}`);
  return { prefix, slots: event.slots.map(slot => slot.id) };
}

let voted = false;
export default function(data) {
  // Burst starts with POST so all users submit together; steady includes reading/thinking.
  if (MODE === 'steady') {
    const detail = http.get(eventUrl, params('detail'));
    serverErrors.add(detail.status === 0 || detail.status >= 500);
    check(detail, { 'event detail returns 200': r => r.status === 200 });
    if (!voted) sleep(2 + Math.random() * 3);
  }
  if (!voted) {
    const nickname = `${data.prefix}${exec.vu.idInTest}`;
    const response = http.post(`${eventUrl}responses/`, JSON.stringify({
      nickname, phoneLastThree: '123',
      slotAvailabilities: data.slots.map((slotId, index) => ({
        slotId, availability: ['available', 'if_needed', 'unavailable'][index % 3],
      })),
    }), { ...params('vote'), headers: { 'Content-Type': 'application/json' } });
    serverErrors.add(response.status === 0 || response.status >= 500);
    check(response, {
      'vote returns 201': r => r.status === 201,
      'vote response includes submitted nickname': r => r.status === 201 &&
        r.json('responses').some(vote => vote.nickname === nickname),
    });
    // Never retry a timed-out POST: the vote may already have been committed.
    voted = true;
  }
  const poll = http.get(`${eventUrl}poll/`, params('poll'));
  serverErrors.add(poll.status === 0 || poll.status >= 500);
  check(poll, { 'event poll returns 200': r => r.status === 200 });
  if (MODE === 'steady') sleep(5);
}

export function teardown(data) {
  const response = http.get(eventUrl, params('verification'));
  if (response.status !== 200) {
    check(response, { 'final verification returns 200': r => r.status === 200 });
    return;
  }
  const votes = response.json('responses').filter(vote => vote.nickname.startsWith(data.prefix));
  const unique = new Set(votes.map(vote => vote.nickname));
  check(votes, {
    'all users votes are persisted exactly once': () => votes.length === VUS && unique.size === VUS,
    'all persisted votes contain every slot choice': () => votes.length === VUS && votes.every(vote =>
      Array.isArray(vote.slotAvailabilities) && vote.slotAvailabilities.length === data.slots.length),
  });
  console.log(`Verified ${votes.length}/${VUS} persisted votes for ${data.prefix}`);
}
