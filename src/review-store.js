export function storageConfig(env) {
  const root = env.PRIVATE_RESOURCE_ROOT?.trim() || "/private";
  const bucket = env.OSS_CONTENT_BUCKET?.trim();
  const region = env.OSS_CONTENT_REGION?.trim()?.replace(/^oss-/, "");
  if (!bucket || !region) throw new Error("REVIEW_OSS_CONFIG_REQUIRED");
  if (!/^\/[^/]+(?:\/[^/]+)*$/.test(root) || root.includes("..") ||
      !/^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$/.test(bucket) || !/^[a-z]{2,3}-[a-z][a-z0-9-]+$/.test(region)) {
    throw new Error("REVIEW_OSS_CONFIG_INVALID");
  }
  const endpoint = `https://oss-${region}.aliyuncs.com`;
  if (env.OSS_CONTENT_ENDPOINT?.trim() && env.OSS_CONTENT_ENDPOINT.trim().replace(/\/$/, "") !== endpoint) {
    throw new Error("REVIEW_OSS_PUBLIC_ENDPOINT_REQUIRED");
  }
  return { root, bucket, region: `oss-${region}`, endpoint };
}

const status = error => error?.status ?? error?.statusCode ?? error?.res?.status;
const nameOf = path => path.replace(/^\//, "");

export async function createReviewStore(config, credentials, OSSClient) {
  if (![credentials?.accessKeyId, credentials?.accessKeySecret, credentials?.securityToken].every(Boolean)) {
    throw new Error("REVIEW_ROLE_REQUIRED");
  }
  const Client = OSSClient || (await import("ali-oss")).default;
  const oss = new Client({
    bucket: config.bucket, region: config.region, endpoint: config.endpoint, secure: true, authorizationV4: true,
    accessKeyId: credentials.accessKeyId, accessKeySecret: credentials.accessKeySecret, stsToken: credentials.securityToken,
    timeout: 5_000, retryMax: 0,
  });
  return {
    async listJsonPaths(pathPrefix, { cursor, limit }) {
      const prefix = nameOf(pathPrefix);
      const result = await oss.listV2({ prefix, "max-keys": limit, continuationToken: cursor });
      return {
        paths: (result.objects || []).map(item => item.name).filter(name => name.startsWith(prefix) && name.endsWith(".json")).map(name => `/${name}`),
        nextCursor: result.isTruncated ? result.nextContinuationToken : null,
      };
    },
    async readJson(path, { missing } = {}) {
      try {
        const result = await oss.get(nameOf(path));
        return JSON.parse(Buffer.from(result.content).toString("utf8"));
      } catch (error) {
        if (missing !== undefined && (error?.code === "NoSuchKey" || status(error) === 404)) return structuredClone(missing);
        throw error;
      }
    },
    async putJsonOnce(path, value) {
      try {
        await oss.put(nameOf(path), Buffer.from(`${JSON.stringify(value, null, 2)}\n`), {
          mime: "application/json; charset=utf-8",
          headers: { "x-oss-forbid-overwrite": "true" },
        });
      } catch (error) {
        if (error?.code !== "FileAlreadyExists" && status(error) !== 409) throw error;
        if (JSON.stringify(await this.readJson(path)) !== JSON.stringify(value)) {
          throw Object.assign(new Error("RESOURCE_EXISTS", { cause: error }), { code: "RESOURCE_EXISTS", statusCode: 409 });
        }
      }
    },
    async deletePaths(paths) {
      for (const path of new Set(paths)) await oss.delete(nameOf(path));
    },
  };
}
