# 更新日志

## [1.0.2] - 2026-08-16

### 文档
- 新增 `docs/CONFIG.md` 配置文档：配置项一览、管理员权限、推送行为、
  数据源缓存与常见问题。
- 更新日志补充“更新方式”说明。

## [1.0.1] - 2026-08-16

### 新增
- 新增功能菜单 `acmer群管理插件菜单`：群聊直接发送即可查看全部指令与所需
  权限（无需 @机器人）；原 `/acm`、`比赛帮助` 保留为快捷方式。

## [1.0.0] - 2026-08-16

### 新增
- ACM 竞赛工具插件：牛客 / Codeforces / AtCoder / 洛谷比赛查询，
  每日早报、赛前 15 分钟提醒，WebUI 配置页与管理员校验。

## 更新方式

1. `git pull` 拉取最新代码；
2. 把 `main.py`、`metadata.yaml`、`requirements.txt`、`src/`、`pages/`
   同步到 `AstrBot/data/plugins/acmer_qq_group_bot/`；
3. 在 AstrBot WebUI 插件管理页重载插件（或重启 AstrBot）。
