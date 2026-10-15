# 隔离矩阵（R-IND-01 交付物）

```bash
PYTHON=/path/to/python bash tools/run_single_plugin.sh acmer_qq_group_bot
```

| 用例 | 期望 | 结果 | 证据（时间/输出摘要） |
|---|---|---|---|
| 只装本插件启动 | 无异常；插件类可导入；工具已注册 | ✅ | 2026-10-15，`插件类：[AcmerGroupBot]`；`AI 工具：[acmer_contests, acmer_daily_problem, acmer_group_rank, acmer_profile]` |
| 全量单测（隔离副本内） | 全绿 | ✅ | `569 passed`（exit=0） |
| 零跨插件 import | 门禁 exit 0 | ✅ | `零耦合门禁通过（76 个文件）` |
| 不装其它三个插件 | 工具照常工作 | ✅ | 工具只依赖本插件缓存/KV（比赛走本地缓存、排行走 rank_service） |
| 缺失后台页面/配置 | 走默认值，不报错 | ✅ | `get_settings()` 缺键时有默认值；`_normalize_settings_payload` 同步兜底 |
| 关闭 AI 开关 | 工具立即返回 `disabled`，人类指令不受影响 | ✅ | `test_tools_disabled_by_config`、`test_config_contract_read_write_and_page` |
| 只读性 | 工具不写快照、不渲染图片 | ✅ | `test_group_rank_is_read_only`、`test_profile_query_is_read_only`（断言 `record_metrics=False`） |

> 顺带修复：`tests/test_accounts.py::test_luogu_analysis_...` 原为**依赖固定日期**的用例（写成 2026-09-01/07-20），
> 在 2026-10-15 已必然失败（与本轮改动无关，已用 `git archive HEAD` 复现）。已改为相对今天构造日期，现全绿。
