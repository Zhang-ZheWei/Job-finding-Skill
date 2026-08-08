# Job-finding

一个面向 Codex 的中文求职岗位研究 skill：从用户目标与简历出发，分阶段采集 BOSS 直聘岗位、提取有限岗位证据、初筛、公司网络背调、评分，并生成可追溯的岗位决策报告。

> 仅在你有权访问招聘平台且遵守其服务条款、当地法律和数据使用要求的前提下使用。遇到登录墙、验证码或安全验证时，skill 会停止，不会尝试绕过访问控制。

## 能做什么

- **S0**：固定提问流程，固化求职目标、个人画像和搜索范围。
- **S1–S2**：按已确认的城市 URL 和关键词采集岗位索引，并生成有限的职位摘要与工商主体信息。
- **S3–S5**：基于证据初筛岗位、对通过岗位的公司进行网络背调，并给出综合评分。
- **S6**：从结构化结果生成 Markdown 岗位决策报告。

每一阶段必须由用户确认后单独执行；不会自行推进到下一阶段。

## 安装

将本仓库克隆到 Codex 的 skills 目录，或将其中的 `job-finding/` 文件夹复制到你的 skills 目录：

```bash
git clone https://github.com/Zhang-ZheWei/Job-finding.git
cp -R Job-finding/job-finding "$CODEX_HOME/skills/job-finding"
```

然后在 Codex 中使用：

```text
使用 $job-finding 帮我分阶段采集、初筛、背调、评分并生成 BOSS 岗位决策报告。
```

## 依赖

- Python 3（仅使用标准库）
- Node.js 24：S1 / S2 的浏览器交互脚本需要
- 已安装并可用的 `$web-access` skill，以及已授权的 Chrome 会话

请先在本机检查这些依赖；本项目不会保存登录凭据、Cookie、完整职位正文或页面 HTML。

## 目录说明

```text
job-finding/
├── SKILL.md          # 主工作流与安全边界
├── agents/           # Codex 界面元数据
├── references/       # S0–S6 阶段契约
└── scripts/          # 配置、结构化存储与报告生成脚本
```

## 开发与校验

```bash
python3 -m compileall -q job-finding/scripts
node --check job-finding/scripts/boss_collect_s1.mjs
node --check job-finding/scripts/boss_read_s2.mjs
```


## 贡献与反馈

欢迎通过 [Issues](https://github.com/Zhang-ZheWei/Job-finding/issues) 报告问题或提出建议。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)，并在提交变更时同步更新 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

本项目采用 [MIT License](LICENSE)。
