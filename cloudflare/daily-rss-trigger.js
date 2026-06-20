const GITHUB_API_VERSION = "2022-11-28";
const WORKFLOW_FILE = "daily-rss-publish.yml";
const REPO_OWNER = "ZhangEx18";
const REPO_NAME = "us_politics_news";
const REF = "main";

async function triggerDailyPublish(env) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is not configured");
  }

  const response = await fetch(
    `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "User-Agent": "us-politics-news-daily-rss-trigger",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
      },
      body: JSON.stringify({ ref: REF }),
    },
  );

  if (response.status !== 204) {
    const body = await response.text();
    throw new Error(`GitHub workflow dispatch failed: ${response.status} ${body}`);
  }
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(triggerDailyPublish(env));
  },

  async fetch(_request, env) {
    await triggerDailyPublish(env);
    return new Response("Daily RSS publish triggered\n", { status: 202 });
  },
};
