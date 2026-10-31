# LivingMemory Framework 1.1.0

这是包含长期记忆与可选学习模块的单个 AstrBot 插件，版本 `2.7.0-beta.3+memoryfw.3`。保留 lxfight、NickMo、EterUltimate 等原作者及 AGPLv3 许可证；学习源码的 LICENSE 位于 `learning/LICENSE`。

只安装本插件。旧 `astrbot_plugin_self_learning` 源码应在停机迁移时移出 `data/plugins`，旧配置先合并，原学习数据目录保留。完整迁移工具、原理、测试与前端源码位于配套 `astrbot-memory-framework-1.1.0.zip`。

`framework_modules.mode` 默认 `memory_and_learning`，保留原学习能力；设为 `memory_only` 并重载后不加载学习实现。`/lmem modules` 查看状态。既有记忆和学习命令、记忆面板及学习面板均保留。

数据与算法未合并重写。默认仍使用原两类 plugin_data 目录；已配置的自定义路径、数据库类型、模型与权重应通过配置迁移保留。来源、会话、人格、证据预算与撤销检查继续生效，学习停用期间的通知会保留待补投。

本地验证基线为 AstrBot 4.29.1 维护核心、Python 3.12.12。新部署默认 session + 人格隔离、7000 字节受管证据预算、SQLite 学习存储及关闭自动人格应用。密钥配置在 AstrBot 中。真实模型质量与高负载节省幅度需另行评测。

完整安装、迁移、配置、命令和构建说明已整合到框架源码根目录的 [README](../../README.md)；Usage 统计与提示词布局见其中的 [对应章节](../../README.md#llm-usage)。仅持有插件安装 ZIP 时，请配套获取 `astrbot-memory-framework-1.1.0.zip` 查阅完整说明。维护源码不要通过上游一键更新覆盖；使用本框架的后续发行包升级。

[English / upstream capabilities](README_en.md) · [变更历史](CHANGELOG.md) · [修改声明](FRAMEWORK_NOTICE.md) · [许可证](LICENSE)
