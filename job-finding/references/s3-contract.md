# S3 岗位初筛契约

## 目的

根据 S2 的结构化 JD 摘要、有限原文证据和 S0 `config.json` 中用户已经确认的结构化信息，判断岗位是否值得进入后续公司背调。S3 追求高召回、少误杀，不负责精细排序或评分。

本文件只规定通用筛选方法，不内置任何特定用户的学历、经历、城市、薪资、岗位方向或职业偏好。

## 输入与筛选视图

S3 只在 S2 已为当前 `job-index.json` 的全部岗位生成详情终态后启动。`per_city_target` 模式允许 S1 仍有后续组合。程序验证 S0、S1、S2 的配置哈希和输入关系，再从 `config.json` 生成只读 `screening_context`：

```json
{
  "candidate_profile": "config.json.candidate_profile 的完整结构化对象",
  "target_work_features": "config.json.job_target.desired_work_features",
  "hard_exclusions": "config.json.job_target.hard_exclusions",
  "soft_preferences": "config.json.job_target.soft_preferences",
  "target_directions": "config.json.job_target.target_directions"
}
```

字段作用：

- `candidate_profile`：按 `basis=personal` 或 `basis=generic` 判断是否使用个人学历、经历、能力和准入事实；
- `target_work_features`：判断岗位核心职责是否符合用户目标；
- `hard_exclusions`：只有 JD 直接命中其中一项时，才可据此淘汰；
- `soft_preferences`：信息不明时生成待核实项，不得直接当成淘汰条件；
- `target_directions`：使用方向描述和 `positive_signals` 判断岗位方向，不能由 Skill 固定写死。

`screening_context` 只随 `pending` 命令返回，不写成新的中间文件。不得根据对话记忆重新解释用户，也不得从简历或对话中私自增加用户未确认的硬性淘汰条件。`config.json` 缺失、校验失败、S2 未完成或哈希关系不一致时停止。

## 状态

- `初筛通过`：JD 有直接证据命中当前配置的目标工作特征，没有明确硬冲突；
- `可能无关`：存在一定相关性，但职责占比、工作性质或要求是否符合仍不明确；
- `淘汰`：JD 有直接证据命中当前配置的硬性排除项，或岗位硬性要求与候选人背景明确冲突；
- `无法判断`：S2 详情失败、需人工复核，或有效信息不足以形成业务判断。

`初筛通过`和`淘汰`必须引用至少一条同岗位 S2 证据。详情未完成时只能选择`无法判断`。

## 通用判断规则

1. 只根据当前岗位的 S2 摘要、证据和 S0 `screening_context` 判断；
2. 岗位标题、搜索关键词和公司介绍不能替代 JD 职责证据；
3. 判断核心职责，不因偶发、辅助或措辞模糊的任务直接通过或淘汰；
4. 命中 `hard_exclusions` 或明确资格冲突时可以淘汰，但理由必须指出对应配置与直接证据；
5. 只命中 `soft_preferences`、职责占比不明或要求措辞存在弹性时，使用`可能无关`或加入待核实项；
6. 没有在配置中声明的个人偏好不得作为淘汰理由；
7. 不得根据常识补造编码比例、销售指标、出差频率、工作地点或薪资条件；
8. 高召回优先：没有明确硬冲突时，不把不确定性伪装成淘汰结论。

## 岗位方向

一级方向和其他方向只能来自当前 `config.json.job_target.target_directions[].name`，并且必须有 JD 证据支持，不能仅根据岗位名称判断。

`初筛通过`必须有一级方向；其他状态允许一级方向为 `null`。

## 模型提交格式

模型只提交：

```json
{
  "job_key": "id:example",
  "status": "初筛通过",
  "reason": "岗位核心职责命中当前任务的目标工作特征",
  "evidence_ids": ["证据ID"],
  "items_to_verify": ["仍需核实的条件"],
  "reporting": {
    "primary_direction": "config.json 中用户确认的方向名称",
    "other_directions": []
  }
}
```

模型不得提交数值置信度、覆盖率、复核等级、详情哈希、配置哈希、版本或阶段完成状态。

## 程序生成字段

- `config_hash`：直接继承 S0 `config.json.config_hash`，证明本批初筛使用的是当前用户配置；
- `detail_record_hash`：证明初筛引用的是当前 S2 详情；
- `report_city`：从 S1 来源城市生成；
- `review_level`：根据详情状态、结论、证据和待核实项生成；
- `revision`：记录同一岗位初筛结论的修订版本。

复核等级规则：

- 详情未完成或状态为`无法判断`：`必须复核`；
- `可能无关`：有证据时`建议复核`，无证据时`必须复核`；
- `初筛通过`或`淘汰`：存在待核实项时`建议复核`，否则`无需复核`。

## 输出与验收

S3 只新增 `job-research-data/screening-results.json`，不生成 `screening-profile.json` 或其他中间事实文件。

每条结果必须引用当前 `job-details.json` 中同一岗位的合法证据 ID，不保存完整 JD，不读取网页，不进行公司背调或评分。已筛岗位详情或筛选配置发生变化时，程序必须阻止静默复用旧结果；只追加新批次详情时保留旧结果并继续筛选新增岗位。

`per_city_target` 完成当前批次后必须回到 S1。S1 只统计本文件中 `status=初筛通过`且 `reporting.report_city` 为对应城市的唯一岗位，决定继续下一个搜索组合或跳过该城市剩余组合。

每次只取得并提交首个待筛岗位，不能跳过或重排。验收至少覆盖：明确通过、明确淘汰、可能无关或无法判断中的三类；非法方向、跨岗位证据、模型置信度、S2 未完成、乱序提交、配置变化和详情失败却通过必须被程序拒绝。测试中的画像和淘汰条件只是 fixture，不得成为 Skill 默认规则。
