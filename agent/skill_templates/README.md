# 用户级技能模板（需自带 API Key / 私有 host）

本目录下的技能**不会**作为系统技能自动加载，需要用户自行安装到自己的技能仓库，
并使用**自己申请的 API Key 与私有 host**。多用户场景下，每个用户用各自凭据，互相隔离。

## 为什么放在这里
依赖第三方 API Key / 专属 host 的技能不适合在系统级"写死"一份凭据。
把它下放到用户仓库后：
- 系统默认只保留无需凭据的通用技能（calculator / datetime / chinese-counter /
  lunar-converter / document / web-fetch 等）；
- 用户用自己申请的 key/host 编写或安装技能，密钥按 `user_id` 加密隔离，互不可见。

## 安装方式（二选一）
1. 对话中让 Agent 调用 `install_skill_template("<模板名>")`，会自动复制到
   `agent/skills/user/{你的user_id}/<模板名>/` 并立即生效；
2. 手动把对应子目录复制到 `agent/skills/user/{你的user_id}/<技能名>/`。

## 使用前必须设置自己的密钥
安装后先通过 `set_user_secret` 写入你的凭据（仅对你自己可见），例如某需要 Key 的技能：

- `<skill>_api_host`：该技能的 API host
- `<skill>_api_key`：你的 API Key

技能脚本里的 `https://{secret:<skill>_api_host}/...&key={secret:<skill>_api_key}`
会在每次调用时，按**当前调用者 user_id** 解析成你设置的对应值。

> 也可不装模板、完全自己写：在你自己的技能目录里新建 SKILL.md + scripts，
> 把 key/host 直接写进 http_config 的 URL（内联方式），同样可用。
> 但用 `set_user_secret` 管理密钥更安全（加密存储、不落明文到脚本）。
