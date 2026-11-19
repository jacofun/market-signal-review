import { createReviewStore, storageConfig } from "./src/review-store.js";
import { drainSignalReviews, signalHistoryRoot } from "./src/market-signal-review.js";
import { SOURCE_COMMIT } from "./version.js";

const SERVICE = "market-signal-review";
const TIMER_NAME = "market-research-review-v4";
const TIMER_MESSAGE = "drain-market-signal-reviews";
const log = data => console.log(JSON.stringify(data));
const setupErrors = new Set(["REVIEW_ROLE_REQUIRED", "REVIEW_OSS_CONFIG_REQUIRED", "REVIEW_OSS_CONFIG_INVALID", "REVIEW_OSS_PUBLIC_ENDPOINT_REQUIRED"]);
const ossCodes = new Set(["AccessDenied", "NoSuchBucket", "InvalidAccessKeyId", "InvalidSecurityToken", "SecurityTokenExpired", "SignatureDoesNotMatch"]);

function failureDetails(error) {
  const status = error?.status ?? error?.statusCode;
  return {
    ...(ossCodes.has(error?.code) ? { ossCode: error.code } : {}),
    ...(Number.isInteger(status) && status >= 100 && status < 600 ? { httpStatus: status } : {}),
  };
}

async function checkMarketEndpoint(fetchImpl) {
  const response = await fetchImpl("https://fapi.binance.com/fapi/v1/markPriceKlines?symbol=BTCUSDT&interval=1m&limit=1", { redirect: "error", signal: AbortSignal.timeout(8_000) });
  await response.body?.cancel().catch(() => {});
  if (!response.ok) throw Object.assign(new Error("REVIEW_MARKET_UNREACHABLE"), { status: response.status });
}

export function createHandler({ env = process.env, version = "development", createStore = createReviewStore,
  drain = drainSignalReviews, fetchImpl = fetch, logger = log } = {}) {
  const versionInfo = { service: SERVICE, codeVersion: version, nodeMajor: Number(process.versions.node.split(".")[0]) };
  return async (event, context) => {
    let input;
    try { input = JSON.parse(Buffer.isBuffer(event) ? event.toString("utf8") : event); }
    catch { throw new Error("INVALID_REVIEW_TIMER_EVENT"); }
    if (input?.action === "health") return { status: "ok", ...versionInfo };
    const verifying = input?.action === "verify_dependencies";
    if (!verifying && (input?.triggerName !== TIMER_NAME || input?.payload !== TIMER_MESSAGE || !Number.isFinite(Date.parse(input?.triggerTime)))) {
      throw new Error("INVALID_REVIEW_TIMER_EVENT");
    }

    let stage = "setup";
    try {
      const config = storageConfig(env);
      const root = signalHistoryRoot(config.root);
      const store = await createStore(config, context?.credentials);
      if (verifying) {
        stage = "oss_list";
        await store.listJsonPaths(`${root}/review-queue/`, { limit: 1 });
        stage = "market_endpoint";
        await checkMarketEndpoint(fetchImpl);
        return { status: "ok", ...versionInfo, ossList: true, marketReachable: true };
      }
      stage = "drain";
      const results = await drain({ store, root, signal: AbortSignal.timeout(45_000), log: logger });
      logger({ event: "market_review_timer_completed", ...versionInfo, ...results });
      return results;
    } catch (error) {
      const code = setupErrors.has(error?.message) ? error.message : stage === "market_endpoint"
        ? "REVIEW_MARKET_UNREACHABLE" : stage === "oss_list" ? "REVIEW_OSS_LIST_FAILED" : "REVIEW_QUEUE_OR_CONFIGURATION_FAILED";
      const details = failureDetails(error);
      logger({ event: verifying ? "market_review_dependency_check_failed" : "market_review_timer_failed", stage, code, ...details });
      if (verifying) return { status: "blocked", ...versionInfo, stage, code, ...details };
      throw new Error(code);
    }
  };
}

export const handler = createHandler({ version: SOURCE_COMMIT });
