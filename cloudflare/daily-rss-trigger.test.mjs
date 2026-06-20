import assert from "node:assert/strict";
import test from "node:test";

import worker from "./daily-rss-trigger.js";

test("fetch triggers the Daily RSS Publish workflow dispatch", async () => {
  const calls = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return new Response(null, { status: 204 });
  };

  try {
    const response = await worker.fetch(new Request("https://example.com"), {
      GITHUB_TOKEN: "github_pat_test",
    });

    assert.equal(response.status, 202);
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

test("fetch fails when GitHub rejects workflow dispatch", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("bad credentials", { status: 401 });

  try {
    await assert.rejects(
      () => worker.fetch(new Request("https://example.com"), { GITHUB_TOKEN: "bad" }),
      /GitHub workflow dispatch failed: 401 bad credentials/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
