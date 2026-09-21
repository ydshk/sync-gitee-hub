# sync-gitee-hub

批量将 GitHub 仓库自动同步到 Gitee 并翻译 markdown 文档为中文的集中式工作流。

## 工作原理

```
GitHub (多owner)  ──┐
                     │  GitHub Actions (定时/手动/推送触发)
                     │  ──遍历 repos.txt──┐
                     │                      ▼
                     │         逐个仓库：clone + 增量翻译 + 推送
                     │                      │
                     └──────────────► Gitee (固定owner: home-assistant-xin)
```

- **源端（GitHub）**：每个仓库的 owner 可以不同，在 `repos.txt` 中按 `owner/repo` 指定
- **目标端（Gitee）**：所有仓库都同步到固定账号 `home-assistant-xin` 下
- **翻译**：用 DeepLF API 将 `README.md` 等文件翻译为中文，术语表保护专有名词不译

### 增量翻译流程

1. Clone GitHub 仓库（英文原版）+ Clone6.tee 仓库（中文版，浅拷贝）
2. 逐文件 SHA 对比：
   - 所有文件 SHA 未变 → 写 `.all_skipped` 标记 → 跳过推送
   - 有变动 → 变动文件调 API 翻译，未变动文件从 Gitee 恢复中文版
3. 后处理：还原占位符 + 格式修复（中英混排空格、链接修复等）
4. rsync 同步到 Gitee clone 目录 → commit → 非 force push（保留 Gitee 提交历史）

## 文件结构

```
sync-gitee-hub/
├── .github/workflows/
│   └── sync-multi-repos.yml       # 同步+翻译工作流
├── scripts/
│   ├── translate.py               # 翻译主逻辑（占位符保护、段落拆分、文件遍历）
│   └── postprocess.py             # 后处理（还原占位符 + 格式修复）
├── repos.txt                      # 仓库清单（owner/repo 或 owner/repo => gitee_name）
├── glossary.txt                   # 术语表（专有名词保持英文不译）
├── translation-map.txt            # 映射表（英文词 => 中文译法）
├── translate-files.txt            # 翻译目标文件名（README.md、DOCS.md、CHANGELOG.md）
├── cache/                         # 翻译缓存（段落级 + 逐文件 SHA）
│   └── <gitee_repo>/
│       ├── cache.json            B  殡落缓存（key=保护后文本，value=API 返回）
│       └── file_shas.json         # 逐文件 SHA 字典
└── README.md                      # 本文档
```

> `translation-service`（`ydshk/translation-service`）为独立仓库，提供翻译 API 调用 + 缓存管理。

## 配置步骤

### 1. 创建"同步中心"仓库

在 GitHub 上新建一个仓库（例如 `sync-gitee-hub`），将本项目所有文件推送上去。

### 2. 配置 Secrets

进入同步中心仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

| Name | 是否必填 | 说明 | 获取方式 |
|------|---------|------|---------|
| `GITEE_USERNAME` | ✅ 必选 | Gitee 登录用户名 | Gitee 个人主页 |
| `GITEE_TOKEN` | ✅ 必选 | Gitee 私人令牌 | Gitee → 头像 → 设置 → 私人令牌，勾选 `projects` 权限 |
| `DEEPL_API_KEY` | ✅ 必选 | DeepL API Free 密钥 | https://www.deepl.com/pro-api 需 DeepL API Free 账户，50 万字符/月 |
| `GH_PAT` | ⚪ 可选 | GitHub Personal Access Token | GitHub → Settings → Developer settings → PAT (classic)，勾选 `repo` 权限 |

> 💡 **公开仓库无需配置 `GH_PAT`**：若 `repos.txt` 中全部是公开仓库，工作流会自动使用 GitHub 内置的 `GITHUB_TOKEN` 拉取代码。

### 3. 在 Gitee 预建空仓库

为 `repos.txt` 中每个条目，按其对应的 **Gitee 仓库名**在 https://gitee.com/home-assistant-xin 下创建对应空仓库：

- 简单格式 `owner/repo`：Gitee 仓库名 = `repo`
- 映射格式 `owner/repo => gitee_name`：Gitee 仓库名 = `gitee_name`
- **不要**勾选"使用 Readme 初始化仓库"

### 4. 编辑仓库清单

打开 [`repos.txt`](repos.txt)，按格式添加要同步的仓库。提交并推送后，工作流会自动触发首次同步。

## repos.txt 格式规范

### 支持的两种格式

| 格式 | 写法 | Gitee 端仓库名 |
|------|------|----------------|
| 简单格式 | `owner/repo` | `repo`（默认两端一致） |
| 映射格式 | `owner/repo => gitee_name` | `gitee_name`（手动指定） |

### 校验规则

| 规则 | 说明 |
|------|------|
| 必须包含且仅包含一个 `/` | GitHub 端 `owner/repo` 中只能有一个斜杠 |
| `=>` 后的 Gitee 名 | 仅允许字母数字、点 `.`、下划线 `_`、连字符 `-` |
| 注释行 | 以 `#` 开头的行会被忽略 |
| 空行 | 自动忽略 |
| **Gitee 仓库名不可重复** | 多个条目映射到同一 Gitee 仓库时会报错 |

## 触发方式

| 触发方式 | 说明 | SHA 跳过 | 段落缓存 |
|---------|------|----------|----------|
| **定时同步** | 每天北京时间 00:00 自动同步全部仓库 | ✅ 生效 | ✅ 生效 |
| **手动触发** | Actions → Run workflow，可指定仓库或强制重译 | ✅ 生效（除非 force） | ✅ 生效（除非 force） |
| **配置变更推送** | 修改 workflow/repos9. repos.txt/glossary.txt/translation-map.txt/scripts/ 后推送 | ❌ 跳过（处理所有文件） | ✅ 生效 |

> **配置变更推送**时传 `--no-sha-skip`：处理所有文件但段落缓存命中不调 API，只重新应用后处理。改了 `postprocess.py`/`glossary.txt` 后推送即可生效，无需消耗 API 额度。

### 手动触发选项

- **`repos`**：指定要同步的仓库（逗号分隔，格式 `owner/repo` 或 `owner/repo=>gitee_name`），留空则同步全部
- **`force_retranslate`**：强制重新翻译（忽略段落缓存，所有 README 重新调用翻译 API）

## 翻译配置

### glossary.txt — 术语表

每行一个专有名词，翻译时保持英文不译。按长度降序匹配（避免子串问题）。

```
UniFi Network Application
Music Assistant Server
Actual Budget
AdGuard Home
Glances
Grafana
ESPHome
```

> ⚠️ 本文件有条目时，`translate.py` 中的 `DEFAULT_GLOSSARY` 不生效，需把所有术语都列在这里。

### translation-map.txt — 映射表

指定英文词的中文译法，格式 `英文 => 中文`：

```
Releases => 版本发布
procedure => 步骤
stream => 流
```

### translate-files.txt — 翻译目标文件

指定哪些文件名需要翻译（不区分大小写）：

```
README.md
DOCS.md
CHANGELOG.md
```

## 缓存机制

### 两层缓存

1. **逐文件 SHA**（`file_shas.json`）：决定是否跳过整个文件
2. **段落级缓存**（`cache.json`）：决定是否调 DeepL API（key=保护后文本，value=API 返回）

### 缓存失效场景

| 改动 | 是否需要删缓存 | 原因 |
|------|---------------|------|
| 改 `postprocess.py` | ❌ 不需要 | 后处理每次重新应用，推送后自动生效 |
| 改 `glossary.txt` | ✅ 需删缓存 | 保护后文本变 → 缓存 key 变 → 不命中旧缓存 → 调 API 重译 |
| 改 `translation-map.txt` | ✅ 需删缓存 | 同上 |
| 改 `translate.py` 占位符逻辑 | ✅ 需删缓存 | 占位符格式变 → 缓存不兼容 |

> 删缓存时只需删对应仓库的 `cache/<repo>/` 目录，不影响其他仓库。

## 注意事项

### 1. Gitee 提交历史

- **已存在仓库**：rsync 同步内容 + commit + 非 force push，保留 Gitee 提交历史，每次同步只新增一个提交
- **首次同步**（仓库不存在）：force push 初始化
- **请勿在 Gitee 端直接修改代码**，否则 push 可能失败（非 fast-forward）

### 2. DeepL API 额度

- DeepL API Free：50 万字符/月
- 额度耗尽时 API 静默失败（不报错，仅替换术语表保留英文原文）
- �A 殀查方法：翻译结果只替换了术语表但未翻译 → 查 `DEEPL_API_KEY` 是否有效或额度是否用完

### 3. 分支名

push 到同名分支，不强制映射 `main`：master 仓保持 master，main 仓保持 main。

### 4. Token 安全

- `GITEE_TOKEN`、`DEEPL_API_KEY`、`GH_PAT` 都通过 GitHub Secrets 存储
- 建议定期轮换 token

## 故障排查

### Q: 同步失败，提示 `Repository not found`

- 检查 `repos.txt` 中 `owner/repo` 拼写是否正确
- 若是**私有**仓库，确认已配置 `GH_PAT` 且拥有该仓库读取权限
- 检查 Gitee 端仓库是否已创建

### Q: 翻译结果只替换了术语表但未翻译

- DeepL API 额度可能已耗尽（HTTP 456 Quota exceeded）
- 检查 `DEEPL_API_KEY` 是否有效
- API 失败时静默保留#留英文原文，不报错

### Q: 改了 `postprocess.py` 推送后 Gitee 未更新

- 确认是 push 触发（非 schedule）：push 触发会传 `--no-sha-skip` 处理所有文件
- 段落缓存命中不调 API，但后处理会重新应用
- 检查 Actions 日志是否有 `Push trigger: processing all files`

### Q: 改了 `glossary.txt` 推送后翻译未变

- 改术语表需删对应仓库缓存（`cache/<repo>/`）后重译
- 推送会触发 `--no-sha-skip`，但段落缓存命中旧翻译（保护后文本变 → key 变 → 不命中 → 调 API 重译）
- 若未自动重译，手动触发 `force_retranslate=true`

### Q: 定时任务没有触发

- GitHub Actions 的定时任务可能有几分钟到十几分钟的延迟
- 检查仓库是否超过 60 天未活动（GitHub 会自动停用闲置仓库的定时任务）

## 相关文件

- [工作流定义](.github/workflows/sync-multi-repos.yml)
- [仓库清单](repos.txt)
- [术语表](glossary.txt)
- [映射表](translation-map.txt)
- [翻译目标文件](translate-files.txt)
- [翻译主逻辑](scripts/translate.py)
- [后处理逻辑](scripts/postprocess.py)
