# 提交与发布规范

本文件约定绿梦小助手项目的 **commit message** 写法与 **版本发布** 流程，供后续开发维护遵守。

---

## 一、Commit Message 约定

采用「类型前缀 + 简短描述」格式，便于日后 `git log` / `git blame` / 回滚时快速定位改动。

```
<type>: <简短描述>

[可选正文：说明为什么改、影响范围、验证方式]
```

### type 类型

| 类型 | 用途 |
|------|------|
| `feat` | 新增功能（如「新增计划编排」） |
| `fix` | 修复 bug |
| `refactor` | 重构（不改变外部行为） |
| `docs` | 文档 / 注释 |
| `build` | 打包、依赖、构建脚本 |
| `chore` | 其他杂项（清理、格式化等） |

### 描述要求

- **必须写**，禁止空 message（历史上 11-12 之后的提交 message 为空，今后避免）。
- 用中文、祈使句、一句话说清「做了什么」。
- 首行不超过 50 字左右。

### 示例

```
feat: 新增计划编排页，支持多脚本依次运行

- 左侧新增「计划」导航页
- 支持新建方案、添加步骤、保存到 plans.json
- 运行计划时依次「传送到传送点 → 跑 N 轮 → 下一步」
```

```
fix: 修复选图卡死，改用 win32ui 原生文件对话框
```

```
refactor: 版本号改为单一权威源，仅 runtime_env.APP_VERSION 一处维护
```

---

## 二、版本号管理（单一权威源）

- **唯一来源**：`runtime_env.py` 里的 `APP_VERSION`。
- 改版本时 **只改这一处**：
  - `build_flet.py` 从 `runtime_env.APP_VERSION` 读取；
  - `installer.nsi` 由 `build_flet.py` 通过 `-DAPP_VERSION` 传入（未传会 `!error` 报错）；
  - 界面（`macro_ui_flet.py`）通过 `runtime_env.APP_VERSION` 引用。
- 不要再在别处维护硬编码版本号。

---

## 三、发布流程

每次发布新版本，按以下步骤：

1. **改版本号**：修改 `runtime_env.py` 的 `APP_VERSION`（例如 `1.1.2` → `1.1.3`）。
2. **提交代码**：`git add -A && git commit -m "feat/fix: ..."`（遵守上面的 message 约定）。
3. **打 tag**（带说明）：
   ```bash
   git tag -a v1.1.3 -m "v1.1.3：本次更新内容简述"
   git push origin v1.1.3
   ```
4. **推分支**：`git push origin main`。
5. **本地打包**（需 Python 3.12 + PyInstaller + NSIS）：
   ```bash
   python build_flet.py --verify --zip --installer
   ```
   产物在 `dist_flet/`（`Lumin-Setup.exe` + `Lumin-win64.zip`）。
6. **在 GitHub 发 Release**：以刚打的 tag 创建 Release，上传两个产物，`body` 用 `## vX.Y.Z 更新内容` 列点说明（参照 v1.1.1 / v1.1.2 的写法）。

### 版本号规则（SemVer 简化）

- `x.y.z`：`x` 大版本（架构级变化）、`y` 功能更新、`z` 修复补丁。
- 本工具按「功能更新 / 修复」的粒度，多数情况递增 `z`（补丁）或 `y`（小功能）。

---

## 四、常见提醒

- **不要**提交空 message。
- **不要**把构建产物（`build/`、`dist_flet/`、`*.spec`、`__pycache__/`）提交进仓库——`.gitignore` 已排除。
- **不要**把用户运行数据（`recordings/plans.json`、`runs.jsonl`、`*.log`）提交进仓库。
- 模板图（`tp_imgs/`）、录制脚本（`recordings/*.py` + `*.json`）属于产品数据，**应纳入版本管理**。
