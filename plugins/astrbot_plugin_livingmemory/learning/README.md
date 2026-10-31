# 内部学习模块

此目录保留原 self_learning 的学习、反馈强化、质量监控、人格审查、黑话与社交实现，当前由父目录的 LivingMemory Framework 管理。

`module.py` 中的 `LearningModule` 是普通 Python 类，不是第二个 AstrBot 插件。不要单独安装此目录。模式、配置和管理命令由父插件统一提供；`_conf_schema.json` 用于学习面板，`UPSTREAM_METADATA.yaml` 记录迁移前元数据。

默认数据位置仍是 `plugin_data/astrbot_plugin_self_learning`，用于兼容已有数据；这不表示运行了第二个插件。原作者与 LICENSE、历史 CHANGELOG 和英文能力说明保留。完整测试及前端构建源码随框架完整源码包提供。

安装、配置、学习链路、面板、开发和接口说明已整合到框架源码根目录的 [README](../../../README.md)。仅持有插件 ZIP 时，请在配套完整源码包中查阅。

[English / upstream capabilities](README_EN.md) · [历史变更](CHANGELOG.md) · [贡献说明](CONTRIBUTING.md) · [安全说明](SECURITY.md) · [修改声明](FRAMEWORK_NOTICE.md) · [许可证](LICENSE)
