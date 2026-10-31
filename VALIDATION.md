# 1.1.0 单插件发行验收（2026-11-01）

当前版本通过 **469 项本地测试**：记忆与事件分发 218、学习 237、发行 7、模块与迁移契约 7。实际 AstrBot 解压 491 个运行文件一致，核对 757 个源码指纹；225 个受保护文件与 39 个原配置分组不变。

真实核心三次模式切换启动验证了采集、召回、来源撤销、停用期间待投递、恢复补偿和防重放，均正常关闭。仅记忆不导入学习实现。完整模式的进程内存快照差异很小，不声称大幅提速；安装 ZIP 合计字节减少 12.83%。

环境：本地维护 AstrBot 4.29.1、Windows、Python 3.12.12，合成数据与本地 Provider，付费 API 0 次。未自动部署，未改动生产配置或数据库。结果见 `validation-summary.json`；复现入口为 `tools/verify_release.py`、`tools/run_regressions.py`、`tools/verify_startup.py` 和 `tools/verify_module_switch.py`。使用新的隔离输出目录，核心/依赖参数沿用下方历史说明。

`MEMORY_FRAMEWORK_MODE=memory_only` 可验收全新仅记忆模式；模式切换脚本自行在完整/仅记忆/完整模式间切换，不需要连接真实群或真实模型。真实质量、稳态性能与其他部署环境尚待验收。

---

以下为 1.0.x 历史验收，双插件路径仅适用于其归档源码。

# 发行验收记录

日期：2026-10-27（Asia/Shanghai）。框架：1.0.0。测试对象是 dist 中的插件 ZIP 解压安装结果，不是正式运行目录。

## 通过项

| 验证 | 结果 |
|---|---|
| 安装/打包测试 | 7 passed：只读预检、双插件安装、已有源码/配置保护、坏哈希和越界路径拒绝、schema 与预设一致 |
| LivingMemory 针对性回归 | 86 passed：v0.2–v0.5 来源/生命周期不变量、会话作用域、证据注入 |
| self_learning 针对性回归 | 21 passed：权威证据、撤销消费、委托入口与临时上下文 |
| AstrBot 原生上传解压器 | 两个 ZIP 元数据识别和完整解压通过，分别 293 / 458 文件逐字节一致 |
| 源码核对 | 747 个原始文件有来源及发行 SHA-256；10 个文件为声明过的打包修改 |
| 正式源码保护 | 打包结束对照原始哈希，747 个正式源码文件无变化 |
| 空库冷启动与关闭 | 两次完整启动，两组件均就绪，均正常关闭；图谱按发行默认开启 |
| 合成端到端用例 | 采集 → 记忆候选 → 权威证据 → 来源撤销 → 两插件不可见 → 冷启动后重投被拒绝 |
| 新安装配置 | schema 自动生效：权威模式与 Hook 开启、SQLite、自动人格应用关闭 |
| 包内检查 | Python AST 解析、基础依赖版本、Dashboard 引用资源、空密钥默认值、无运行数据库/索引/缓存 |

共 **114 项测试通过**，另有两次冷启动及原生解压验收。机器可读摘要见 `validation-summary.json`，基础依赖实际版本见其中 `release.plugins[].dependencies`。

## 验证环境和边界

使用本地 AstrBot 4.29.1 维护核心、Windows x64、Python 3.12；核心自带运行依赖加隔离目录中的 networkx、pytz、emoji、cachetools 和 json-repair。测试依赖 pytest/pytest-asyncio 取自已有测试目录，未安装进正式 Python。

冷启动使用空测试数据库、合成平台事件和本地假 Chat/Embedding Provider；阻断外部网络连接，没有真实群连接或外部模型调用。自动学习和黑话联网在测试配置中关闭，图谱保持发行默认开启。依赖安装钩子在生命周期测试中替换为离线行为；另行核对了所有 requirements 的已安装版本约束。未在完全空白的操作系统重装整个 AstrBot/Python 环境。

回归日志包含一条警告（各测试进程各自计数）；冷启动日志中的 Provider 等待、可选 scikit-learn 缺失使用统计降级、部分服务无 stop 方法为现有实现日志。重投被撤销消息时的拒绝异常是用例预期结果，不代表消息被重新接收。

未声称完成真实 Provider 总结/回答质量、所有平台原生撤回通知、跨平台运行、性能负载、历史迁移、彻底清除、精确 token 限制或未修改的上游核心兼容验收。限制与原设计手册一致。

## 复现

先在测试 Python 中准备 AstrBot 的运行依赖、两插件 requirements、pytest 和 pytest-asyncio。`--core` 指向包含 `astrbot/` 的核心源码目录。每次使用新的输出目录。

```sh
python tools/build_release.py
python tests/test_distribution.py
python tools/verify_release.py --core /path/to/core --output /tmp/memory-release-check
python tools/run_regressions.py --core /path/to/core --output /tmp/memory-regression-check
```

冷启动脚本通过环境变量指定路径（下面为 PowerShell）：

```powershell
$env:ASTRBOT_CORE = 'C:\test\astrbot-core'
$env:MEMORY_FRAMEWORK_TEST_ROOT = 'C:\test\memory-cold-start-new'
python tools/verify_startup.py
```

若测试包在单独目录，回归脚本可加 `--test-deps /path/to/testdeps`；运行依赖在独立目录时，`verify_release.py` / `verify_startup.py` 支持 `MEMORY_FRAMEWORK_EXTRA_DEPS`。这些目录和原始测试日志位于本工作区 `validation/`，均不进入发行 ZIP。

真实上传解压器会消费上传 ZIP，验证脚本始终传入临时副本；发行文件保持可下载状态。

## 1.0.1 维护验证（2026-10-29）

本次使用便携包 Python 3.12.12 和实际 AstrBot 核心，在外网阻断、临时数据目录及本地 Provider 替身下完成 180 项测试，覆盖 Usage、流式、摘要、合并和来源边界。此前结果仍是初始发行的历史记录；本次没有重跑在线质量评测。公共前缀对照为 164 → 892 个 Unicode 字符，不是 token 数或服务端命中率。

补充验收：7 项发行/安装器测试通过；实际 AstrBot ZIP 读取器完成两组件解包校验、751 项源码指纹检查和静态资产核验。纯 Python 缺失依赖仅装入 C 盘临时测试依赖目录，生产 Python 环境未安装或升级依赖。
