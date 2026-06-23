import assert from "node:assert/strict";
import test from "node:test";

import worker, {
  resolveSchedules,
  getBuiltinManifest,
  isBeijingMonthStart,
} from "./daily-rss-trigger.js";

const WORKFLOW = "publish-product.yml";

// ── helper ──

function setupFetch() {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(null, { status: 204 });
  };
  return {
    calls,
    restore() { globalThis.fetch = original; },
  };
}

function setupCtx() {
  const promises = [];
  return {
    ctx: { waitUntil(p) { promises.push(p); } },
    promises,
  };
}

function setupDate(iso) {
  const fake = new Date(iso);
  const Original = globalThis.Date;
  class Mock extends Original {
    constructor(...args) {
      return args.length === 0 ? new Original(fake) : new Original(...args);
    }
    static now() { return fake.getTime(); }
  }
  globalThis.Date = Mock;
  return { restore() { globalThis.Date = Original; } };
}

function inputsFromCall(call) {
  return JSON.parse(call.options.body).inputs;
}

// ── dispatch 测试 ──

test("news daily dispatches publish-product.yml with correct inputs", async () => {
  const fetch = setupFetch();
  const { ctx, promises } = setupCtx();
  try {
    await worker.scheduled({ cron: "30 23 * * *" }, { GITHUB_TOKEN: "tok" }, ctx);
    assert.equal(promises.length, 1);
    await Promise.all(promises);

    assert.equal(fetch.calls.length, 1);
    assert.ok(fetch.calls[0].url.endsWith(`/workflows/${WORKFLOW}/dispatches`));
    const inputs = inputsFromCall(fetch.calls[0]);
    assert.equal(inputs.product_key, "news");
    assert.equal(inputs.report_type, "daily");
  } finally {
    fetch.restore();
  }
});

test("news weekly dispatches publish-product.yml with correct inputs", async () => {
  const fetch = setupFetch();
  const { ctx, promises } = setupCtx();
  try {
    await worker.scheduled({ cron: "35 23 * * 0" }, { GITHUB_TOKEN: "tok" }, ctx);
    assert.equal(promises.length, 1);
    await Promise.all(promises);

    assert.equal(fetch.calls.length, 1);
    const inputs = inputsFromCall(fetch.calls[0]);
    assert.equal(inputs.product_key, "news");
    assert.equal(inputs.report_type, "weekly");
  } finally {
    fetch.restore();
  }
});

test("news monthly dispatches on Beijing month start", async () => {
  const fetch = setupFetch();
  const date = setupDate("2026-07-31T23:40:00.000Z"); // 北京时间 8 月 1 日
  const { ctx, promises } = setupCtx();
  try {
    await worker.scheduled({ cron: "40 23 28-31 * *" }, { GITHUB_TOKEN: "tok" }, ctx);
    assert.equal(promises.length, 1);
    await Promise.all(promises);

    assert.equal(fetch.calls.length, 1);
    const inputs = inputsFromCall(fetch.calls[0]);
    assert.equal(inputs.product_key, "news");
    assert.equal(inputs.report_type, "monthly");
  } finally {
    fetch.restore();
    date.restore();
  }
});

test("news monthly skips dispatch before Beijing month start", async () => {
  const fetch = setupFetch();
  const date = setupDate("2026-07-30T23:40:00.000Z"); // 北京时间 7 月 31 日
  const { ctx } = setupCtx();
  try {
    await worker.scheduled({ cron: "40 23 28-31 * *" }, { GITHUB_TOKEN: "tok" }, ctx);
    assert.equal(fetch.calls.length, 0);
  } finally {
    fetch.restore();
    date.restore();
  }
});

test("algorithms daily dispatches publish-product.yml with correct inputs", async () => {
  const fetch = setupFetch();
  const { ctx, promises } = setupCtx();
  try {
    await worker.scheduled({ cron: "45 23 * * *" }, { GITHUB_TOKEN: "tok" }, ctx);
    assert.equal(promises.length, 1);
    await Promise.all(promises);

    assert.equal(fetch.calls.length, 1);
    const inputs = inputsFromCall(fetch.calls[0]);
    assert.equal(inputs.product_key, "algorithms");
    assert.equal(inputs.report_type, "daily");
  } finally {
    fetch.restore();
  }
});

test("unmatched cron does not dispatch", async () => {
  const fetch = setupFetch();
  const { ctx } = setupCtx();
  try {
    await worker.scheduled({ cron: "0 0 1 1 *" }, { GITHUB_TOKEN: "tok" }, ctx);
    assert.equal(fetch.calls.length, 0);
  } finally {
    fetch.restore();
  }
});

test("fetch endpoint returns 404", async () => {
  const resp = await worker.fetch(new Request("https://example.com"), { GITHUB_TOKEN: "tok" });
  assert.equal(resp.status, 404);
  assert.equal(await resp.text(), "Not Found\n");
});

test("dispatch fails when GitHub rejects", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response("bad credentials", { status: 401 });
  const { ctx, promises } = setupCtx();
  try {
    await worker.scheduled({ cron: "30 23 * * *" }, { GITHUB_TOKEN: "bad" }, ctx);
    assert.equal(promises.length, 1);
    await assert.rejects(
      () => Promise.all(promises),
      /GitHub workflow dispatch failed for publish-product\.yml: 401/,
    );
  } finally {
    globalThis.fetch = original;
  }
});

// ── resolveSchedules 单元测试 ──

test("resolveSchedules matches correct cron", () => {
  const manifest = getBuiltinManifest();
  const result = resolveSchedules({ cron: "30 23 * * *" }, manifest);
  assert.equal(result.length, 1);
  assert.equal(result[0].product_key, "news");
  assert.equal(result[0].report_type, "daily");
});

test("resolveSchedules returns empty for unknown cron", () => {
  const manifest = getBuiltinManifest();
  const result = resolveSchedules({ cron: "0 0 1 1 *" }, manifest);
  assert.equal(result.length, 0);
});

test("resolveSchedules monthly skips when not Beijing month start", () => {
  const manifest = getBuiltinManifest();
  const notMonthStart = new Date("2026-07-30T23:40:00.000Z");
  const result = resolveSchedules({ cron: "40 23 28-31 * *" }, manifest, notMonthStart);
  assert.equal(result.length, 0);
});

test("resolveSchedules monthly matches on Beijing month start", () => {
  const manifest = getBuiltinManifest();
  const monthStart = new Date("2026-07-31T23:40:00.000Z");
  const result = resolveSchedules({ cron: "40 23 28-31 * *" }, manifest, monthStart);
  assert.equal(result.length, 1);
  assert.equal(result[0].product_key, "news");
  assert.equal(result[0].report_type, "monthly");
});

test("isBeijingMonthStart returns true on Beijing 1st", () => {
  assert.ok(isBeijingMonthStart(new Date("2026-07-31T23:40:00.000Z")));
});

test("isBeijingMonthStart returns false before Beijing 1st", () => {
  assert.ok(!isBeijingMonthStart(new Date("2026-07-30T23:40:00.000Z")));
});
