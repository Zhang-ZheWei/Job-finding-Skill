---
name: job-finding
description: >-
  当用户提出“帮我找工作、找岗位或推荐职位”“搜索、搜集或采集 BOSS 直聘岗位”“帮我筛岗位、根据简历筛岗位或判断岗位是否适合”“岗位初筛、公司背调、岗位评分或生成岗位报告”“开始或继续岗位搜索任务”等请求时使用。
  通过 S0 固定提问模板配置岗位搜索任务，并分阶段采集 BOSS 直聘岗位信息。用于引导用户确认
  岗位目标、简历、结构化个人画像、城市 URL 和搜索方式，生成带信息来源的 config.json；或执行 S1 岗位索引采集，或根据 S1 结果执行
  S2 岗位详情摘要与 BOSS 公司页工商主体提取，或根据 S2 证据执行 S3 岗位初筛，或对通过岗位执行 S4 公司网络背调、S5 综合评分和 S6 最终报告。
  支持岗位身份校验、有限 JD 证据、公司页去重、方向枚举、公司来源证据、组内权重重分配、结构化 JSON 写入和安全 Markdown 渲染。
---

# 岗位搜索与评分

只执行用户当前确认的阶段，不自动进入下一阶段。

## 任务创建与隔离

新任务开始前先完整阅读并执行[任务创建与隔离契约](references/task-isolation.md)。先调用 `task_manager.py resolve-root` 解析系统默认或用户指定的 `tasks_root`，只在 S0 任务卡中展示真实绝对路径，不提前创建目录。用户确认完整任务卡后才调用 `task_manager.py create`，由程序使用当前时间戳生成唯一 `task_id` 和独立 `{run_root}`；不得询问用户自定义任务名称，不得把当前工作区或旧任务目录直接作为新任务目录。

继续旧任务时调用 `task_manager.py list`，向用户展示可选时间戳标识并要求明确选择。即使只有一个历史任务，也不得自动选择。S0–S6 所有读取或写入任务目录的正式命令都必须携带同一个 `--task-id`；程序统一确认 `task_id`、目录名和 `task.json` 一致。

## S0 新任务入口

新任务必须先完整阅读并执行[S0 新任务引导与配置契约](references/conversation-intake.md)。按契约的固定话术和顺序提问，只替换占位内容；已经由用户主动提供的信息可以跳过重复询问。当前任务没有简历时必须索要，只有用户明确拒绝后才能跳过。

将用户回答、简历和职业画像统一转换为契约定义的 `information_sources`、`candidate_profile`、`job_target`、`company_preferences` 和 `search_scope`。每条个人事实和偏好必须引用信息来源，不得保存简历全文或将推测写成事实。在最终任务卡确认前禁止访问 BOSS 或执行 S1。

只有用户对完整任务卡明确回复`确认执行`后，才能在任务卡确认的 `tasks_root` 下创建任务，再调用 `task_config.py prepare` 写入 `{run_root}/job-research-data/config.json`。写入后必须调用 `task_config.py validate`。配置缺失、校验失败或已有不同配置时停止。

S0 配置完成只表示任务输入已经固定，不代表任何采集阶段完成。S1、S2 和 S3 均继承同一 `config_hash`；S3 直接从该配置生成只读筛选视图，不重复创建用户画像文件。

## 阅读契约

新任务或继续旧任务先完整阅读[任务创建与隔离契约](references/task-isolation.md)。新任务再完整阅读[新任务引导与配置契约](references/conversation-intake.md)。执行 S1 前完整阅读 [S1 采集契约](references/s1-contract.md)。执行 S2 前完整阅读 [S2 详情与工商主体契约](references/s2-contract.md)。执行 S3 前完整阅读 [S3 岗位初筛契约](references/s3-contract.md)。执行 S4 前完整阅读 [S4 公司网络背调契约](references/s4-contract.md)。执行 S5 前完整阅读 [S5 综合评分契约](references/s5-contract.md)。执行 S6 前完整阅读 [S6 最终报告契约](references/s6-contract.md)。

## 执行 S1 配置驱动采集

1. 先验证 S0 配置，并取得首个待处理组合：

```bash
python3 "$SKILL_DIR/scripts/task_config.py" validate --run-root "$RUN_ROOT" --task-id "$TASK_ID"
python3 "$SKILL_DIR/scripts/s1_store.py" next --run-root "$RUN_ROOT" --task-id "$TASK_ID"
```

组合只能由 `config.json` 中的城市 URL 和关键词生成。不得由模型重新输入、改写或调整顺序。

2. 调用 `$web-access`，完成依赖检查，显示自动化风险提示，并使用已授权的 Chrome 会话。取得工作区 Node 24 的绝对路径。
3. 每次只采集一个组合，只创建一个后台搜索标签页：

```bash
python3 "$SKILL_DIR/scripts/collect_s1.py" \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --node-bin "$NODE24_BIN"
```

脚本自动读取配置并选择首个未完成组合。每个实际执行的组合都自然采集到结束，再把结果合并进全局索引；不得并行执行多个组合。

如果命令返回 `status=manual_scroll_required`，立即暂停，不得执行 S2、重跑新标签页或把当前组合报告为完成。向用户说明当前关键词、首屏数量和现有数量，并请用户只在返回的 `target_id` 对应搜索标签页中手动滚动一次。

`exhaustive` 模式继续执行下一个 S1 组合。`per_city_target` 模式每完成一个 S1 组合后，必须先完成当前全部新增岗位的 S2 和 S3，再次调用 `s1_store.py next`。程序只按 S3 `初筛通过`且 `report_city` 为该城市的岗位计数：未达标时返回该城市下一个组合，达标时把该城市剩余组合标记为 `skipped_target`。不得用 S1 原始岗位数代替目标数。

用户确认完成滚动后，用返回的准确 `target_id`、原始首屏数量和用户确认的滚动次数续接同一组合。该入口同时支持限量验收和正式 `exhaustive` 组合；不得导航、替用户继续滚动或关闭该标签页：

```bash
python3 "$SKILL_DIR/scripts/collect_s1.py" \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --node-bin "$NODE24_BIN" \
  --existing-target "$TARGET_ID" \
  --initial-visible-count "$INITIAL_COUNT" \
  --scroll-rounds 1
```

只有已经实际观察到滚动前数量，并且用户确认完成滚动时，才能使用该备用方式。续接后如果仍返回 `manual_scroll_required`，继续暂停并如实报告，不得写入完成状态。

4. 每个组合写入后执行验证：

```bash
python3 "$SKILL_DIR/scripts/s1_store.py" validate --run-root "$RUN_ROOT" --task-id "$TASK_ID"
```

5. 报告当前城市、关键词、新增唯一岗位数、成功刷新轮数、当前城市 S3 初筛通过数、已完成/跳过/待处理组合数和需要用户处理的问题。中断后不得重跑已完成组合。

指定 URL 的 20 条滚动验收只在独立测试目录中使用 `--url`、`--city` 和 `--limit 20`，不得对正式配置任务使用该入口。

## 安全停止

出现以下情况时停止，不得声称成功：Chrome 不可用、用户未登录、出现验证码或安全验证、必要选择器失效、可见岗位卡片缺少有效身份，或者采集数量未达到要求。

关闭采集器自行创建的任务标签页，不得绕过访问控制。

## 限定范围

只写入：

- `{run_root}/job-research-data/job-index.json`
- `{run_root}/job-research-data/checkpoint.json`

不得打开岗位详情页、总结 JD、筛选岗位、识别公司主体、背调公司、评分或生成 Markdown 报告。

## 执行 S2 详情与工商主体提取

1. 验证当前 S1 批次，再查看 S2 首个待处理岗位：

```bash
python3 "$SKILL_DIR/scripts/s1_store.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"

python3 "$SKILL_DIR/scripts/s2_store.py" pending \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --limit 1
```

`per_city_target` 模式允许 S1 仍有后续组合，但当前 `job-index.json` 中的岗位必须按顺序全部处理。不得自行填写、跳过或重排岗位。

2. 调用 `$web-access`，取得工作区 Node 24 的绝对路径。读取首个待处理岗位详情和对应 BOSS 公司页：

```bash
python3 "$SKILL_DIR/scripts/read_s2.py" \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --node-bin "$NODE24_BIN"
```

脚本自动从 S2 待处理队列选择岗位。浏览器结果中的 `jd_text` 只能保留在当前处理过程中，不得写入文件。

3. 只根据当前 `jd_text` 生成契约规定的五组摘要和有限原文证据。不得根据岗位标题补造内容，不得输出筛选结论。
4. 将未经改写的浏览器结果和模型生成的 `semantic` 对象组成一个 JSON，通过标准输入直接提交，不创建中间载荷文件：

```bash
python3 "$SKILL_DIR/scripts/s2_store.py" upsert \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --input -
```

5. 每次只处理一个岗位。写入器只接受首个待处理岗位；提交成功后丢弃完整 JD，再读取下一个岗位。
6. 完成当前批次后验证：

```bash
python3 "$SKILL_DIR/scripts/s2_store.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

S2 只新增 `{run_root}/job-research-data/job-details.json`。不得写入完整 JD、整页正文、HTML、初筛、外部背调或评分字段。

## 执行 S3 岗位初筛

1. 取得首个待筛岗位、由 S0 配置生成的只读 `screening_context`，以及该岗位的 S2 摘要和证据：

```bash
python3 "$SKILL_DIR/scripts/s3_store.py" pending \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --limit 1
```

该命令会验证 S2 已全部完成、S0/S1/S2 哈希关系一致。校验失败时停止，不得跳过门禁或根据对话重新生成筛选画像。

2. 只根据 S3 契约、当前 `screening_context`、摘要和证据判断`初筛通过`、`可能无关`、`淘汰`或`无法判断`。不得读取网页、公司介绍或完整 JD。
3. 按契约提交结论、理由、同岗位证据 ID、待核实项和方向，通过标准输入直接写入：

```bash
python3 "$SKILL_DIR/scripts/s3_store.py" upsert \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --input -
```

4. 每次只处理一个岗位，写入器只接受首个待筛岗位。完成当前批次后验证：

```bash
python3 "$SKILL_DIR/scripts/s3_store.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

S3 只新增 `{run_root}/job-research-data/screening-results.json`，不生成 `screening-profile.json`。不得输出数值置信度、覆盖率、公司背调、分数或阶段完成声明。

5. `per_city_target` 模式完成当前批次 S3 后，必须回到 S1 调用 `s1_store.py next`。程序据此统计各城市初筛通过数并决定继续该城市下一个组合或跳过剩余组合；S1 全部终态后才能进入 S4。

## 执行 S4 公司网络背调

1. 取得首个待背调公司、关联岗位和 S0 公司偏好：

```bash
python3 "$SKILL_DIR/scripts/s4_store.py" pending \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --limit 1
```

该命令验证 S1 已无待处理组合且 S3 已全部完成，并只从`初筛通过`岗位和 S2 已取得的工商主体生成去重公司队列。企业名称未取得、查询失败或来源冲突的岗位由程序写入 `skipped_jobs`，跳过 S4 和 S5，但不阻断其他岗位；不得根据招聘品牌猜测企业名称。阶段哈希不可信时仍必须停止。

2. 严格执行 `pending.required_queries`：
   - 使用真实企业名称查询爱企查和公司官网；爱企查只采集行业、注册资本、实缴资本、成立日期、人员规模、营业收入和用于分析主营业务的简介；没有官网时记录未找到，并主要依据爱企查；
   - 爱企查的参保人数与员工人数分开记录；普通企业从企业年报读取销售总额或主营业务收入，上市企业可从财务指标读取营业总收入；未公示时如实记录，不得推算；
   - 在知乎、小红书、牛客网、脉脉中，分别使用每个品牌名和真实企业名称查询；
   - 每次网友评价查询都检查相关帖子和评论。
3. 调用 `$web-access`，一次只处理一家公司。必须在平台真实搜索框提交关键词，并在结果页核对页面显示的关键词与 `required_queries.search_term` 完全一致；`search_url` 只能复制搜索后的浏览器最终 `location.href`，不得猜测路径、手工拼接或手工编码。搜索页只记录查询动作；公司基本信息引用爱企查企业详情、企业年报、上市财务页面或官网原页，网友评价引用实际帖子或评论，公开风险引用政府、法院、监管机构或其他一手原页，不使用爱企查的平台风险统计。
4. 按 S4 契约提交完整 `query_attempts`、三个查询组和有限来源证据。模型使用当前提交内的 `source_ref` 建立引用，程序生成稳定证据 ID。通过标准输入直接写入，不创建中间载荷文件：

```bash
python3 "$SKILL_DIR/scripts/s4_store.py" upsert \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --input -
```

5. 每次只提交首个待背调公司。漏查平台或关键词、未检查帖子和评论、找到内容却没有原始来源时，写入器必须拒绝。部分平台无法访问时保存`部分完成`和失败证据，不得写成空结果；查询完成但没有可靠事项时保存`已完成`和空事项数组。
6. 完成当前批次后验证：

```bash
python3 "$SKILL_DIR/scripts/s4_store.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

S4 只新增 `{run_root}/job-research-data/company-research.json`。不得保存整页正文、HTML、搜索结果页正文、Cookie、置信度、覆盖率、风险分数、岗位评分或 Markdown 报告。

## 执行 S5 综合评分

1. S4 已按当前流程完成后，取得首个待评分岗位、固定评分锚点、从 S0 动态生成的只读评分条件以及 S2/S4 证据：

```bash
python3 "$SKILL_DIR/scripts/s5_store.py" pending \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --limit 1
```

S5 只处理前序流程已经交付并出现在 S4 公司记录中的岗位，不重新判断岗位是否应该通过 S3，也不审核、补查或修改 S0–S4。

2. 对 `job_match` 和 `company_evaluation` 的全部维度逐项选择`可评分`或`不可评分`：
   - `可评分`必须选择当前维度允许的锚点，并引用当前维度的 `criterion_id` 和合法证据 ID；
   - `不可评分`不选择锚点，如实说明缺失或冲突原因；
   - 岗位维度只使用当前岗位 S2 证据，公司维度只使用当前公司的 S4 对应组证据；
   - 硬性岗位条件已由 S3 处理，不在 S5 重复加分。
3. 按 S5 契约提交完整维度判断。模型不得提交权重、系数、分数、覆盖率、评级、哈希、阶段状态或复核字段：

```bash
python3 "$SKILL_DIR/scripts/s5_store.py" upsert \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --input -
```

4. 每次只提交首个待评分岗位。程序负责组内缺失权重重分配、Decimal 四舍五入、岗位分、公司分、综合分、评级和证据覆盖率。完成当前批次后验证：

```bash
python3 "$SKILL_DIR/scripts/s5_store.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

S5 只新增 `{run_root}/job-research-data/job-scores.json`。不得联网、重新打开网页、跨阶段复核、设置入选分数线或因低分删除岗位。某组完全不可评分时综合分为空，但岗位仍写入结果并交给最终报告。

## 执行 S6 最终报告

1. S6 不需要模型继续分析，也不联网。直接从当前 S0–S5 结构化结果生成报告：

```bash
python3 "$SKILL_DIR/scripts/s6_report.py" build \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

该命令先调用现有阶段验证器。S1–S5 任一阶段未完成、输入哈希过期或关系无效时必须停止，不得通过修改报告绕过门禁。

2. 生成后再次验证报告和 manifest：

```bash
python3 "$SKILL_DIR/scripts/s6_report.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

3. S6 只新增 `{run_root}/result/岗位决策报告.md` 和 `{run_root}/job-research-data/report-manifest.json`。总表按配置城市分组，字段顺序必须符合 S6 契约；岗位匹配分链接到岗位分析，公司评价分链接到去重后的公司背调卡片。所有 S5 岗位都保留，分数只用于排序。
4. 向用户交付时先用简短摘要说明：报告目的、主要内容、本轮结论、需要确认的问题和确认后可推进的动作，并提供最终报告的可点击文件链接。用户不需要先阅读实现文档。
