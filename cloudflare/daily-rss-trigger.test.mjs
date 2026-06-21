import assert from "node:assert/strict";
import test from "node:test";

import worker from "./daily-rss-trigger.js";

test("scheduled triggers the Daily RSS Publish workflow dispatch", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(null, { status: 204 });
  };

  const waitUntilPromises = [];
  const ctx = {
    waitUntil(promise) {
      waitUntilPromises.push(promise);
    },
  };

  try {
    await worker.scheduled({}, { GITHUB_TOKEN: "github_pat_test" }, ctx);
    assert.equal(waitUntilPromises.length, 1);
    await Promise.all(waitUntilPromises);

    assert.equal(calls.length, 1);
    assert.equal(
      calls[0].url,
      "https://api.github.com/repos/ZhangEx18/us_politics_news/actions/workflows/daily-rss-publish.yml/dispatches",
    );
    assert.equal(calls[0].options.method, "POST");
    assert.equal(calls[0].options.headers.Authorization, "Bearer github_pat_test");
    assert.deepEqual(JSON.parse(calls[0].options.body), { ref: "main" });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("fetch endpoint is disabled", async () => {
  const response = await worker.fetch(new Request("https://example.com"), {
    GITHUB_TOKEN: "github_pat_test",
  });

  assert.equal(response.status, 404);
  assert.equal(await response.text(), "Not Found\n");
});

test("scheduled fails when GitHub rejects workflow dispatch", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("bad credentials", { status: 401 });

  const waitUntilPromises = [];
  const ctx = {
    waitUntil(promise) {
      waitUntilPromises.push(promise);
    },
  };

  try {
    await worker.scheduled({}, { GITHUB_TOKEN: "bad" }, ctx);
    assert.equal(waitUntilPromises.length, 1);
    await assert.rejects(
      () => Promise.all(waitUntilPromises),
      /GitHub workflow dispatch failed: 401 bad credentials/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
