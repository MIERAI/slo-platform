// Synthetic users for one application in one environment (chart platform/traffic, from app.yaml `traffic`).
// Users have a day (quiet nights, a lunch and an evening peak), mix requests, make mistakes (4xx: valid, not bad, for
// an SLO) and sometimes all arrive at once (bursts). A constant 2 req/s would hide what matters to SLOs: at night one
// error is a large share of the traffic. Open loop (arrival rate): a slow service does not slow the users down.
//
//   REQUESTS          JSON list: {weight, method, url, body?, badBody?, badRate?}; {rand} in url / body → 0..999
//   PEAK_RPM, SCALE   requests per minute at the day's peak × the environment's scale
//   DAY_MINUTES       one day lasts this long (1440: a real day; less: a compressed one, for quick tests)
//   DAYS              days per run; then k6 exits and the container restarts (burst scenarios preallocate VUs)
//   ALIGN_CLOCK=1     start the day where the wall clock is (TZ_OFFSET_HOURS from UTC): with DAY_MINUTES=1440 the
//                     night is the real night, whenever the pod (re)starts
//   BURST_HOURS       "12,20": 3× the peak for one (compressed) minute at these hours
import http from 'k6/http';

const DAY = Number(__ENV.DAY_MINUTES || 1440);
const DAYS = Number(__ENV.DAYS || 1);
const RATE = Number(__ENV.PEAK_RPM || 300) * Number(__ENV.SCALE || 1);
const REQUESTS = JSON.parse(__ENV.REQUESTS || '[]');
if (!REQUESTS.length) throw new Error('REQUESTS is empty: nothing to send');
// share of the peak per hour of the day, 0h..23h
const PROFILE = [0.08, 0.05, 0.04, 0.04, 0.05, 0.1, 0.25, 0.45, 0.6, 0.7, 0.75, 0.85,
                 0.95, 0.8, 0.7, 0.7, 0.75, 0.85, 1.0, 0.95, 0.8, 0.55, 0.3, 0.15];
const BURST_HOURS = (__ENV.BURST_HOURS || '12,20').split(',').filter((h) => h !== '').map(Number);

const hour = DAY / 24;               // minutes of one (compressed) hour
const TZ = Number(__ENV.TZ_OFFSET_HOURS || 0);
// minutes into the day at the start: 0, or (ALIGN_CLOCK) where the wall clock is in its day, scaled to DAY
const OFFSET = __ENV.ALIGN_CLOCK === '1' ? ((((Date.now() / 60000 + TZ * 60) % 1440) + 1440) % 1440) / 1440 * DAY : 0;
const H0 = Math.floor(OFFSET / hour);   // the hour of the day the run starts in
const secs = (min) => `${Math.max(1, Math.round(min * 60))}s`;
// VUs to keep up when every request runs into the 10s timeout (an outage), within the memory limit
const vus = (rpm) => Math.min(60, Math.max(5, Math.ceil((rpm / 60) * 10)));

const stages = [];
for (let i = 0; i < DAYS * 24; i++) {   // hour by hour from H0; the first one only for what is left of it
  const p = PROFILE[(H0 + i) % 24];
  stages.push({ target: Math.max(1, Math.round(RATE * p)), duration: secs(i === 0 ? (H0 + 1) * hour - OFFSET : hour) });
}
const scenarios = {
  users: {
    executor: 'ramping-arrival-rate', exec: 'user', timeUnit: '1m', stages,
    startRate: Math.max(1, Math.round(RATE * PROFILE[H0])), preAllocatedVUs: 5, maxVUs: vus(RATE),
  },
};
for (let d = 0; d <= DAYS; d++) {
  for (const h of BURST_HOURS) {
    const at = d * DAY + h * hour - OFFSET;   // minutes after the start
    if (at < 0 || at + 1 > DAYS * DAY) continue;
    scenarios[`burst-${d}-${h}`] = {
      executor: 'constant-arrival-rate', exec: 'user', timeUnit: '1m', duration: '60s',
      rate: Math.max(1, Math.round(RATE * 3)), startTime: secs(at), preAllocatedVUs: 1, maxVUs: Math.ceil(vus(RATE) / 2),
    };
  }
}

export const options = { scenarios, discardResponseBodies: true, summaryTrendStats: ['p(95)'] };

const TOTAL = REQUESTS.reduce((t, r) => t + r.weight, 0);
const rand = (s) => s.replace(/\{rand\}/g, () => String(Math.floor(Math.random() * 1000)));

export function user() {
  let x = Math.random() * TOTAL;
  const r = REQUESTS.find((q) => (x -= q.weight) < 0) || REQUESTS[0];
  // the name tag groups k6's own metrics per request template, not per (random) URL
  const params = { timeout: '10s', tags: { name: `${r.method} ${r.url}` } };
  let body = r.body;
  if (r.badBody !== undefined && Math.random() < (r.badRate || 0)) body = r.badBody;   // a user's mistake: a 4xx
  if (body !== undefined) {
    body = rand(typeof body === 'string' ? body : JSON.stringify(body));
    params.headers = { 'Content-Type': 'application/json' };
  }
  http.request(r.method, rand(r.url), body === undefined ? null : body, params);
}
