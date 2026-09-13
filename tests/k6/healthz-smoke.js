import http from 'k6/http';
import { check } from 'k6';

// Minimal smoke test for the `integration` CI job (see
// openspec/changes/add-ci-pipeline/design.md Decision 9 and 11):
// hit /healthz/ once and assert it responds 200. Scope is deliberately
// narrow — "server boots, routing works" — not a load test.
export const options = {
  vus: 1,
  iterations: 1,
};

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';

export default function () {
  const res = http.get(`${BASE_URL}/healthz/`);
  check(res, {
    'status is 200': (r) => r.status === 200,
  });
}
