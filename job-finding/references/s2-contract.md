# S2 岗位详情与 BOSS 工商主体契约

## 目的

根据 `job-index.json` 中的岗位身份逐个读取当前岗位详情，保存结构化 JD 摘要、有限原文证据和 BOSS 公司页展示的工商主体信息。S2 不判断岗位是否适合用户。

## 输入

- 已通过完整校验的当前 S1 批次；`per_city_target` 模式允许仍有后续搜索组合；
- 与 S1 `job-index.json` 一致的 S0 `config_hash`；
- `job-index.json` 中唯一存在的 `job_key`、`job_id` 和 `boss_job_url`；
- 浏览器读取器返回的当前岗位详情；
- 模型根据当前 JD 临时文本生成的结构化摘要和有限证据。

浏览器读取结果和模型摘要通过内存或标准输入传递，不落盘为中间 JSON。

S2 每次只能自动选择并处理 `job-index.json` 中首个没有详情终态的岗位。配置哈希不一致、岗位顺序不一致或已经处理过的 S1 岗位核心事实变化时停止。目标模式新增下一批岗位时，程序校验旧岗位的逐岗位输入哈希后更新顶层输入哈希，并继续处理新增岗位，不重复处理旧岗位。

## 浏览器规则

- 使用 `$web-access` 和已授权的 Chrome 会话。
- 一次只打开一个详情标签页，读取结束后关闭。
- 从 URL path 验证当前岗位 ID 与 S1 一致。
- 只从 `.job-detail-section` 读取 JD，允许回退到 `.job-detail`；不得读取 `document.body.innerText` 或推荐岗位区域。
- 从岗位顶部公司卡片取得 `/gongsi/{company-id}.html` 链接。
- 同一 BOSS 公司页已经存在时只建立引用，不重复访问。
- 新公司页只从 `.job-sec.company-business .business-detail li` 读取工商字段。
- 出现验证码、访问限制、岗位身份不一致或页面串位时立即停止，不得继续推测。

## 模型摘要

模型只生成：

```json
{
  "summary": {
    "core_responsibilities": [],
    "hard_requirements": [],
    "key_capability_and_tool_requirements": [],
    "work_style_and_risks": [],
    "missing_or_uncertain": []
  },
  "evidence": [
    {
      "category": "responsibility",
      "text": "当前 JD 中的有限原文片段"
    }
  ]
}
```

`evidence.category` 只能使用 `responsibility`、`requirement`、`capability`、`work_style` 或 `uncertainty`。每条证据必须是当前 JD 中不超过 300 字的原文片段，程序负责核对。

不得让模型填写阶段状态、内容指纹、证据 ID、公司主体键、企业名称、统一社会信用代码或其他工商字段。

## 输出

只新增 `job-research-data/job-details.json`。顶层保存 `config_hash` 和当前完整 `job-index.json` 的输入哈希；每条岗位详情另存其依赖的 S1 岗位事实哈希，以支持安全追加新批次。其中包含两类记录：

- `record_type=job_detail`：岗位详情状态、来源 URL、选择器、内容指纹、五组摘要和有限证据；
- `record_type=boss_company_subject`：BOSS 公司页 URL、招聘品牌、工商字段、关联岗位和冲突状态。

不得持久化名为 `jd_text`、`body`、`html`、`full_text`、`full_jd` 或 `page_text` 的字段。

## 工商主体规则

- `boss_company_subject_key` 只根据规范化 BOSS 公司页 URL 生成。
- `enterprise_name` 只从公司页工商区域的“企业名称”字段取得，不使用招聘品牌猜测。
- 保存页面实际披露的企业名称、统一社会信用代码、法定代表人、成立时间、注册资本和注册地址；缺失字段保持空字符串。
- 工商状态只能是 `已取得`、`未取得`、`查询失败` 或 `来源冲突`。
- BOSS 工商企业名称是后续网络查询锚点，不自动等同于劳动合同签约主体。
- 工商主体未取得、查询失败或来源冲突，不改变岗位详情终态，也不阻止该岗位进入 S3；该状态必须原样保存在 `boss_company_subject.business.status` 中。
- S4 只对最终取得可信企业名称的初筛通过岗位生成公司任务；其他初筛通过岗位由程序写入 `company-research.json.skipped_jobs`，跳过公司背调和评分，不得阻塞其他岗位。

## 失败与恢复

- 岗位详情终态只能是 `已完成`、`失败` 或 `需人工复核`。
- 详情失败时保存空摘要、空证据和结构化失败原因，不得根据标题补全。
- 每成功提交一个岗位就原子更新 `job-details.json`，中断后从首个没有详情记录的岗位继续。
- 重启时仍自动选择首个没有详情终态的岗位，不要求模型传递岗位 ID。
- 已处理岗位的岗位键、ID、详情 URL、岗位名或招聘品牌变化时停止复用旧 S2 结果；只追加新岗位或新增召回来源时保留旧结果。

## 验收条件

- 每个测试岗位的 ID、详情 URL 和 S1 一致；
- 成功摘要只包含当前 JD 支持的信息；
- 所有证据均能在当前 JD 中找到；
- 至少一个测试岗位成功取得 BOSS 工商企业名称；
- 企业名称未取得时仍能完成岗位详情，并保存明确工商状态；
- 同一公司页不会生成重复主体；
- `job-details.json` 不包含完整 JD、整页正文或 HTML；
- 中断后可以继续处理下一个未完成岗位；
- 校验通过。
