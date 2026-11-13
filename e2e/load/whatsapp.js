// Load test for the paths that carry real traffic (k6: https://grafana.com/docs/k6/).
//
//   inbound:   new customers writing on WhatsApp. Each message is a signed Meta webhook
//              (sent through the fake service, which times the API's acknowledgement and
//              the reply). Every message must get an AI reply sent through the WhatsApp API.
//   dashboard: the team's screens polling the inbox counts, the conversation list and the
//              overview figures while that happens.
//
// Prepare a business first (README.md, "Load tests"), then:
//   k6 run -e RATE=20 -e DURATION=2m e2e/load/whatsapp.js
//
// Thresholds encode the targets in docs/LAUNCH_CHECKLIST.md; k6 exits non-zero on a miss.
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const API = __ENV.API_URL || "http://127.0.0.1:8000";
const FAKES = __ENV.FAKES_URL || "http://127.0.0.1:9000";
const RATE = Number(__ENV.RATE || 20); // customer messages per second
const DASHBOARD_RATE = Number(__ENV.DASHBOARD_RATE || 10); // dashboard requests per second
const DURATION = __ENV.DURATION || "2m";
const DRAIN_SECONDS = Number(__ENV.DRAIN_SECONDS || 180);
const REPLY_P95_SECONDS = Number(__ENV.REPLY_P95_SECONDS || 10);
const target = JSON.parse(open("./.target.json"));

const webhookAck = new Trend("webhook_ack_ms", true);
const webhookRejected = new Counter("webhook_rejected");

export const options = {
  scenarios: {
    inbound: {
      executor: "constant-arrival-rate",
      exec: "inbound",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: Math.max(20, RATE * 2),
      maxVUs: Math.max(100, RATE * 10),
    },
    dashboard: {
      executor: "constant-arrival-rate",
      exec: "dashboard",
      rate: DASHBOARD_RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  thresholds: {
    // Meta retries a webhook that isn't answered quickly; answer well inside a second.
    webhook_ack_ms: ["p(95)<500", "p(99)<1000"],
    webhook_rejected: ["count==0"],
    "http_req_duration{scenario:dashboard}": ["p(95)<800"],
    "http_req_duration{name:inbox counts}": ["p(95)<800"],
    "http_req_duration{name:conversation list}": ["p(95)<800"],
    "http_req_duration{name:overview}": ["p(95)<800"],
    "http_req_duration{name:me}": ["p(95)<800"],
    "http_req_failed{scenario:dashboard}": ["rate<0.01"],
    checks: ["rate>0.99"],
  },
  teardownTimeout: `${DRAIN_SECONDS + 30}s`,
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
};

export function setup() {
  const reset = http.post(`${FAKES}/_control/stats-reset`);
  check(reset, { "stats reset": (r) => r.status === 200 });
}

export function inbound() {
  // A different customer each time, so every message starts its own conversation and
  // must get its own reply (messages from one customer within seconds are answered once).
  const waId = `91${Math.floor(7000000000 + Math.random() * 2999999999)}`;
  const r = http.post(
    `${FAKES}/_control/inbound`,
    JSON.stringify({ text: "Hi, are you open on Saturday?", wa_id: waId, name: "Load Test Customer" }),
    { headers: { "Content-Type": "application/json" }, tags: { name: "inbound" } },
  );
  const ok = check(r, { "webhook accepted": (res) => res.status === 200 && res.json("status") === 200 });
  if (ok) webhookAck.add(r.json("ack_ms"));
  else webhookRejected.add(1);
}

const READS = [
  ["/api/v1/conversations/counts", "inbox counts"],
  ["/api/v1/conversations?limit=25", "conversation list"],
  ["/api/v1/analytics/overview?days=7", "overview"],
  ["/api/v1/auth/me", "me"],
];

export function dashboard() {
  const [path, name] = READS[Math.floor(Math.random() * READS.length)];
  const r = http.get(`${API}${path}`, {
    headers: { Authorization: `Bearer ${target.token}`, "X-Requested-With": "XploWhatsApp" },
    tags: { name },
  });
  check(r, { "dashboard read ok": (res) => res.status === 200 });
}

// After the traffic stops, wait for the workers to finish and check every customer got
// an answer. The time each one waited is measured by the fake service.
export function teardown() {
  let stats;
  for (let waited = 0; waited <= DRAIN_SECONDS; waited += 2) {
    stats = http.get(`${FAKES}/_control/stats`).json();
    if (stats.waiting === 0) break;
    sleep(2);
  }
  console.log(`AI replies: ${JSON.stringify(stats, null, 2)}`);
  if (stats.waiting !== 0) {
    throw new Error(`${stats.waiting} of ${stats.inbound} customers had no reply after ${DRAIN_SECONDS}s`);
  }
  // Customers wait at most this long for the reply to leave (the AI provider's own time
  // is near zero here; add its latency for production expectations).
  if (stats.reply_seconds && stats.reply_seconds.p95 > REPLY_P95_SECONDS) {
    throw new Error(`95% of replies took up to ${stats.reply_seconds.p95}s (target ${REPLY_P95_SECONDS}s)`);
  }
}
