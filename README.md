# acmerQQ群机器人

基于 AstrBot 的 ACM 竞赛工具插件：查询牛客 / Codeforces / AtCoder / 洛谷
最近比赛，并支持每日早报与赛前 15 分钟提醒。

## 文档

- [更新日志](CHANGELOG.md)
- [配置文档](docs/CONFIG.md)

## 指令

所有指令在群聊**直接发送即可触发**（无需 @机器人，`/` 前缀同样可用），
私聊可直接发送；`acmer群管理插件菜单`、`acmer激活` 也是直接发送。

| 指令 | 功能 | 权限 |
| :--- | :--- | :--- |
| `/nk`（`牛客`） | 牛客全部未开始比赛 | 所有人 |
| `/最近nk`（`最近牛客`） | 牛客最近一场 | 所有人 |
| `/cf`（`codeforces`） | Codeforces 全部未开始比赛 | 所有人 |
| `/最近cf`（`最近Codeforces`） | Codeforces 最近一场 | 所有人 |
| `/atc`（`atcoder`） | AtCoder 全部未开始比赛 | 所有人 |
| `/最近atc`（`最近AtCoder`） | AtCoder 最近一场 | 所有人 |
| `/lg`（`洛谷`） | 洛谷全部未开始比赛 | 所有人 |
| `/最近lg`（`最近洛谷`） | 洛谷最近一场 | 所有人 |
| `/update`（`刷新比赛`） | 强制刷新全部平台数据 | 管理员 |
| `acmer激活`（`/激活`） | 首次激活本群主动推送（重启后任意群消息自动恢复） | 所有人 |
| `acmer群管理插件菜单` | 功能菜单（`/acm`、`比赛帮助` 仍可用） | 所有人 |

## WebUI 配置

AstrBot 管理面板 → 插件管理 → acmerQQ群机器人：

- 管理员列表（决定 `/update` 权限；QQ 官方适配器不会把群角色映射为
  AstrBot 管理员，所以必须在这里显式配置）。
- 每日早报时间（默认 08:00）、推送平台、赛前提醒开关。
- “通知尝试 @全体成员”：开启后通知会尝试带 `<@everyone>` 发送；
  若机器人没有权限导致发送失败，会自动降级为普通通知并暂停该群 6 小时重试。
- 群推送配置：机器人收到过消息的群会自动注册，可单独开关与设置早报时间。

## 数据源（均为公开数据，无需登录）

| 平台 | 来源 | 说明 |
| :--- | :--- | :--- |
| Codeforces | `codeforces.com/api/contest.list` | 官方 API，过滤 `phase=BEFORE` |
| 牛客 | `ac.nowcoder.com/acm/calendar/contest` | 牛客比赛日历接口 |
| AtCoder | `atcoder.jp/contests/` | 官网 Upcoming Contests 赛程表 |
| 洛谷 | `luogu.com.cn/contest/list` | 页面内 `#lentille-context` JSON，无需 Cookie |

所有数据统一转为 `Contest` 模型并缓存 5 分钟；网络超时 10 秒、最多重试 3 次。

## 已知限制

- QQ 官方机器人在群聊中发送“@全体成员”受限：官方格式文档仅明确支持
  “文字子频道”的 `@everyone`，且要求机器人拥有“发送@全部成员”权限；
  社区与官方 FAQ 也确认官方 bot 在群聊中 @全体成员 为已知问题（每天全群
  共享约 10 次）。因此插件默认不开启，开启后也会在失败时自动降级。
- QQ 官方适配器的主动推送依赖该群先与机器人产生过消息（用于缓存会话场景），
  否则可能静默跳过；本插件会在群消息到达时自动注册该群。

## 与原开发文档的主要差异（严谨性修正）

- 牛客日历真实接口为 `ac.nowcoder.com/acm/calendar/contest`（文档写的是
  `/acm/contest/calendar`，返回的是页面而非 JSON）；返回字段为
  `contestId/contestName/ojName/link/startTime/endTime`，其中 `startTime`
  是**毫秒**时间戳，时长需由 `startTime`/`endTime` 计算。
- 原文档称“AtCoder 与牛客共用牛客日历接口”，实测牛客日历对 AtCoder 收录
  不全（9 月起为空），因此 AtCoder 改为直接解析官网赛程表。
- 洛谷页面数据不在 HTML DOM 中，而是内嵌在
  `<script id="lentille-context">` 的 JSON（`data.contests.result[]`，
  `startTime` 为**秒**时间戳），因此无需 BeautifulSoup/lxml，直接解析
  JSON 即可，依赖也更少。
- AstrBot 4.x 没有 `@filter.schedule()` / `@on_schedule` 装饰器，定时推送
  由插件自己的 asyncio 后台任务实现；数据持久化使用 AstrBot 的插件 KV
  存储（`put_kv_data`/`get_kv_data`），不是文档示例中的 `get_db_client()`。
- `filter.PermissionType.ADMIN` 在 QQ 官方适配器下永远为 false（事件 role
  不会映射为 admin），因此 `/update` 使用插件自己的管理员列表校验。

## 部署

将 `metadata.yaml`、`main.py`、`requirements.txt`、`src/`、`pages/` 放入
`AstrBot/data/plugins/acmer_qq_group_bot/`，在 WebUI 启用并重载插件。
