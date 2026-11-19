# Market signal review

独立的新加坡 `market-signal-review` 阿里云 FC 事件函数。它消费网站为 V4 研究写入的 OSS 队列，按 4h、12h 和 24h 窗口写入标记价格复盘与摘要。复盘算法和数据契约来自 [网站 V4 实现](https://github.com/jacofun/vite-shadcnui-blog/tree/76916c20e715c9a5e2189cdd3bb71293ca66032f/functions/private-auth)。

## 部署

`main` 的推送和手动运行会部署服务器代码。PR 运行测试、打包及入口检查。流水线使用 GitHub Secrets `ALIYUN_FC_ACCESS_KEY_ID` 与 `ALIYUN_FC_ACCESS_KEY_SECRET`，只把 ZIP 传给新加坡已存在的 `market-signal-review`。函数使用 Node.js 20 和 `index.handler`，启用公网访问，实例并发为 1 且超时至少 75 秒。流水线校验 FC 代码 CRC-64，并同步调用线上入口核对提交和运行时版本。

随后用 FC 执行角色列举生产 OSS 队列，检查币安标记价格 K 线接口，并创建或更新 `market-research-review-v4`，周期为 `@every 5m`，负载为 `drain-market-signal-reviews`。探针只读。实际复盘由定时触发器处理。

| 设置 | 用途 |
| --- | --- |
| `OSS_CONTENT_BUCKET` | 与网站私有鉴权函数相同的内容 Bucket |
| `OSS_CONTENT_REGION` | Bucket 地域，例如 `oss-cn-beijing` |
| `PRIVATE_RESOURCE_ROOT` | 网站私有资源根目录，默认 `/private` |
| `OSS_CONTENT_ENDPOINT` | 可选。默认推导 Bucket 地域的 HTTPS 公网端点 |
| `MARKET_REVIEW_ROLE_ARN` | 可选 GitHub Actions Variable。函数还没有角色时，将该已存在的角色绑定给函数 |

部署优先保留函数已有环境变量和角色。上表存储项也可通过 GitHub Actions Variables 补齐。如果 Bucket 或地域缺失，部署会尝试从北京的 `fc-blog-authenticator-web` 读取 Bucket、地域和根目录三个存储设置。跨地域 OSS 访问使用该地域的公网端点。缺少源函数读取权限时可在复盘函数中直接配置这三项。函数运行时使用 `context.credentials`。

部署身份需要目标函数上的 `fc:GetFunction`、`fc:UpdateFunction`、`fc:InvokeFunction`、`fc:ListTriggers`、`fc:CreateTrigger` 和 `fc:UpdateTrigger`。从网站继承缺失设置时，需要源函数的 `fc:GetFunction`。通过 Actions Variables 绑定执行角色时，部署身份还需该角色的 `ram:PassRole`。

函数执行角色须信任 FC，并获得以下 OSS 权限。以 `P=private/.trading/owner/market-signals` 为例，实际前缀使用网站配置。

| OSS 资源 | 权限 |
| --- | --- |
| Bucket | `oss:ListObjects`，`oss:Prefix` 限定为 `P/review-queue/` |
| `P/records/*` | `oss:GetObject` |
| `P/review-queue/*` | `oss:GetObject`、`oss:PutObject`、`oss:DeleteObject` |
| `P/reviews/*`、`P/review-index/*` | `oss:GetObject`、`oss:PutObject` |

存储沿用网站的 OSS 条件写入语义。启用或暂停版本控制的 Bucket 会忽略 `x-oss-forbid-overwrite`，应使用网站现有的、从未启用版本控制的归档 Bucket。

如果 Actions 输出了 `review_code_verified` 后报 `REVIEW_ROLE_REQUIRED`，代码已部署，需绑定执行角色并重跑工作流。出现 `REVIEW_OSS_CONFIG_REQUIRED` 时补存储设置，出现 `REVIEW_OSS_LIST_FAILED` 时核对 Bucket 地域与角色的队列列举权限。日志出现 `review_timer_verified` 表示 FC 已回读到启用的五分钟触发器。

## 本地验证

```sh
npm ci --ignore-scripts
npm test
python -m pip install -r scripts/requirements.txt
python -m unittest discover -s test -p 'test_*.py' -v
python scripts/package.py
```

参考 [FC 定时触发器](https://www.alibabacloud.com/help/en/functioncompute/time-triggers)、[Node.js 事件处理器](https://www.alibabacloud.com/help/en/functioncompute/request-handlers) 和 [OSS 初始化](https://www.alibabacloud.com/help/en/oss/initialization-10)。
