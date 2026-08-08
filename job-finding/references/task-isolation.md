# 任务创建与隔离契约

## 目的

每次新求职任务使用独立目录和唯一 `task_id`，避免不同候选人、不同简历或不同岗位目标的数据相互混淆。

## 新建任务

新任务开始时，先执行：

```bash
python3 "$SKILL_DIR/scripts/task_manager.py" create \
  --tasks-root "$TASKS_ROOT"
```

程序用当前带时区时间生成 `task-YYYYMMDD-HHMMSS-ffffff`，并创建：

```text
{tasks_root}/{task_id}/
└── job-research-data/
    └── task.json
```

- `task_id`只使用程序生成的时间戳，不询问或接受用户自定义名称。
- `{run_root}`等于`{tasks_root}/{task_id}`。
- 已存在的任务目录绝不复用或覆盖。
- `task.json`只记录任务身份、创建时间和后续配置绑定，不保存简历正文或用户画像。

## 身份校验

对任务执行任何正式阶段前都必须携带同一个 `task_id`。S0–S6 所有读取或写入任务目录的正式命令都要求 `--task-id`，并在处理数据前执行同一项身份校验。基础校验命令为：

```bash
python3 "$SKILL_DIR/scripts/task_manager.py" validate \
  --run-root "$RUN_ROOT" \
  --task-id "$TASK_ID"
```

程序必须确认命令参数、目录名和 `task.json` 中的 `task_id` 三者一致；任一不一致立即停止。

## 继续旧任务

先列出任务：

```bash
python3 "$SKILL_DIR/scripts/task_manager.py" list \
  --tasks-root "$TASKS_ROOT"
```

`list`只返回可选任务，`selected_task_id`始终为`null`。即使只有一个任务，也必须由用户明确选择 `task_id`，不得按最近创建、目录顺序或对话记忆自动猜测。

## `task.json` 字段

| 字段 | 作用 |
| --- | --- |
| `schema_version` | 任务身份结构版本。 |
| `task_id` | 当前任务唯一时间戳标识，也是任务目录名。 |
| `created_at` | 带时区的 ISO 8601 创建时间，必须与 `task_id` 对应。 |
| `config_hash` | S0 配置绑定值；任务刚创建时为 `null`，S0 确认写入后只能绑定一次或幂等重试同一值。 |

`task_id`只用于选择并校验任务目录，不写入每条岗位、公司或评分记录，不参与语义判断和评分。后续阶段仍基于上一步结构化 JSON 与现有哈希继续执行。
