# 司机社自动签到

每天由 [cron-job.org](https://cron-job.org/) 定时调用 GitHub API，触发 GitHub Actions 在 Ubuntu Runner 中执行司机社签到。

默认不使用 GitHub Actions 自带的 `schedule`，只保留手动/API 触发入口，计划执行时间为北京时间每天 **09:00**。

## 功能

- 从官方发布页和官方 Telegram 频道自动提取最新地址；
- 只接受 HTTPS 地址，并验证 TLS、最终跳转地址和论坛页面特征；
- 验证完成前不会向候选网站提交账号密码；
- 发布页普通请求失败时自动切换为无头 Chrome 获取；
- 当前入口登录失败时，自动切换其他已通过安全验证的官方入口；
- 支持图形验证码 OCR；
- 账号和密码仅从 GitHub Actions Secret 读取；
- 日志隐藏账号，不打印密码、Cookie 或页面源码；
- 地址发现、登录或签到失败时，GitHub Actions 会明确显示失败。

## 一、配置 GitHub Secret

进入仓库：

`Settings` → `Secrets and variables` → `Actions` → `Secrets` → `New repository secret`

添加：

| Name | Value |
| --- | --- |
| `XSIJISHE` | `你的账号&你的密码` |

示例格式仅用于说明：

```text
example_user&example_password
```

不要把真实账号密码写进代码、README、Issue 或 Actions 日志。

密码中可以包含 `&`；程序只把第一个 `&` 当作账号和密码的分隔符。

### 可选：临时指定论坛地址

正常情况下不需要配置。只有自动发布源暂时无法访问时，才进入：

`Settings` → `Secrets and variables` → `Actions` → `Variables`

添加变量：

| Name | Value |
| --- | --- |
| `FORUM_URL` | 已确认可信的司机社 HTTPS 根地址 |

即使手动指定，程序仍会验证 HTTPS 和论坛页面特征。

## 二、先手动测试 GitHub Actions

1. 打开仓库的 `Actions` 页面。
2. 选择“司机社每日签到”。
3. 点击 `Run workflow`。
4. 选择 `main` 后确认运行。
5. 等待任务结束，确认地址发现、登录和签到步骤均成功。

只有手动测试成功后，再配置 cron-job.org。

## 三、创建 GitHub Fine-grained Token

打开 GitHub：

`Settings` → `Developer settings` → `Personal access tokens` → `Fine-grained tokens`

建议配置：

| 项目 | 值 |
| --- | --- |
| Token name | `cron-job-sijishe` |
| Expiration | 按需选择，建议设置有效期并定期轮换 |
| Repository access | `Only select repositories` |
| Repository | `sufeifan123/Sijishe` |
| Repository permissions | `Actions: Read and write` |

生成后立即复制 Token。Token 只保存到 cron-job.org，不要添加到代码或提交到仓库。

## 四、配置 cron-job.org

登录 [cron-job.org](https://cron-job.org/)，创建一个 Cron Job。

### 基本配置

| 项目 | 值 |
| --- | --- |
| Title | `司机社每日签到` |
| URL | `https://api.github.com/repos/sufeifan123/Sijishe/actions/workflows/checkin.yml/dispatches` |
| Request method | `POST` |
| Time zone | `Asia/Shanghai` |
| Schedule | 每天 `09:00` |

### 请求头

| Header | Value |
| --- | --- |
| `Accept` | `application/vnd.github+json` |
| `Authorization` | `Bearer 你的Fine-grained Token` |
| `Content-Type` | `application/json` |
| `X-GitHub-Api-Version` | `2026-03-10` |

`Bearer` 后面必须有一个空格。

### 请求体

选择 Raw Body，填写：

```json
{"ref":"main"}
```

保存后使用 cron-job.org 的立即执行功能测试。GitHub API 返回 `204 No Content` 表示成功接收触发请求；随后应能在仓库的 Actions 页面看到新任务。

建议在 cron-job.org 中启用失败通知，并关闭响应正文保存。GitHub Token 等同于密码，泄漏后应立即撤销并重新生成。

## 地址安全策略

自动地址仅来自以下受信任来源：

- `https://47447.net/`
- `https://t.me/s/xsijishe`

候选地址必须同时满足：

1. 使用 HTTPS；
2. TLS 证书验证成功；
3. 跳转后的最终地址仍为 HTTPS；
4. 页面同时具有司机社品牌标记和 Discuz/签到技术特征。

如果没有地址通过验证，任务会失败，并且不会提交账号密码。

## 项目文件

| 文件 | 说明 |
| --- | --- |
| `sijishe.py` | 地址发现、登录和签到脚本 |
| `requirements.txt` | Python 依赖 |
| `.github/workflows/checkin.yml` | GitHub Actions 工作流 |
| `tests/test_sijishe.py` | 地址和配置解析测试 |
| `notify.py` | 原项目通知模块，当前工作流未启用 |

## 常见问题

### Actions 提示没有配置 XSIJISHE

检查 Secret 名称是否严格为 `XSIJISHE`，并确认它配置在当前仓库的 Actions Secrets 中。

### cron-job.org 返回 401

Token 无效、已过期，或 `Authorization` 请求头格式错误。应为 `Bearer`、一个空格、再加 Token。

### cron-job.org 返回 403

检查 Fine-grained Token 是否选择了本仓库，以及是否授予 `Actions: Read and write`。

### cron-job.org 返回 404

检查仓库名称、工作流文件名 `checkin.yml`，并确认工作流已经存在于默认分支 `main`。

### 自动地址发现失败

先查看 Actions 日志。如果两个官方发布源均临时不可访问，可以暂时配置 `FORUM_URL` Repository Variable；恢复后删除该变量即可重新启用自动发现。
