# acmerQQ群机器人 后台 UI 重设计 · 实现文档

> 状态:**未执行 · 等确认**(本文档只描述实现步骤,尚未修改任何代码)
> 上游:`docs/后台UI重设计-需求文档.md`、`docs/后台UI重设计-设计文档.md`
> 目标版本:**1.17.0**

---

## 0. 前置声明

本文件只写"怎么做"。**在你确认之前,不会动 `main.py` / `pages/settings/index.html` / 任何配置与线上环境。**
确认后按 §8 批次推进,每批次跑绿测试再进下一批;生产部署前再确认一次。

---

## 1. 改动清单

| 文件 | 改动 | 类型 |
| --- | --- | --- |
| `pages/settings/index.html` | 整页重写(509 行 → 预计 1800~2500 行,含绑定抽屉) | 重写 |
| `main.py` | 新增 7 个 Web 接口(含绑定 GET/POST)+ 推送日志 + 幂等隔离 + 抽函数 | 新增/微调 |
| `src/scheduler.py` | 早报成功/失败写推送日志 | 微调 |
| `tests/test_web_admin.py` | 概览/日志/绑定 CRUD/排行/导入 用例 | 新增 |
| `tests/test_admin_web_run.py` | 试跑幂等隔离用例 | 新增 |
| `README.md` / `docs/CONFIG.md` | 后台功能说明 | 文档 |
| `CHANGELOG.md` / `metadata.yaml` | 1.17.0 | 收尾 |

**不改**:`src/account_registry.py`、`src/account_store.py`、`src/rank_service.py` 的对外行为,以及现有 3 个接口的语义。**不引入 i18n。**

---

## 2. 后端实现(`main.py`)

### 2.1 新增常量与工具

```python
PUSH_LOG_KEY = "push_log"
PUSH_LOG_MAX = 200
RUN_NOW_KINDS = ("morning", "weekly", "settle", "signup")

def _query_param(name: str, default: str = "", cast=str):
    """读取 GET 查询参数,兼容不同 AstrBot 版本的 request 实现。"""
```

> `_query_param` 内部优先 `request.args.get(name)`,若该版本是 `request.query` 则回退;集中一处,避免散落。

### 2.2 推送日志

```python
async def _log_push(self, group_id: str, kind: str, ok: bool, detail: str = "") -> None:
    """把一次推送结果写进环形日志(最近 PUSH_LOG_MAX 条),失败只告警。"""
    try:
        items = await self.get_kv_data(PUSH_LOG_KEY, []) or []
        if not isinstance(items, list):
            items = []
    except Exception:
        return
    items.append({"ts": time.time(), "group_id": str(group_id), "kind": str(kind),
                  "ok": bool(ok), "detail": str(detail)[:200]})
    try:
        await self.put_kv_data(PUSH_LOG_KEY, items[-PUSH_LOG_MAX:])
    except Exception as exc:
        logger.warning("写入推送日志失败：%s", exc)
```

写入点(6 处,均在“已有流程的结果处”,不改流程):

| 位置 | kind |
| --- | --- |
| `src/scheduler.py::_maybe_morning_push` | `morning` |
| `main.py::push_weekly_boards` | `weekly_boards` |
| `main.py::_push_settlement` | `settle` |
| `main.py::tick_weekly_report` | `weekly_report` |
| `main.py::tick_signup_reminders` | `signup` |
| `main.py::_web_test_push` | `test` |

> §scheduler` 用 `getattr(self.plugin, "_log_push", None)` 调用,避免硬依赖。

### 2.3 `_next_push_times`

对每个 `enabled` 群算“下次早报”;若 `weekly_report_enabled`,再算“下次周报”;按时间升序返回最多 20 条 `{"group_id","kind","at"}`。
赛果/报名提醒是事件驱动,不在其中。

### 2.4 接口实现

**(1) `_web_overview` — `GET /{plugin}/overview`**

```python
async def _web_overview(self):
    groups = await self.get_groups()
    admins = await self._get_admins()
    return json_response({"status": "success", "data": {
        "group_count": len(groups),
        "enabled_group_count": sum(1 for g in groups if g.enabled),
        "admin_count": len(admins),
        "platform_id": self._default_platform_id(),
        "store_backend": "sqlite" if self.account_registry.store_enabled else "kv",
        "next_pushes": await self._next_push_times(groups, datetime.now(CN_TZ)),
        "recent_pushes": (await self.get_kv_data(PUSH_LOG_KEY, []) or [])[-20:][::-1],
    }})
```

**(2) `_web_push_log` — `GET /{plugin}/push-log`**
参数 `limit`(默认 50,上限 200)、`group_id`(可选);返回倒序 `{"items":[…],"total":n}`。

**(3) `_web_bindings_list` — `GET /{plugin}/bindings`(查)**

```python
async def _web_bindings_list(self):
    platform = _query_param("platform", "")
    q = _query_param("q", "").strip().lower()
    limit = max(1, min(500, int(_query_param("limit", "100") or 100)))
    offset = max(0, int(_query_param("offset", "0") or 0))
    accounts = await self.account_registry.get_all_accounts()
    items = []
    for uid, per in accounts.items():
        if not isinstance(per, dict):
            continue
        for pf, rec in per.items():
            if not isinstance(rec, dict) or (platform and pf != platform):
                continue
            handle = str(rec.get("handle") or rec.get("platform_user_id") or "")
            qq_name = str(rec.get("qq_name") or "")
            if q and q not in f"{uid} {handle} {qq_name}".lower():
                continue
            items.append({"user_id": str(uid), "platform": pf, "handle": handle,
                          "platform_user_id": str(rec.get("platform_user_id") or ""),
                          "display_name": str(rec.get("display_name") or handle),
                          "qq_name": qq_name,
                          "verified_at": float(rec.get("verified_at") or 0)})
    items.sort(key=lambda x: x["verified_at"], reverse=True)
    counts = {}
    for it in items:
        counts[it["platform"]] = counts.get(it["platform"], 0) + 1
    return json_response({"status": "success", "data": {
        "total": len(items), "items": items[offset:offset + limit],
        "platform_counts": counts}})
```

**(4) `_web_bindings_write` — `POST /{plugin}/bindings`(增/改/删)**

```python
async def _web_bindings_write(self):
    payload = await request.json(default=None)
    if not isinstance(payload, dict):
        return error_response("请求体格式不正确")
    action = str(payload.get("action") or "").strip()

    if action == "delete":
        items = payload.get("items")
        if not isinstance(items, list) or not items:
            return error_response("请选择要删除的绑定")
        removed = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            uid = str(it.get("user_id") or "").strip()
            pf = str(it.get("platform") or "").strip()
            if not uid or pf not in ACCOUNT_PLATFORMS:
                continue
            try:
                if await self.account_registry.remove_binding(uid, pf):
                    removed += 1
            except Exception as exc:
                logger.warning("后台解绑失败 user=%s platform=%s: %s", uid, pf, exc)
        if removed:
            self._invalidate_all_rank_cache()
            logger.info("后台解绑 admin=web removed=%d", removed)
        return json_response({"status": "success", "data": {"action": "delete", "removed": removed}})

    if action != "save":
        return error_response("不支持的 action")

    user_id = str(payload.get("user_id") or "").strip()
    platform = str(payload.get("platform") or "").strip()
    identifier = str(payload.get("identifier") or "").strip()
    qq_name = str(payload.get("qq_name") or "").strip()[:32]
    group_id = str(payload.get("group_id") or "").strip()

    if not user_id:
        return error_response("请填写 QQ 用户 ID")
    if platform not in ACCOUNT_PLATFORMS:
        return error_response("不支持的平台")
    if not identifier:
        return error_response("请填写账号")
    normalized = normalize_account_identifier(platform, identifier)
    if not normalized:
        return error_response(self.account_fetcher.invalid_identifier_message(platform))

    try:
        profile = await self.account_fetcher.get_profile(platform, normalized, detail=False, force=True)
    except AccountFetchError as exc:
        return error_response(self._account_error_text(platform, exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("后台绑定抓取失败：%s", exc)
        return error_response(self._account_error_text(platform, exc))

    old_handle = ""
    try:
        rec = (await self.account_registry.get_user_accounts(user_id)).get(platform)
        if isinstance(rec, dict):
            old_handle = str(rec.get("handle") or rec.get("platform_user_id") or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取原绑定失败：%s", exc)

    try:
        await self.account_registry.save_binding(
            user_id, platform, profile, group_id=group_id or None, qq_name=qq_name
        )
    except ValueError:
        return error_response("这个平台账号已经绑定到其他 QQ 用户，请先删除原绑定")
    except Exception as exc:  # noqa: BLE001
        logger.error("后台绑定保存失败：%s", exc, exc_info=True)
        return error_response("绑定保存失败，请稍后重试")

    self._invalidate_all_rank_cache()
    logger.info("后台绑定 admin=web user=%s platform=%s handle=%s", user_id, platform, profile.handle)
    return json_response({"status": "success", "data": {
        "action": "save",
        "item": {"user_id": user_id, "platform": platform,
                 "handle": profile.handle, "qq_name": qq_name},
        "replaced": old_handle or None}})
```

> 关键点:**复用 `save_binding` / `remove_binding`**,因此唯一约束、KV 双写、SQLite 回退、pending 清理全部自动继承;写/删后必须 `_invalidate_all_rank_cache()`。

**(5) `_web_rank` — `GET /{plugin}/rank`**

```python
rows, errors = await self.rank_service.read(
    group_id, platform, progress=progress, allow_stale=not refresh, force=bool(refresh)
)
return json_response({"status": "success", "data": {"rows": rows, "errors": errors, "stale": not refresh}})
```

**(6) `_web_run_now` — `POST /{plugin}/run-now`**

入参 `{"group_id","kind","contest_id"?}`;校验:群存在 → `enabled` → `_group_scene_ready(...)`。
分发:
- `morning`:`build_test_text(group)` → `send_notification(group, text)` →(可选)`push_weekly_boards`
- `weekly`:新增 `_push_weekly_report_for_group(group, now, write_key=False)`
- `settle`:从 `self.settlement.settlement_candidates(platform, now, delay_minutes)` 按 `contest_id` 选一场 → `_push_settlement(..., write_key=False)`
- `signup`:新增 `_push_signup_for_group(group, contest, tier, write_key=False)`

返回 `{"ok":bool,"results":[{"step","ok","message"}]}`;结果同时 `_log_push(kind="test")`。

**(7) `_web_import` — `POST /{plugin}/import`**
入参 `{"apply":bool,"include_groups":bool,"payload":{admin_users,settings,groups}}`;
`apply=false` 返回差异预览,`apply=true` 落库;校验复用 §2.6 抽出的函数。

### 2.5 幂等键隔离(试跑关键)

| 函数 | 改动 |
| --- | --- |
| `_push_settlement(…, write_key: bool = True)` | 仅 `write_key` 为真才写 `settle_*` 键 |
| `_push_weekly_report_for_group(group, now, *, write_key: bool = True)` | 从 `tick_weekly_report` 循环体抽出;试跑传 False |
| `_push_signup_for_group(group, contest, tier, *, write_key: bool = True)` | 从 `tick_signup_reminders` 循环体抽出;试跑传 False |
| 早报试跑 | **不复用** `_maybe_morning_push`(它必写 `morning_*`);改用 `build_test_text + send_notification` |

要求:`write_key=True` 时与现逻辑**逐字一致**,现有测试保持绿。

### 2.6 校验复用(为 `/import` 抽函数)

从 `_web_config_set` 抽出(行为不变):

```python
async def _normalize_settings_payload(self, settings: dict, current: dict) -> dict: ...
def _normalize_groups_payload(self, groups: list) -> dict: ...   # {scoped_key: cfg}
```

`_web_config_set` 改为调用它们;`_web_import` 复用 + 追加:平台白名单、群 `group_id` 非空、`admin_users` 为字符串列表。

### 2.7 路由注册

在现有 3 条之后追加(同名路径不同方法分别注册):

```python
self.context.register_web_api(f"/{PLUGIN_NAME}/overview", self._web_overview, ["GET"], "后台概览")
self.context.register_web_api(f"/{PLUGIN_NAME}/push-log", self._web_push_log, ["GET"], "推送日志")
self.context.register_web_api(f"/{PLUGIN_NAME}/bindings", self._web_bindings_list, ["GET"], "账号绑定列表")
self.context.register_web_api(f"/{PLUGIN_NAME}/bindings", self._web_bindings_write, ["POST"], "账号绑定增删改")
self.context.register_web_api(f"/{PLUGIN_NAME}/rank", self._web_rank, ["GET"], "群排行只读")
self.context.register_web_api(f"/{PLUGIN_NAME}/run-now", self._web_run_now, ["POST"], "立即试跑")
self.context.register_web_api(f"/{PLUGIN_NAME}/import", self._web_import, ["POST"], "配置导入")
```

---

## 3. 前端实现(`pages/settings/index.html`)

### 3.1 行数预算

| 区 | 预计 |
| --- | --- |
| `<style>` | 550~700 |
| `<body>`(7 视图 + 抽屉 + 弹窗骨架) | 450~600 |
| `<script>` | 800~1200 |

### 3.2 HTML 骨架

```html
<aside id="nav">…7 项 <a class="nav-item" href="#/bindings" data-nav="bindings">账号绑定</a> …</aside>
<main>
  <header id="topbar">…平台实例 / 未保存徽章 / 导出…</header>
  <section data-view="overview" hidden>…</section>
  <section data-view="basics" hidden>…</section>
  <section data-view="settings" hidden>…7 个 <details class="group">…</section>
  <section data-view="groups" hidden>…</section>
  <section data-view="bindings" hidden>…工具栏 + 表格 + 分页…</section>
  <section data-view="rank" hidden>…</section>
  <section data-view="backup" hidden>…</section>
</main>
<div id="toasts"></div>
<aside id="drawer" hidden>…新增/编辑绑定表单…</aside>
<div id="modal" hidden>…确认/差异表…</div>
```

### 3.3 JS 状态与函数

```js
const state = {
  view: 'overview',
  config: { adminUsers: [], settings: {}, groups: [], platformId: '' },
  dirty: { basics: false, settings: false, groups: false },
  page: { groups: 1, size: 20, query: '', selected: new Set() },
  bindings: { items: [], total: 0, counts: {}, query: '', platform: '', page: 1, size: 20, selected: new Set() },
  data: { rank: null, overview: null, log: [] },
  ui: { saving: false, busy: null, editing: null },
};
```

函数清单:

| 函数 | 说明 |
| --- | --- |
| `apiGet/apiPost` | 包 bridge,统一错误 → Toast |
| `toast/confirmDialog/openDrawer/closeDrawer` | 全局反馈与抽屉 |
| `route/setView` | hash 路由 + 脏检查拦截 |
| `renderOverview/renderBasics/renderSettings/renderGroups/renderBindings/renderRank/renderBackup` | 各视图渲染 |
| `markDirty/clearDirty/syncDirtyBadge` | 脏标记 |
| `collectSettings/validateSettings/collectGroups` | 表单收集与校验(规则与后端同源) |
| `applyGroupFilter/renderGroupsPage/batchSetEnabled/batchTest` | 群列表 |
| `renderBindingsPage/openBindingForm/submitBinding/deleteBindings` | **绑定 CRUD** |
| `runNow` / `doExport` / `doImport` / `showDiff` | 试跑与备份 |

- 事件委托(`data-action`);全部文本 `textContent`。

### 3.4 绑定 CRUD 前端细节

- 列表:分页(默认 20/页)+ 平台筛选 + 关键词;行内“编辑/删除”;表头全选当前页。
- 抽屉表单:`user_id / platform / identifier / qq_name / group_id`;平台切换时更新账号输入框的 placeholder 提示。
- 提交:前端必填与格式校验 → 按钮 loading(“正在校验账号…”)→ `apiPost('bindings', {action:'save', …})`。
- **覆盖确认**:若已知该 user+platform 已有绑定(列表里能查到),提交前弹“将替换原账号 X,是否继续?”。
- 删除:单条二次确认;批量删除列出条数。
- 成功后:重新拉列表(或本地更新)+ Toast;并清空选择。

### 3.5 群列表算法

同前版:过滤 → `slice` 分页 → 全选当前页 → 批量只改内存并置脏 → 行内改动高亮 + 还原本行。

### 3.6 脏标记与离开拦截

控件变更置脏;保存成功清脏;切换视图/`beforeunload` 有脏 → 确认。

### 3.7 导入导出 / 试跑

- 导出:`apiGet('config')` → Blob → bridge `download`。
- 导入:`apiPost('import',{apply:false,…})` → 差异表 → 确认 → `apply:true`。
- 试跑:选群/类型/场次 → 结果步骤条 → 概览可见日志。

### 3.8 无障碍与安全

焦点可见;图标按钮 `aria-label`;对话框焦点陷阱 + Esc;全部 `textContent`;无 i18n。

---

## 4. 测试

### 4.1 `tests/test_web_admin.py`(新增)

| 用例 | 断言 |
| --- | --- |
| `overview` | 群数/管理员数/`store_backend`/`next_pushes` 字段齐全 |
| `push-log` 截断 | 写 250 条只留 200,倒序返回 |
| `bindings 查` | 两用户三平台 → 3 条;`platform=codeforces` 剩 1 条;`q` 匹配昵称 |
| `bindings 增` | 调 `get_profile(force=True)` + `save_binding(user_id, platform, profile, qq_name, group_id)` |
| `bindings 改(覆盖)` | 已有绑定时返回 `replaced=旧 handle`,且只调用一次保存 |
| `bindings 增-冲突` | `save_binding` 抛 `ValueError` → `error_response` 且**不失效缓存** |
| `bindings 增-非法账号` | 归一化失败 → 不抓取、不保存,返回平台格式提示 |
| `bindings 删` | `remove_binding` 调用 + `_invalidate_all_rank_cache` 调用 |
| `bindings 批量删` | 3 条中 2 条成功 → `removed=2` |
| `bindings 缺参` | 缺 `user_id`/`platform`/`identifier` → 逐项错误 |
| `rank 只读` | 默认 `allow_stale=True, force=False`;`refresh=1` → `force=True` |
| `import 校验` | `settle_delay_minutes=999` → 整体拒绝并列出字段 |
| `import 预览不落库` | `apply:false` 后 KV 未变化 |

### 4.2 `tests/test_admin_web_run.py`(新增)

| 用例 | 断言 |
| --- | --- |
| `_push_settlement(write_key=False)` | 不写 `settle_*` |
| `_push_weekly_report_for_group(write_key=False)` | 不写 `weekly_*` |
| `_push_signup_for_group(write_key=False)` | 不写 `signup_*` |
| 默认 `write_key=True` | 与现逻辑一致(回归) |
| 试跑后正式 tick | 仍能正常推送 |

### 4.3 前端手测清单

1. 7 个视图切换 + 刷新后停留原 hash。
2. 19 项设置改动 → 未保存徽章 → 保存成功消失;非法值就地报错。
3. 群列表:搜索/分页/全选当前页/批量禁用/批量测试。
4. **绑定**:新增成功、编辑换绑(覆盖确认)、删除、批量删除、冲突提示、非法账号提示、抓取中转圈。
5. 排行:默认快照 + “较旧”标记 + 刷新。
6. 试跑:未就绪群给原因;正常群出步骤结果。
7. 导入:非法 JSON / 字段非法 / 正常文件(预览→确认)。
8. 窄屏(≤820px)抽屉与卡片式表格。

### 4.4 回归

`pytest tests -q` 全绿(当前 336,**只增不减**);`ruff check --select F,E9` 无新增问题。

---

## 5. 部署与验证(确认后才做)

1. 本地跑绿 → 提交推送。
2. 生产:备份到 `data/plugins/.backups/` → 覆盖 `pages/settings/index.html` 与 `main.py`(及测试/文档)。
3. 重载插件(或重启 AstrBot)让后端新接口生效。
4. 按 §4.3 逐项核对。

---

## 6. DoD

- [ ] 7 个新接口实现,现有 3 个接口契约不变
- [ ] 19 项设置 + 管理员 + 群配置(含新增 3 个可见字段)可读可写
- [ ] **绑定增删改查**全部可用(含冲突拒绝、批量删除、缓存失效)
- [ ] 群排行只读可用(默认不触发全群抓取)
- [ ] 运行状态 + 立即试跑可用,试跑不写幂等键
- [ ] 导入导出(预览→确认)可用
- [ ] 分区独立保存 + 未保存提示 + 字段级校验
- [ ] 桌面/窄屏均可操作
- [ ] 测试只增不减、ruff 通过
- [ ] 生产核对通过、文档与版本号同步(1.17.0)

---

## 7. 与 QQ 指令绑定的关系(避免误解)

后台“账号绑定”与群里「@某人 绑定cf」**写的是同一张表**(`AccountRegistry`):

- 后台新增/换绑 = 群里代绑定的等价操作(同样跳过验证码、同样受唯一约束保护)。
- 后台删除 = 群里「解绑」的等价操作。
- 因此**不存在两套数据**,后台改完,群里的战绩卡/排行立即按新绑定计算。

---

## 8. 批次建议

| 批次 | 内容 | 准出 |
| --- | --- | --- |
| B1 | 后端:推送日志 + 概览 + 日志接口 + 路由 | 新接口单测绿 |
| B2 | 后端:绑定**列表(GET)** | 单测绿 |
| B3 | 后端:绑定**增/改/删(POST)** + 缓存失效 | 含冲突/批量/非法用例绿 |
| B4 | 后端:排行只读接口 | 单测绿 |
| B5 | 后端:抽校验函数让 `config` 复用 + `/import` | 现有 `_web_config_set` 回归绿 |
| B6 | 后端:试跑 + 幂等隔离 | 幂等用例绿 |
| B7 | 前端:骨架 + 令牌 + 概览/基础 | 手测 1、2 过 |
| B8 | 前端:推送设置分区 + 脏标记/校验 | 手测 2 过 |
| B9 | 前端:群列表 搜索/分页/批量/行内 | 手测 3 过 |
| B10 | 前端:绑定 CRUD(抽屉/覆盖确认/批量删) | 手测 4 过 |
| B11 | 前端:排行 + 备份 + 试跑 | 手测 5~7 过 |
| B12 | 响应式与无障碍打磨 + 文档版本 | 手测 8、DoD 全过 |

---

> **再次确认:以上尚未执行。** 你说“开始”后我按 B1~B12 改代码并逐批跑测试;生产部署前会再向你确认一次。
