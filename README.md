# Job-finding Skill

[![当前版本](https://img.shields.io/badge/release-v0.1.1-blue)](https://github.com/Zhang-ZheWei/Job-finding-Skill/releases/tag/v0.1.1)
[![Validate skill](https://github.com/Zhang-ZheWei/Job-finding-Skill/actions/workflows/validate.yml/badge.svg)](https://github.com/Zhang-ZheWei/Job-finding-Skill/actions/workflows/validate.yml)
[![许可证](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一个面向 Codex 的中文岗位研究 skill。它根据你的求职目标和简历，分阶段完成 BOSS 直聘岗位采集、岗位初筛、公司背调、综合评分，并生成可追溯的岗位决策报告。

当前稳定版本：[`v0.1.1`](https://github.com/Zhang-ZheWei/Job-finding-Skill/releases/tag/v0.1.1)

> 本项目用于辅助岗位研究和求职决策，不替代使用者对岗位、公司及个人职业选择的独立判断。请在遵守目标网站服务条款和适用法律的前提下使用。

## 能做什么

- 引导确认岗位方向、简历、候选人条件、城市、关键词和公司偏好。
- 按城市和关键词采集 BOSS 直聘岗位，支持中断后继续。
- 提取有限岗位摘要，并根据简历和求职条件进行初筛。
- 对初筛通过岗位的公司进行基本信息、公开风险和员工评价背调。
- 对岗位匹配度和公司情况进行综合评分。
- 生成包含岗位排序、岗位分析和公司背调的 Markdown 报告。
- 将每次求职任务保存在独立目录，避免不同任务的数据混淆。

## 工作流程

| 阶段 | 内容 |
| --- | --- |
| S0 | 确认求职目标、简历、筛选条件、城市 URL、搜索方式和保存目录 |
| S1 | 采集岗位索引 |
| S2 | 读取岗位详情并生成有限摘要 |
| S3 | 根据候选人条件和岗位证据进行初筛 |
| S4 | 对通过初筛岗位的公司进行网络背调 |
| S5 | 计算岗位匹配分、公司评价分和综合评级 |
| S6 | 生成最终岗位决策报告 |

每个阶段独立执行。当前阶段完成后，skill 不会在未经允许的情况下自动进入下一阶段。

## 运行要求

- Codex
- Python 3
- Node.js 24
- [`web-access`](https://github.com/eze-is/web-access) skill
- 可用且已由用户授权的 Chrome 会话

S1、S2 和 S4 需要访问真实网页。登录状态、验证码、安全验证、网站结构变化或访问限制都可能导致对应阶段暂停。

## 安装

推荐直接使用 Codex Agent 从 GitHub 安装，无需手动复制文件。

将下面的内容发送给 Codex：

```text
请使用 $skill-installer 安装以下 GitHub Skill：

https://github.com/Zhang-ZheWei/Job-finding-Skill/tree/v0.1.1/job-finding

请安装到默认 Codex Skills 目录。安装前检查是否存在同名 skill；如果已经存在，请先告诉我，不要直接覆盖。

安装完成后，请确认 SKILL.md 已正确安装，告诉我实际安装路径，并提醒我该 skill 将在下一个 Codex 任务中可用。

同时检查 web-access skill 是否已经安装；如果缺少，只告诉我缺少该依赖，不要擅自安装。
```

## 开始使用

在 Codex 中输入：

```text
使用 $job-finding 根据我的目标和简历引导配置任务，采集、初筛、背调并生成 BOSS 岗位决策报告。
```

也可以直接表达需求：

```text
帮我根据简历筛选广州和深圳的 AI 产品岗位。
```

```text
继续我之前的岗位搜索任务。
```

新任务开始后，skill 会逐步确认：

1. 目标岗位方向和关键词；
2. 简历或是否明确不提供简历；
3. 硬性条件、软偏好和公司偏好；
4. 搜索城市及对应的 BOSS 城市 URL；
5. 搜索方式和目标数量；
6. 任务保存目录。

只有完整任务卡得到明确确认后，才会创建任务目录并开始正式执行。

## 输出内容

每次任务使用独立的 `task-时间戳` 目录，主要包含：

```text
task-YYYYMMDD-HHMMSS-ffffff/
├── job-research-data/       # 各阶段结构化数据
└── result/
    └── 岗位决策报告.md       # 最终报告
```

默认保存位置：

- macOS：用户“文稿/Documents”目录下的 `Codex岗位搜索任务`
- Windows：系统“文档”目录下的 `Codex岗位搜索任务`
- 其他系统：由用户提供保存目录

用户可以在确认任务卡之前更改保存目录。

## 更新

如果保留了克隆仓库，可以拉取最新稳定版本：

```bash
git switch main
git pull --ff-only origin main
```

然后重新将 `job-finding/` 同步到 Codex skills 目录。已发布版本与更新记录可查看 [Releases](https://github.com/Zhang-ZheWei/Job-finding-Skill/releases) 和 [CHANGELOG.md](CHANGELOG.md)。

## 隐私与限制

- 不保存完整简历、完整 JD、整页 HTML、Cookie 或搜索结果页正文。
- 不尝试绕过登录、验证码、安全验证、访问控制或反自动化措施。
- 不要在 Issue 中提交简历、联系方式、Cookie、Token、完整 JD 或其他隐私数据。
- 招聘信息可能过期、下线或被招聘方修改，申请前应重新核实。
- 公司背调结果仅用于求职参考，不构成法律、投资或雇佣建议。

## 反馈与许可证

- 使用 [Issues](https://github.com/Zhang-ZheWei/Job-finding-Skill/issues) 报告问题或提出建议。
- 本项目采用 [MIT License](LICENSE)。
