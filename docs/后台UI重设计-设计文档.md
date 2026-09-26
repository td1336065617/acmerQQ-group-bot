# acmerQQ群机器人 后台 UI 重设计 · 设计文档

> 状态:**待评审(未实现)**
> 上游:`docs/后台UI重设计-需求文档.md`
> 约束:零依赖、单文件 `pages/settings/index.html`、原生 CSS/JS、仅用 bridge `ready/apiGet/apiPost/download`

---

## 1. 信息架构(IA)

左侧导航 **7** 项,内容区单页切换(hash 路由):

| # | 导航 | 视图 | 内容 |
| --- | --- | --- | --- |
| 1 | 概览与状态 | `#/overview` | 概览数字、最近推送、下次推送、存储/平台状态 |
| 2 | 基础设置 | `#/basics` | 管理员列表、平台实例、主动推送说明 |
| 3 | 推送设置 | `#/settings` | 7 个可折叠分组 |
| 4 | 群推送配置 | `#/groups` | 搜索/分页/批量/行内编辑/测试推送 |
| 5 | 账号绑定 | `#/bindings` | **增删改查**:列表 + 新增/编辑弹窗 + 删除/批量删除 |
| 6 | 群排行 | `#/rank` | 群 + 平台选择、只读排行、刷新 |
| 7 | 配置备份 | `#/backup` | 导出、导入(预览差异 + 确认) |

hash 路由:非法 hash 回落 `#/overview`。

## 2. 视觉设计

### 2.1 设计令牌(在现有变量基础上扩展)

保留 `--ely-deep/--ely-plum/--ely-pink/--ely-pink-soft/--ely-ice/--ely-pearl/--ely-line`。

| 类别 | 令牌 | 建议值 |
| --- | --- | --- |
| 语义色 | `--ok/--warn/--err/--info` | 现成功绿/危险粉 + 琥珀 + 青 |
| 文本 | `--fg-1/--fg-2/--fg-3` | 主/次/弱 |
| 间距 | `--sp-1..6` | 4/8/12/16/24/32 |
| 圆角 | `--r-sm/--r-md/--r-lg/--r-pill` | 8/12/18/999 |
| 阴影 | `--sh-1/--sh-2` | 卡片/悬浮 |
| 动效 | `--t-fast/--t-base` | 120ms/200ms ease-out |

### 2.2 组件规范

卡片、区块标题、表单行、开关、分段控件、徽章、表格、按钮(主/次/危险/幽灵)、Toast、对话框、空态、骨架屏 —— 规格同前版设计(卡片保留顶部渐变条;表格粘性表头、窄屏转卡片行;按钮保存中 disabled+spinner)。

**新增组件**:
| 组件 | 用途 |
| --- | --- |
| 抽屉式表单弹窗 | 绑定的新增/编辑(字段较多,用抽屉比窄弹窗好填) |
| 差异表 | 导入预览(新增/修改/删除三色) |
| 结果步骤条 | 试跑逐步结果(构建 → 发送 → 周榜) |
| 状态点 | 概览/群列表的已激活、推送成功/失败 |

## 3. 布局与响应式

```
┌──────────────────────────────────────────────────────┐
│ 顶栏:标题 · 平台实例 · 未保存计数 · 导出 …           │
├────────────┬─────────────────────────────────────────┤
│ 侧栏导航    │  内容区(单页视图)                      │
│ 7 项       │                                         │
└────────────┴─────────────────────────────────────────┘
```

- 桌面:`grid-template-columns: 232px 1fr`
- ≤820px:侧栏抽屉 + 表格转卡片行
- ≤520px:标签置顶,输入占满宽

## 4. 交互设计

### 4.1 分区保存与脏标记
- 分区持有 `dirty` 标志;任一控件变更 → 置脏 → 标题出“未保存”徽章 + 保存按钮高亮。
- 切换视图 / `beforeunload` 有脏分区 → 弹确认。
- 保存:前端校验 → loading → `apiPost('config', …)` → 成功清脏 + Toast;失败保留脏 + 就近报错。

### 4.2 校验
- 前端规则表与后端一致;失焦即时校验,保存前整体校验;失败滚动到首个错误字段。

### 4.3 群列表
- 搜索(群 ID/平台)+ 20/页分页;全选作用当前页;批量启用/禁用只改内存 + 置脏,统一保存。
- 批量测试:选中群**串行**调 `test-push`,逐条结果。
- 行内改动 → 行高亮 + “还原本行”。

### 4.4 账号绑定(核心新增)

**列表**:复选框 | QQ 用户 | 平台 | 账号 | 昵称 | 绑定时间 | 操作(编辑/删除)。
工具栏:平台筛选、关键词搜索(QQ/账号/昵称)、分页、批量删除、“新增绑定”。

**新增/编辑(抽屉)**:
- 字段:QQ 用户 ID、平台、账号、昵称(选填)、归属群(选填,下拉已注册群)
- “编辑”时预填原值;平台可改(等于换平台绑定,会提示)。
- 提交前校验:必填、平台合法、账号格式(前端按平台给提示:CF/AtCoder 用户名,牛客/洛谷 数字 UID 或主页链接)。
- **覆盖确认**:若该用户在该平台已有绑定,弹“将替换原账号 X,是否继续?”。
- 后端返回:成功 → 刷新列表 + Toast;冲突(账号属他人) → 明确错误,表单保留。
- 保存后端会实时抓取资料,可能耗时 1~3 秒 → 按钮 loading + “正在校验账号…”。

**删除**:单条二次确认;批量删除列出将删条数后确认。

**只读不做的**:不提供“改绑他人账号归属”(若账号已被别人绑定,只能先删除原绑定)。

### 4.5 群排行(只读)
- 选群 + 平台 → 载入;默认命中快照(`allow_stale`),顶部显示“数据可能较旧 + 刷新”。
- 点刷新才 `force` 重算(可能较慢,显示 loading 与提示)。

### 4.6 试跑
- 选群 → 选类型 → 需要的选场次;前置校验结果就地提示(群未启用 / 会话未就绪)。
- 结果用步骤条展示并写入日志;**不写幂等键**。

### 4.7 导入导出
- 导出:`apiGet('config')` → 组装 JSON → bridge `download`;可选是否含群配置。
- 导入:选文件 → `apiPost('bindings'? no) → `apiPost('import', {apply:false})` 取差异预览 → 展示差异表 → 确认 → `apply:true` 落库。

## 5. 接口设计

现有(语义不变):`GET /{plugin}/config`、`POST /{plugin}/config`、`POST /{plugin}/test-push`。

新增:

| 方法 | 路径 | 用途 | 主要参数 / 返回 |
| --- | --- | --- | --- |
| GET | `/{plugin}/overview` | 概览与状态 | `{group_count, enabled_group_count, admin_count, platform_id, store_backend, next_pushes, recent_pushes}` |
| GET | `/{plugin}/push-log` | 推送日志 | `limit/group_id` → `{items, total}` |
| GET | `/{plugin}/bindings` | **查**:列表 | `platform/q/limit/offset` → `{total, items:[{user_id, platform, handle, display_name, qq_name, verified_at}], platform_counts}` |
| POST | `/{plugin}/bindings` | **增/改/删** | 见下 |
| GET | `/{plugin}/rank` | 排行只读 | `group_id/platform/progress/refresh` → `{rows, errors, stale}` |
| POST | `/{plugin}/run-now` | 立即试跑 | `group_id/kind/contest_id?` → `{ok, results:[{step, ok, message}]}` |
| POST | `/{plugin}/import` | 导入校验/应用 | `{apply, include_groups, payload}` |

### 5.1 `POST /{plugin}/bindings` 契约

```jsonc
// 增 / 改(action = save)
{ "action": "save", "user_id": "<QQ号/openid>", "platform": "codeforces",
  "identifier": "jiangly", "qq_name": "小明", "group_id": "123456" }

// 删(action = delete;items 支持批量)
{ "action": "delete", "items": [{ "user_id": "…", "platform": "codeforces" }] }
```

返回:
```jsonc
// save 成功
{ "status": "success", "data": { "action": "save",
  "item": { "user_id": "…", "platform": "codeforces", "handle": "jiangly", "qq_name": "小明" },
  "replaced": "old_handle_or_null" } }

// delete 成功
{ "status": "success", "data": { "action": "delete", "removed": 2 } }
```

错误(`error_response`):
- 缺少 `user_id` / `platform` / `identifier` → 逐项说明
- 平台不支持 / 账号格式非法 → 复用 `invalid_identifier_message`
- 抓取失败 → 复用 `_account_error_text` 同款文案
- 账号已被他人绑定 → “这个平台账号已经绑定到其他 QQ 用户,请先删除原绑定”

### 5.2 只读接口要点
- `bindings(查)`:数据源 `account_registry.get_all_accounts()` 展平;一次性返回 + 前端筛选。
- `rank`:默认 `allow_stale=True, force=False`,只有 `refresh=1` 才 `force=True`。
- `overview`:`store_backend = sqlite|kv`;`next_pushes` 只算早报/周报(赛果与报名提醒是事件驱动)。

## 6. 后端数据与副作用(绑定写操作)

| 步骤 | 说明 |
| --- | --- |
| 校验 | `user_id` 非空、`platform ∈ ACCOUNT_PLATFORMS`、`identifier` 归一化非空 |
| 抓取 | `account_fetcher.get_profile(platform, identifier, detail=False, force=True)` |
| 旧值 | `get_user_accounts(user_id).get(platform)` → 用于“已替换”与覆盖确认 |
| 写入 | `account_registry.save_binding(user_id, platform, profile, group_id=?, qq_name=?)` |
| 冲突 | `save_binding` 抛 `ValueError` → 明确拒绝(**不顶号**) |
| 删除 | `account_registry.remove_binding(user_id, platform)`(内部同时清理待确认绑定) |
| 缓存 | 写/删成功后 `_invalidate_all_rank_cache()` |
| 日志 | `logger.info("后台绑定/解绑 …")`(操作者记为 web 后台) |

> 与 QQ 指令绑定**共用同一套 Registry API**,因此“唯一账号约束”“双写 KV”“SQLite 回退”全部自动继承。

## 7. 新增 KV 键

| 键 | 结构 | 说明 |
| --- | --- | --- |
| `push_log` | `[{ts, group_id, kind, ok, detail}]` | 环形,最近 **200** 条 |

> 界面偏好(nav、page_size)用 `localStorage`,不进 KV。**不引入 i18n**。

## 8. 前端结构(单文件内分区)

```html
<style> P1 令牌 / P2 布局 / P3 组件 / P4 响应式 </style>
<body>
  <aside id=nav>…7 项…</aside>
  <main>
    <header id=topbar>…未保存计数 / 导出…</header>
    <section data-view=overview> …
    <section data-view=basics>   …
    <section data-view=settings> …7 分组…
    <section data-view=groups>   …
    <section data-view=bindings> …列表 + 抽屉…
    <section data-view=rank>     …
    <section data-view=backup>   …
  </main>
  <div id=toasts></div><div id=drawer></div><div id=modal></div>
</body>
<script> 1 状态 2 工具 3 渲染 4 动作 5 路由 6 启动 </script>
```

- 单一 `state` 对象 + 手动 `render(view)`;事件委托(`data-action`)。
- 全部文本 `textContent`,杜绝 XSS。
- 单文件超 1800 行时可拆 `app.css`/`app.js`(页面资源路由已支持),本期不做。

## 9. 降级与容错

| 情况 | 降级 |
| --- | --- |
| bridge 未就绪/超时 | 顶部横幅 + 禁用写操作 |
| 新接口 404(旧后端) | 对应视图提示“后端版本不支持”,其余可用 |
| 排行超时 | 旧快照 + “数据可能较旧” + 手动刷新 |
| 绑定抓取慢 | 按钮 loading“正在校验账号…”,失败保留表单 |
| 推送会话未就绪 | 试跑/测试推送就地说明原因,不发送 |
| 导入非法 | 整体拒绝并列明第几项 |

## 10. 风险与取舍

1. **单文件长度**:预计 1800~2500 行(新增绑定抽屉/差异表);用注释分区与稳定命名,必要时按 §8 拆分。
2. **绑定写入风险**:删/改会直接影响用户账号归属 → 全部二次确认 + 唯一约束保护。
3. **排行成本**:默认只读快照。
4. **试跑副作用**:不写幂等键。
5. **批量测试/批量删除**:串行执行 + 逐条反馈,避免打爆链路。
