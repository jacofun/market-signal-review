import assert from "node:assert/strict";
import test from "node:test";
import { createHandler } from "../index.js";
import { createReviewStore, storageConfig } from "../src/review-store.js";

const event = payload => Buffer.from(JSON.stringify(payload));
const env = { OSS_CONTENT_BUCKET: "private-reviews", OSS_CONTENT_REGION: "oss-cn-beijing" };
const credentials = { accessKeyId: "id", accessKeySecret: "secret", securityToken: "token" };
const context = { credentials };
const timer = { triggerName: "market-research-review-v4", payload: "drain-market-signal-reviews", triggerTime: "2026-11-19T00:00:00Z" };

test("health returns a version without reading credentials or accessing OSS", async () => {
  const handler = createHandler({ env: {}, version: "sha", createStore: () => assert.fail("unexpected OSS access") });
  const health = await handler(event({ action: "health" }), {});
  assert.equal(health.codeVersion, "sha");
  assert.equal(health.service, "market-signal-review");
  assert.equal(health.status, "ok");
});

test("dependency probe checks the production prefix and market endpoint without draining", async () => {
  const handler = createHandler({
    env,
    createStore: (config, actual) => {
      assert.equal(config.endpoint, "https://oss-cn-beijing.aliyuncs.com");
      assert.equal(actual, credentials);
      return { listJsonPaths: async (prefix, options) => {
        assert.equal(prefix, "/private/.trading/owner/market-signals/review-queue/");
        assert.equal(options.limit, 1);
      } };
    },
    fetchImpl: async url => {
      assert.equal(new URL(url).pathname, "/fapi/v1/markPriceKlines");
      return new Response("[]");
    },
    drain: () => assert.fail("unexpected write"),
  });
  const result = await handler(event({ action: "verify_dependencies" }), context);
  assert.equal(result.status, "ok");
  assert.equal(result.ossList, true);
  assert.equal(result.marketReachable, true);
});

test("blocked probe emits bounded failure fields and distinguishes role, region and OSS permission", async () => {
  const logs = [];
  const handler = createHandler({ env, logger: entry => logs.push(entry) });
  assert.equal((await handler(event({ action: "verify_dependencies" }), {})).code, "REVIEW_ROLE_REQUIRED");
  const invalid = createHandler({ env: { ...env, OSS_CONTENT_ENDPOINT: "https://oss-cn-beijing-internal.aliyuncs.com" }, logger: entry => logs.push(entry) });
  assert.equal((await invalid(event({ action: "verify_dependencies" }), context)).code, "REVIEW_OSS_PUBLIC_ENDPOINT_REQUIRED");
  const forbidden = createHandler({ env, logger: entry => logs.push(entry), createStore: () => ({
    listJsonPaths: () => { throw Object.assign(new Error("private bucket information"), { code: "AccessDenied", status: 403 }); },
  }) });
  const check = await forbidden(event({ action: "verify_dependencies" }), context);
  assert.equal(check.code, "REVIEW_OSS_LIST_FAILED");
  assert.equal(check.ossCode, "AccessDenied");
  assert.equal(JSON.stringify(logs).includes("private bucket information"), false);
});

test("timer rejects non-timer events and drains a valid FC event using temporary credentials", async () => {
  const logs = [];
  let drained = 0;
  const handler = createHandler({
    env: { ...env, PRIVATE_RESOURCE_ROOT: "/example" }, createStore: (_, auth) => {
      assert.equal(auth, credentials);
      return {};
    },
    drain: async ({ root, signal }) => {
      assert.equal(root, "/example/.trading/owner/market-signals");
      assert.equal(signal.aborted, false);
      drained++;
      return { completed: 1, insufficient: 0, failed: 0, retried: 0 };
    },
    logger: entry => logs.push(entry),
  });
  await assert.rejects(handler(event({ ...timer, payload: "wrong" }), context), /INVALID_REVIEW_TIMER_EVENT/);
  assert.equal((await handler(event(timer), context)).completed, 1);
  assert.equal(drained, 1);
  assert.equal(logs[0].event, "market_review_timer_completed");
});

test("OSS storage uses conditional writes, normalizes list paths and preserves conflicting JSON", async () => {
  const objects = new Map();
  class Store {
    constructor(options) { assert.equal(options.authorizationV4, true); assert.equal(options.stsToken, "token"); }
    async listV2({ prefix }) { return { objects: [...objects.keys()].map(name => ({ name })), isTruncated: false, prefix }; }
    async get(name) {
      if (!objects.has(name)) throw Object.assign(new Error(), { code: "NoSuchKey" });
      return { content: objects.get(name) };
    }
    async put(name, content, options) {
      assert.equal(options.headers["x-oss-forbid-overwrite"], "true");
      if (objects.has(name)) throw Object.assign(new Error(), { status: 409 });
      objects.set(name, content);
    }
    async delete(name) { objects.delete(name); }
  }
  const store = await createReviewStore(storageConfig(env), credentials, Store);
  await store.putJsonOnce("/private/test.json", { n: 1 });
  await store.putJsonOnce("/private/test.json", { n: 1 });
  await assert.rejects(store.putJsonOnce("/private/test.json", { n: 2 }), { code: "RESOURCE_EXISTS" });
  assert.deepEqual(await store.listJsonPaths("/private/", { limit: 5 }), { paths: ["/private/test.json"], nextCursor: null });
  await store.deletePaths(["/private/test.json", "/private/test.json"]);
  assert.equal(await store.readJson("/private/test.json", { missing: null }), null);
});
