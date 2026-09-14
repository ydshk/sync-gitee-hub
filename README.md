# sync-gitee-hub

批量将 GitHub 仓库自动同步到 Gitee 的集中式工作流。

## 工作原理

```
GitHub (多owner)  ──┐
                    │  GitHub Actions (定时/手动/推送触发)
                    │  ──遍历 repos.txt──┐
                    │                      ▼
                    │              逐个仓库镜像推送
                    │                      │
                    └──────────────► Gitee (固定owner: home-assistant-xin)
```

- **源端（GitHub）**：每个仓库的 owner 可以不同，在 `repos.txt` 中按 `owner/repo` 指定
- **目标端（Gitee）**：所有仓库都同步到固定账号 `home-assistant-xin` 下，仓库名与 GitHub 端保持一致

## 文件结构

```
sync-gitee-hub/
├── .github/
│   └── workflows/
│       └── sync-multi-repos.yml   # 同步工作流
├── repos.txt                       # 仓库清单（每行 owner/repo）
└── README.md                       # 本文档
```

## 配置步骤

### 1. 创建"同步中心"仓库

在 GitHub 上新建一个仓库（例如 `sync-gitee-hub`），将本项目所有文件推送上去。

### 2. 配置 Secrets

进入同步中心仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**，添加以下 3 个：

| Name | 说明 | 获取方式 |
|------|------|---------|
| `GITEE_USERNAME` | Gitee 登录用户名 | Gitee 个人主页 |
| `GITEE_TOKEN` | Gitee 私人令牌 | Gitee → 头像 → 设置 → 私人令牌，勾选 `projects` 权限 |
| `GH_PAT` | GitHub Personal Access Token | GitHub → 头像 → Settings → Developer settings → Personal access tokens → Tokens (classic)，勾选 `repo` 权限 |

> ⚠️ **关于 `GH_PAT`**：该 token 必须能够拉取 `repos.txt` 中所有 owner 的源仓库。如果清单中包含其他用户的**私有**仓库，需要对方把你加入协作者；公开仓库则正常 PAT 即可。

### 3. 在 Gitee 预建空仓库

为 `repos.txt` 中每个 `repo` 名，在 https://gitee.com/home-assistant-xin 下创建对应空仓库：

- **不要**勾选"使用 Readme 初始化仓库"
- 公开/私有属性应与 GitHub 端保持一致

### 4. 编辑仓库清单

打开 [`repos.txt`](repos.txt)，按格式添加要同步的仓库：

```
your-github-username/repo-one
other-github-user/repo-two
home-assistant-xin/repo-three
```

提交并推送后，工作流会自动触发首次同步。

## repos.txt 格式规范

### 必须严格遵守的格式

每行一个条目，格式为 `owner/repo`：

```
github_owner/repo_name
```

### 校验规则

| 规则 | 说明 |
|------|------|
| 必须包含且仅包含一个 `/` | 不能多也不能少 |
| `/` 前后都必须有内容 | owner 和 repo 均不能为空 |
| 仅允许字符 | 字母 `a-z A-Z`、数字 `0-9`、点 `.`、下划线 `_`、连字符 `-` |
| 注释行 | 以 `#` 开头的行会被忽略 |
| 空行 | 自动忽略 |

### 合法示例 ✅

```
your-name/repo-one
home-assistant-xin/repo-three
user.name/my-repo
```

### 非法示例 ❌

```
repo-one              # 缺少 owner，无斜杠
/repo-one             # owner 为空
your-name/            # repo 为空
your-name/repo/extra  # 多个斜杠
your-name/repo one    # 含空格
your-name/repo@x      # 含非法字符 @
```

> 格式校验失败时，整个工作流会立即报错退出，不会执行任何同步操作。

## 触发方式

| 触发方式 | 说明 |
|---------|------|
| **定时同步** | 每天北京时间 08:00 自动同步 `repos.txt` 中全部仓库 |
| **手动触发** | Actions → 选择此 workflow → Run workflow，可在输入框中指定要同步的仓库（逗号分隔，格式 `owner/repo`），留空则同步全部 |
| **配置变更** | 修改 `repos.txt` 或 workflow 文件本身并推送后自动触发 |

### 手动触发指定仓库示例

在 Run workflow 的 `repos` 输入框中填入：

```
user-a/repo-1,user-b/repo-2
```

将只同步这两个仓库，不影响 `repos.txt` 中的其他条目。

## 注意事项

### 1. 强制覆盖

工作流默认使用 `git push --force`，**请勿在 Gitee 端直接修改代码**，否则本地修改会被覆盖。所有代码改动应在 GitHub 端进行。

### 2. 仓库名一致性

`repos.txt` 中的 `repo` 部分必须与 Gitee 端仓库名**完全一致**（区分大小写），否则推送会失败。

### 3. 私有仓库同步

若 GitHub 源仓库是私有的：
- `GH_PAT` 必须拥有该仓库的读取权限
- Gitee 目标仓库也建议设为私有，避免代码泄露

### 4. Token 安全

- `GITEE_TOKEN` 和 `GH_PAT` 都通过 GitHub Secrets 存储，不会出现在日志中
- workflow 末尾会执行 `git remote remove gitee` 清理 URL 中的凭证
- 建议定期轮换 token

### 5. GitHub Actions 限额

- 公开仓库：免费且无限制
- 私有仓库：每月有免费分钟数限额（免费账号 2000 分钟/月），定时任务会消耗额度

## 故障排查

### Q: 同步失败，提示 `Repository not found`

- 检查 `repos.txt` 中 `owner/repo` 拼写是否正确
- 检查 `GH_PAT` 是否有权限访问该仓库
- 检查 Gitee 端 `home-assistant-xin/<repo>` 仓库是否已创建

### Q: 同步失败，提示 `Invalid format`

- 检查对应行的格式是否符合 [`repos.txt 格式规范`](#repostxt-格式规范)
- Actions 日志会显示具体哪一行出错

### Q: 定时任务没有触发

- GitHub Actions 的定时任务可能有几分钟到十几分钟的延迟，属于正常现象
- 检查仓库是否超过 60 天未活动（GitHub 会自动停用闲置仓库的定时任务，进入 Actions 重新启用即可）

### Q: 想要更频繁的同步

修改 `sync-multi-repos.yml` 中的 `cron` 表达式：

```yaml
schedule:
  - cron: '0 */6 * * *'   # 每 6 小时同步一次
```

> ⚠️ 不建议设置过于频繁（如每 5 分钟），可能触发 GitHub 限流。

### Q: 想要实时同步某个仓库

对于需要实时同步的核心仓库，可以单独配置 [`sync-to-gitee.yml`](../.github/workflows/sync-to-gitee.yml)（基于 push 事件触发），其他仓库仍走本集中方案。

## 相关文件

- [工作流定义](.github/workflows/sync-multi-repos.yml)
- [仓库清单](repos.txt)
