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
- **目标端（Gitee）**：所有仓库都同步到固定账号 `home-assistant-xin` 下
- **Gitee 仓库名**：默认与 GitHub 端 repo 名一致；若两个 owner 下有同名仓库导致冲突，可用映射格式 `owner/repo => gitee_name` 指定不同的 Gitee 仓库名

## 文件结构

```
sync-gitee-hub/
├── .github/
│   └── workflows/
│       └── sync-multi-repos.yml   # 同步工作流
├── repos.txt                       # 仓库清单（owner/repo 或 owner/repo => gitee_name）
└── README.md                       # 本文档
```

## 配置步骤

### 1. 创建"同步中心"仓库

在 GitHub 上新建一个仓库（例如 `sync-gitee-hub`），将本项目所有文件推送上去。

### 2. 配置 Secrets

进入同步中心仓库 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**：

| Name | 是否必填 | 说明 | 获取方式 |
|------|---------|------|---------|
| `GITEE_USERNAME` | ✅ 必选 | Gitee 登录用户名 | Gitee 个人主页 |
| `GITEE_TOKEN` | ✅ 必选 | Gitee 私人令牌 | Gitee → 头像 → 设置 → 私人令牌，勾选 `projects` 权限 |
| `GH_PAT` | ⚪ 可选 | GitHub Personal Access Token | GitHub → 头像 → Settings → Developer settings → Personal access tokens → Tokens (classic)，勾选 `repo` 权限 |

> 💡 **公开仓库无需配置 `GH_PAT`**：若 `repos.txt` 中全部是公开仓库，工作流会自动使用 GitHub 内置的 `GITHUB_TOKEN` 拉取代码，无需额外创建 PAT。
>
> ⚠️ **仅当清单中包含私有仓库时**，才需要配置 `GH_PAT`，且该 token 必须拥有这些私有仓库的读取权限（若私有仓库属于其他用户，需对方把你加入协作者）。

### 3. 在 Gitee 预建空仓库

为 `repos.txt` 中每个条目，按其对应的 **Gitee 仓库名**在 https://gitee.com/home-assistant-xin 下创建对应空仓库：

- 简单格式 `owner/repo`：Gitee 仓库名 = `repo`
- 映射格式 `owner/repo => gitee_name`：Gitee 仓库名 = `gitee_name`
- **不要**勾选"使用 Readme 初始化仓库"
- 公开/私有属性应与 GitHub 端保持一致

### 4. 编辑仓库清单

打开 [`repos.txt`](repos.txt)，按格式添加要同步的仓库。支持两种格式：

```
# 简单格式（两端仓库名一致）
your-github-username/repo-one
other-github-user/repo-two

# 映射格式（重名场景，自定义 Gitee 端仓库名）
esphome/home-assistant-addon => esphome--home-assistant-addon
music-assistant/home-assistant-addon => music-assistant--home-assistant-addon
```

提交并推送后，工作流会自动触发首次同步。

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
| `/` 前后都必须有内容 | owner 和 repo 均不能为空 |
| `=>` 前后都必须有空格（建议） | `owner/repo => gitee_name` |
| `=>` 后的 Gitee 名 | 仅允许字母数字、点 `.`、下划线 `_`、连字符 `-` |
| 仅允许字符 | 字母 `a-z A-Z`、数字 `0-9`、点 `.`、下划线 `_`、连字符 `-` |
| 注释行 | 以 `#` 开头的行会被忽略 |
| 空行 | 自动忽略 |
| **Gitee 仓库名不可重复** | 多个条目映射到同一 Gitee 仓库时会报错（防止互相覆盖） |

### 合法示例 ✅

```
# 简单格式
your-name/repo-one
user.name/my-repo

# 映射格式（重名场景）
esphome/home-assistant-addon => esphome--home-assistant-addon
music-assistant/home-assistant-addon => music-assistant--home-assistant-addon
```

### 非法示例 ❌

```
repo-one              # 缺少 owner，无斜杠
/repo-one             # owner 为空
your-name/            # repo 为空
your-name/repo/extra  # 多个斜杠
your-name/repo one    # 含空格
your-name/repo@x      # 含非法字符 @
=> something          # 缺少 GitHub 端 owner/repo
your-name/repo =>     # 缺少 => 后的 Gitee 名
```

### 重名场景处理

当两个不同 GitHub owner 下有同名仓库时，由于 Gitee 端 owner 固定为 `home-assistant-xin`，必须用映射格式为它们指定不同的 Gitee 仓库名，否则会互相覆盖。

```
# ❌ 错误写法：两个仓库会同步到同一个 Gitee 仓库 home-assistant-xin/home-assistant-addon
esphome/home-assistant-addon
music-assistant/home-assistant-addon

# ✅ 正确写法：用映射格式指定不同的 Gitee 名
esphome/home-assistant-addon => esphome--home-assistant-addon
music-assistant/home-assistant-addon => music-assistant--home-assistant-addon
```

> 若 `repos.txt` 中出现 Gitee 仓库名重复，prepare 阶段会直接报错退出并指出冲突的条目，不会执行任何同步。

> 格式校验失败时，整个工作流会立即报错退出，不会执行任何同步操作。

## 触发方式

| 触发方式 | 说明 |
|---------|------|
| **定时同步** | 每天北京时间 08:00 自动同步 `repos.txt` 中全部仓库 |
| **手动触发** | Actions → 选择此 workflow → Run workflow，可在输入框中指定要同步的仓库（逗号分隔，格式 `owner/repo` 或 `owner/repo=>gitee_name`），留空则同步全部 |
| **配置变更** | 修改 `repos.txt` 或 workflow 文件本身并推送后自动触发 |

### 手动触发指定仓库示例

在 Run workflow 的 `repos` 输入框中填入：

```
user-a/repo-1,user-b/repo-2=>repo-2-alt
```

将只同步这两个仓库，不影响 `repos.txt` 中的其他条目。手动触发同样支持映射格式（`=>` 前后可省略空格，但建议保留）。

## 注意事项

### 1. 强制覆盖

工作流默认使用 `git push --force`，**请勿在 Gitee 端直接修改代码**，否则本地修改会被覆盖。所有代码改动应在 GitHub 端进行。

### 2. 仓库名一致性

- **简单格式** `owner/repo`：GitHub 端 `repo` 部分必须与 Gitee 端仓库名**完全一致**（区分大小写），否则推送会失败
- **映射格式** `owner/repo => gitee_name`：Gitee 端仓库名以 `=>` 后的 `gitee_name` 为准（区分大小写），预建 Gitee 空仓库时必须用这个名字
- **不可重复**：多个条目映射到同一个 Gitee 仓库名时，prepare 阶段会直接报错退出

### 3. 私有仓库同步

若 GitHub 源仓库是私有的：
- 必须在 Secrets 中配置 `GH_PAT`，且该 token 必须拥有该仓库的读取权限
- 公开仓库无需配置 `GH_PAT`，工作流自动使用 GitHub 内置 `GITHUB_TOKEN`
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
- 若是**私有**仓库，确认已配置 `GH_PAT` 且拥有该仓库读取权限
- 检查 Gitee 端 `home-assistant-xin/<repo>` 仓库是否已创建
- 若使用了映射格式（`=>`），检查 Gitee 端仓库是否按**映射后的名字**创建

### Q: 同步失败，提示 `Invalid format`

- 检查对应行的格式是否符合 [`repos.txt 格式规范`](#repostxt-格式规范)
- Actions 日志会显示具体哪一行出错

### Q: 同步失败，提示 `Duplicate Gitee repo name detected`

- `repos.txt` 中有多个条目映射到了同一个 Gitee 仓库名
- 通常发生在两个 GitHub owner 下有同名仓库、且都用了简单格式 `owner/repo` 的情况
- 解决：用映射格式 `owner/repo => unique_gitee_name` 为冲突仓库指定不同的 Gitee 名
- 日志会显示具体冲突的两个条目，方便定位

### Q: 清单里有多个条目，但只同步了第一个 / 看不到后面的条目

通常是 **Windows CRLF 行尾** 导致的：

- 在 Windows 上编辑 `repos.txt` 保存为 CRLF 行尾，推送到 GitHub 后 Actions 在 Ubuntu 上运行 bash
- bash 的 `read` 会把 `\r` 当作行内容的一部分，导致第二行及之后的条目解析时 `\r` 残留
- 校验 `=>` 后的 Gitee 名时 `\r` 不在 `[A-Za-z0-9._-]` 范围内 → 校验失败 → 条目被静默跳过
- 表现：Actions 列表里看不到后续条目对应的 job，像是"只同步了第一个"

**解决方案**（项目已内置防御，无需手动处理）：

1. `.gitattributes` 强制 `repos.txt` 使用 LF 行尾
2. workflow 内 `tr -d '\r'` 主动去除残留的 `\r`
3. `parse_entry` 失败时 `|| exit 1` 中止 prepare，不再静默吞错

如果仍遇到此问题，可在本地执行 `dos2unix repos.txt` 转换行尾后重新提交。

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
