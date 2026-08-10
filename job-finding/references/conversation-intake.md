# S0 新任务引导与配置契约

## 目录

1. S0 目标
2. 对话规则
3. 固定提问模板
4. 结构化数据
5. 配置写入与门禁

## 1. S0 目标

S0 通过固定问题收集用户的岗位目标、简历、个人画像、城市 URL 和搜索方式。用户确认完整任务卡后，生成唯一的 `{run_root}/job-research-data/config.json`。

后续阶段只读取结构化配置，不根据对话记忆重新理解用户。S0 不访问 BOSS，不执行 S1，不生成其他任务数据。

## 2. 对话规则

1. 新任务必须先按[任务创建与隔离契约](task-isolation.md)解析默认 `tasks_root`，但不得创建目录；用户确认完整任务卡后才创建唯一时间戳 `task_id` 和独立目录。
2. 每条回复只提出一个新问题或一个确认问题。
3. 使用本文的固定话术，只替换占位符内容，不临时改写问题。
4. 用户已经主动提供的信息必须吸收，已满足的问题可以跳过，不重复索要。
5. 不将任何测试用户的学历、城市、岗位、关键词或偏好作为默认值。
6. 模型可以根据用户需求扩展岗位方向和关键词，但必须展示完整列表并等待确认。
7. 用户修改信息时，只重新确认受影响的部分；方向或关键词变化时，必须重新展示完整列表。
8. 当前任务没有简历时必须索要。只有用户明确表示不提供，才能记录 `resume_status=declined`。
9. 不把推测写成个人事实，不保存简历全文、完整对话或求职无关隐私。
10. `进入下一步`只推进信息确认，不代表允许执行。
11. 只有用户对完整任务卡回复`确认执行`后才能创建任务目录并写入 `config.json`。
12. `TASKS_ROOT`必须使用 `task_manager.py resolve-root` 返回且经用户确认的绝对路径；`RUN_ROOT`必须使用确认后由 `task_manager.py create` 返回的新目录。不得使用模型猜测路径、旧任务目录或未经用户确认的当前工作区。

## 3. 固定提问模板

### 3.1 岗位目标

新任务使用以下开场白：

> 你好呀，我们先不急着开始搜岗位。  
> 找工作本来就已经够费心了，所以这次你不用一次把所有信息都准备得很完整——哪怕现在只有一个模糊方向、几个岗位词，或者几条“我绝对不想做什么”，都可以告诉我，我会陪你一点点整理清楚。  
>   
> 我们会分三小步完成配置：  
> 1. 先确定你想找的岗位方向和初筛依据；  
> 2. 再确认城市搜索范围和搜索方式；  
> 3. 最后一起核对任务卡。  
>   
> 第一步，先用一句话告诉我：这次你最想找哪一类岗位？

根据用户回答生成完整草案，使用：

> 我先这样理解你的方向：  
>   
> 岗位方向与关键词：  
> - 【方向 1】：关键词 1、关键词 2  
> - 【方向 2】：关键词 3、关键词 4  
>   
> 方向职责判断：  
> - 【方向 1】：核心职责定义；JD 中出现……时可以支持该方向  
> - 【方向 2】：核心职责定义；JD 中出现……时可以支持该方向  
>   
> 希望的核心工作特征：  
> - 特征 1  
>   
> 明确排除条件：  
> - 条件 1  
>   
> 软偏好：  
> - 偏好 1  
>   
> 这样理解准确吗？需要调整请直接告诉我；如果没问题，请回复“没问题”。

用户没有表达排除、软偏好或公司偏好时，省略对应部分，不自行补充。方向草案确认后才能进入简历环节。

### 3.2 简历与结构化画像

当前任务还没有简历时，使用：

> 接下来请提供你的简历。简历会用于提取学历、经历、能力和职业优势，便于后续初筛和评分。  
>   
> 如果你明确不提供简历，请回复“不提供简历”。

用户提供简历后，只提取第 4 节定义的字段。不提取电话、邮箱、证件号、家庭情况、详细住址或照片。用户已提供职业画像或补充描述时，一并提取并标记来源。

展示结构化结果时固定使用：

> 我已将你的材料整理为以下结构化画像：  
>   
> - 教育背景：……  
> - 工作、实习、项目或其他经历：……  
> - 专业与通用能力：……  
> - 证书、奖项、专利、论文或语言能力：……  
> - 职业优势：……  
> - 其他岗位准入事实：……  
>   
> 以上内容是否准确，并确认用于后续初筛和评分？需要修改请直接告诉我；如果无需调整，请回复“进入下一步”。

没有对应内容的分类显示“未提供”，不补造事实。

用户明确不提供简历时，使用：

> 好的，本次不使用简历。如果你希望结合个人背景，请简要说明学历、主要经历和能力；如果希望只按通用岗位条件筛选，请回复“只按通用条件筛选”。

如果用户选择通用筛选，固定确认：

> 好的，本次将只按你已确认的岗位目标、工作特征和排除条件进行初筛，不使用个人背景。  
> 这项设置需要调整吗？如果不需要，请回复“进入下一步”。

### 3.3 城市和 BOSS URL

使用：

> 接下来请一次性把所有想搜索的城市和对应的 BOSS 直聘筛选 URL 发给我。  
>   
> 请按下面的格式标注，保证城市与 URL 一一对应：  
>   
> - 城市 A：`https://www.zhipin.com/web/geek/jobs?...`  
> - 城市 B：`https://www.zhipin.com/web/geek/jobs?...`  
>   
> 你可以只提供一个城市，也可以一次提供多个城市。

每个 URL 必须执行：

```bash
python3 "$SKILL_DIR/scripts/task_config.py" inspect-url \
  --url "$SEARCH_URL" \
  --city-label "$CITY"
```

校验通过后使用：

> 我已整理出以下城市搜索范围：  
>   
> | 城市 | 筛选信息 | URL 状态 |  
> | --- | --- | --- |  
> | 城市 A | 已保留原链接中的筛选参数 | 可用 |  
> | 城市 B | 已保留原链接中的筛选参数 | 可用 |  
>   
> 以上城市与 URL 的对应关系是否正确？需要调整请直接告诉我；如果没问题，请回复“没问题”。

不向用户翻译没有可靠映射的不透明筛选码，最终任务卡不重复展示长 URL。

### 3.4 搜索方式

城市确认后使用：

> 城市范围已确认。接下来请选择搜索方式：  
>   
> 1. 尽可能多：每个“城市 × 关键词”组合自然搜索到结束；  
> 2. 每城市目标数量：当该城市累计达到 N 个初筛通过岗位后停止。  
>   
> 请选择一种方式。

用户选择每城市目标数量时，只继续询问一个正整数。

### 3.5 最终任务卡

信息齐全后，先取得默认任务根目录：

```bash
python3 "$SKILL_DIR/scripts/task_manager.py" resolve-root
```

用户此前已经要求自定义目录时，使用：

```bash
python3 "$SKILL_DIR/scripts/task_manager.py" resolve-root \
  --tasks-root "$USER_TASKS_ROOT"
```

命令无法解析合法路径时，只询问用户提供其他目录，不展示可确认执行的任务卡。返回`writable=false`但路径合法时仍展示任务卡，并在保存目录后注明“创建时可能需要系统写入授权”。路径可用时使用：

> 本次执行目标：  
>   
> - 城市：……  
> - 搜索模式：……  
> - 岗位方向与完整关键词：……  
> - 简历：已提供／用户明确不提供  
> - 初筛依据：个人背景／通用条件  
> - 候选人背景摘要：……  
> - 目标工作特征：……  
> - 明确排除：……  
> - 软偏好：……  
> - 公司偏好与关注风险：……  
> - 任务标识：确认执行后自动生成
> - 任务保存根目录：……
> - 独立任务目录：确认执行后在上述目录中自动创建 `task-时间戳`子目录
>   
> 如需更改任务保存目录，请直接提供新路径；如使用以上目录且其他信息无误，请回复“确认执行”。

通用筛选时把候选人背景显示为“不使用”。用户没有排除、软偏好或公司偏好时，对应项显示“无明确条件”。

用户提供新路径后，必须再次调用`resolve-root --tasks-root`，并重新展示包含新绝对路径的完整任务卡。任务卡展示期间不得创建 `tasks_root`、`run_root` 或 `task.json`。

### 3.6 过早确认

信息未齐全时收到`确认执行`，使用：

> 我已经记下你的执行意愿了。不过开始前还差一项必要信息，才能保证结果不会跑偏。  
> 【只询问下一项缺失信息】

## 4. 结构化数据

### 4.1 顶层结构

```json
{
  "schema_version": 2,
  "information_sources": {},
  "candidate_profile": {},
  "job_target": {},
  "company_preferences": {},
  "search_scope": {}
}
```

`config_hash`由程序加入。模型不得自行计算或填写。

### 4.2 `information_sources`

```json
{
  "resume_status": "provided",
  "items": [
    {
      "source_id": "resume_1",
      "source_type": "resume",
      "reference": "用户材料路径或稳定引用"
    },
    {
      "source_id": "user_job_target",
      "source_type": "user_statement",
      "reference": "intake:job_target"
    }
  ]
}
```

- `source_type`只能是 `resume`、`career_profile` 或 `user_statement`。
- 本地文件的 `content_hash` 由程序计算；对话陈述不保存内容哈希。
- `provided`必须有简历来源；`declined`不能存在简历来源。

### 4.3 `candidate_profile`

```json
{
  "basis": "personal",
  "education": [
    {
      "institution": "学校名称",
      "institution_attributes": ["已确认的学校属性"],
      "degree_level": "学位层级",
      "major": "专业",
      "start_date": "YYYY 或 YYYY-MM 或 null",
      "end_date": "YYYY 或 YYYY-MM 或 null",
      "is_current": false,
      "academic_highlights": [],
      "source_ids": ["resume_1"]
    }
  ],
  "experiences": [
    {
      "experience_type": "project",
      "organization": null,
      "name": "经历名称",
      "role": null,
      "start_date": null,
      "end_date": null,
      "is_current": false,
      "domains": [],
      "responsibilities": ["从材料中提取的实际职责"],
      "achievements": [],
      "source_ids": ["resume_1"]
    }
  ],
  "capabilities": [
    {
      "category": "technical",
      "name": "能力名称",
      "evidence": [],
      "source_ids": ["resume_1"]
    }
  ],
  "credentials": [
    {
      "credential_type": "certificate",
      "name": "证书或成果名称",
      "issuer": null,
      "issue_date": null,
      "details": null,
      "source_ids": ["resume_1"]
    }
  ],
  "career_strengths": [
    {
      "statement": "职业优势",
      "evidence": [],
      "source_ids": ["resume_1"]
    }
  ],
  "eligibility_facts": [
    {
      "fact_type": "事实类型",
      "value": "已确认的事实",
      "source_ids": ["resume_1"]
    }
  ]
}
```

- `experience_type`：`employment`、`internship`、`project`、`research`、`leadership`、`military`、`volunteer`、`other`。
- `capabilities.category`：`technical`、`product_business`、`solution_delivery`、`communication_management`、`domain_knowledge`、`language`、`other`。
- `credential_type`：`certificate`、`award`、`patent`、`publication`、`language`、`other`。
- `personal`必须至少有一条画像事实；`generic`的所有画像数组必须为空。
- 每条事实必须引用存在的 `source_ids`。

### 4.4 `job_target`

```json
{
  "target_directions": [
    {
      "name": "方向名称",
      "description": "核心职责定义",
      "positive_signals": ["JD 中可以支持该方向的职责"],
      "source_ids": ["user_job_target"]
    }
  ],
  "search_keywords": [
    {
      "term": "准确关键词",
      "directions": ["方向名称"]
    }
  ],
  "desired_work_features": [
    {
      "scope": "responsibility",
      "feature": "目标工作特征",
      "priority": "required",
      "source_ids": ["user_job_target"]
    }
  ],
  "hard_exclusions": [
    {
      "scope": "core_responsibility",
      "rule": "明确不能接受的条件",
      "source_ids": ["user_job_target"]
    }
  ],
  "soft_preferences": [
    {
      "scope": "work_style",
      "preference": "只用于排序或待核实的偏好",
      "source_ids": ["user_job_target"]
    }
  ]
}
```

- 方向和关键词顺序由程序生成。
- 关键词只能引用已确认的方向。
- `positive_signals`用于后续根据 JD 职责分类，不根据搜索词直接定向。
- `required`信息不明时只能待核实；只有 JD 直接命中 `hard_exclusions` 时才能据此淘汰。

### 4.5 `company_preferences`

```json
{
  "preferred_features": [
    {
      "category": "growth",
      "feature": "偏好的公司特征",
      "source_ids": ["user_job_target"]
    }
  ],
  "disqualifying_conditions": [
    {
      "category": "business_model",
      "condition": "影响最终推荐的公司条件",
      "source_ids": ["user_job_target"]
    }
  ],
  "risk_concerns": [
    {
      "category": "employment",
      "concern": "希望后续重点查证的风险",
      "source_ids": ["user_job_target"]
    }
  ]
}
```

公司偏好没有独立固定问题。只在用户主动表达时记录，否则三个数组保持为空。

### 4.6 `search_scope`

```json
{
  "search_mode": "exhaustive",
  "per_city_target_count": null,
  "search_urls": [
    {
      "city_label": "用户确认的城市",
      "url": "用户提供的 BOSS 筛选 URL"
    }
  ]
}
```

程序生成 `city`、`search_base` 和 `order`。`exhaustive`的目标数量必须为 `null`；`per_city_target`必须有正整数目标。该目标固定表示每城市 S3 `初筛通过`岗位数，不是 S1 原始采集数。

## 5. 配置写入与门禁

用户对完整任务卡回复`确认执行`后，先在任务卡确认的目录下创建任务：

```bash
python3 "$SKILL_DIR/scripts/task_manager.py" create \
  --tasks-root "$CONFIRMED_TASKS_ROOT"
```

保留命令返回的 `task_id`、`tasks_root` 和 `run_root`。然后模型只提交第 4 节的已确认字段：

```bash
python3 "$SKILL_DIR/scripts/task_config.py" prepare \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID" \
  --input -
```

程序负责：

- 严格验证字段、枚举和来源引用；
- 计算本地材料内容哈希；
- 生成方向、关键词和城市顺序；
- 生成 URL 派生字段和 `config_hash`；
- 将同一 `config_hash`绑定到 `task.json`，已有不同绑定时拒绝写入；
- 原子写入 `{run_root}/job-research-data/config.json`；
- 相同配置允许幂等复用，不同配置拒绝覆盖。

写入后立即执行：

```bash
python3 "$SKILL_DIR/scripts/task_config.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

S0 完成门禁：

- 用户已确认完整方向和关键词；
- 已提供简历，或用户明确拒绝提供；
- 个人画像已展示并确认，或已确认通用筛选；
- 至少一个目标方向、一个搜索关键词和一项目标工作特征；
- 至少一个合法城市 URL；
- 搜索方式和目标数量关系正确；
- 完整任务卡已展示；
- 任务卡已展示脚本解析出的合法 `tasks_root`；
- 当`tasks_root`需要工作区外写入授权时，已在创建阶段取得授权，或用户已改用可写目录；
- 用户已明确回复`确认执行`；
- `task_manager.py create`返回的`tasks_root`与任务卡确认值一致；
- `task_config.py validate` 通过。
- `task.json.config_hash`与`config.json.config_hash`一致。

校验通过只表示 S0 输入已固化，不代表 S1 或整个求职任务已经完成。
