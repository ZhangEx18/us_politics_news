const GITHUB_API_VERSION = "2022-11-28";
const REPO_OWNER = "ZhangEx18";
const REPO_NAME = "us_politics_news";
const REF = "main";
const BEIJING_TIMEZONE = "Asia/Shanghai";

async function triggerWorkflow(env, workflowFile, inputs = {}) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is not configured");
  }

  const body = { ref: REF };
  if (Object.keys(inputs).length > 0) {
    body.inputs = inputs;
  }

  const response = await fetch(
    `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${workflowFile}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        "User-Agent": "us-politics-news-trigger",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
      },
      body: JSON.stringify(body),
    },
  );

  if (response.status !== 204) {
    const respBody = await response.text();
    throw new Error(`GitHub workflow dispatch failed for ${workflowFile}: ${response.status} ${respBody}`);
  }
}

function isBeijingMonthStart(now = new Date()) {
  const day = new Intl.DateTimeFormat("en-US", {
    timeZone: BEIJING_TIMEZONE,
    day: "numeric",
  }).format(now);
  return day === "1";
}

/**
 * 从 KV 或内置 manifest 读取调度配置。
 * 优先级：env.SCHEDULE_KV (KV) > 内置默认。
 */
function getScheduleManifest(env) {
  if (env.SCHEDULE_KV) {
    return env.SCHEDULE_KV.get("schedule-manifest", { type: "json" });
  }
  return Promise.resolve(getBuiltinManifest());
}

function getBuiltinManifest() {
  return {
    version: 1,
    schedules: [
      {
        product_key: "news",
        report_type: "daily",
        workflow: "publish-product.yml",
        cron: "30 23 * * *",
        inputs: { product_key: "news", report_type: "daily", digest_only: "false" },
      },
      {
        product_key: "news",
        report_type: "weekly",
        workflow: "publish-product.yml",
        cron: "35 23 * * 0",
        inputs: { product_key: "news", report_type: "weekly" },
      },
      {
        product_key: "news",
        report_type: "monthly",
        workflow: "publish-product.yml",
        cron: "40 23 28-31 * *",
        inputs: { product_key: "news", report_type: "monthly" },
      },
      {
        product_key: "algorithms",
        report_type: "daily",
        workflow: "publish-product.yml",
        cron: "45 23 * * *",
        inputs: { product_key: "algorithms", report_type: "daily" },
      },
    ],
  };
}

/**
 * 按 cron 匹配调度项。
 * 月报特殊处理：仅在北京时间月初 1 日触发。
 * 未匹配的 cron 静默返回空数组（不 dispatch）。
 */
function resolveSchedules(event, manifest, now = new Date()) {
  const matched = [];
  for (const schedule of manifest.schedules || []) {
    if (schedule.cron !== event.cron) {
      continue;
    }
    if (schedule.report_type === "monthly" && !isBeijingMonthStart(now)) {
      continue;
    }
    matched.push(schedule);
  }
  return matched;
}

export default {
  async scheduled(event, env, ctx) {
    const manifest = await getScheduleManifest(env);
    const schedules = resolveSchedules(event, manifest);

    for (const schedule of schedules) {
      ctx.waitUntil(triggerWorkflow(env, schedule.workflow, schedule.inputs));
    }
  },

  async fetch() {
    return new Response("Not Found\n", { status: 404 });
  },
};

// 测试辅助导出
export { resolveSchedules, getBuiltinManifest, isBeijingMonthStart, triggerWorkflow };
