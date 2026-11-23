# Daily Robot Report（具身智能与机器人科研日报）

基于 **GitHub Actions + GitHub Pages + Gemini API** 的全自动科研追踪流水线（在 `RedBeanCake/daily-robot-report` 基础上的工程化重构版），聚焦四大研究方向：
**`VLA`**（视觉-语言-动作大模型）、**`WAM`**（世界-动作模型与视频世界模型）、**`Robot-Agent`**（具身智能体规划与自纠）、**`Robot-RL`**（机器人强化学习与 Sim2Real）。

---

## 核心特性

1. **云端全自动运行 & 免费托管**：工作日北京时间 **10:00** 由 GitHub Actions 自动触发，无需自备服务器或代理即可直连 arXiv 与 Google Gemini API；生成的前端页面自动发布至 GitHub Pages（`https://<你的用户名>.github.io/<仓库名>/`）。
2. **多学科抓取 + 节假日去重**：全量抓取 `cs.RO` 当日最新 `New + Cross` 论文（自动剔除旧论文 Revision/Replacement），同时对 `cs.CV`、`cs.LG`、`cs.AI` 先做本地正则预筛并跨类去重；通过 `state/seen_papers.json` 持久化已处理 ID，避免 arXiv 休刊日重复推送。
3. **HTML + PDF 双引擎全文抽取**：显式提取论文作者与机构脚注（修复原版“机构未提及”问题），按 **实验 > 方法 > 引言 > 相关工作 > 附录** 优先级裁剪至 8 万字符；若 arXiv HTML 尚未生成，自动回退 `PyMuPDF` 提取 PDF 全文并剥离参考文献与外部代码链接。
4. **模型兼容 & 限流退避**：默认支持 Google Gemini 官方 OpenAI 兼容接口（`gemini-3.8-flash`），同时兼容百炼 DashScope、DeepSeek、OpenAI 等；内置 429/配额退避重试与批间节流。
5. **飞书 + 邮件双通道富媒体推送**：自动发送合规 `lark_md` 飞书交互卡片（超长自动分片，支持签名校验）与响应式 HTML 邮件（可选附带离线完整 `.html` 精读文档）。

---

## GitHub 公开仓库 4 步部署指南

### 第 1 步：创建公开仓库并推送代码

在 GitHub 新建一个 **Public** 仓库（例如 `daily-robot-report`），将本项目代码推送到 `main` 分支（请确认不包含 `.env`）。

### 第 2 步：开启 GitHub Actions 写入权限与 GitHub Pages

1. 进入仓库 **`Settings → Actions → General`**：
   - 将 **Workflow permissions** 勾选为 **`Read and write permissions`** 并保存（使 Actions 能自动提交每日归档并部署 `gh-pages`）。
2. 首次运行成功生成 `gh-pages` 分支后，进入 **`Settings → Pages`**：
   - **Source** 选择 `Deploy from a branch`，分支选择 **`gh-pages`** `/ (root)` 并保存。

### 第 3 步：配置 Secrets（密钥）

进入仓库 **`Settings → Secrets and variables → Actions → New repository secret`**，按需添加：

| Secret 名称 | 是否必填 | 说明 |
| :--- | :--- | :--- |
| `GEMINI_API_KEY` | **必填**（与 `LLM_API_KEY` 二选一） | Google AI Studio 获取的 Gemini API Key（支持 `AQ.` / `AIza` 密钥，默认调用 `gemini-3.8-flash`，高峰拥堵时自动回退至 `gemini-3.7-flash` / `gemini-3.5-flash-lite`） |
| `LLM_API_KEY` | 可选（改用百炼/DeepSeek/OpenAI 时填） | 若同时设置 `LLM_BASE_URL` / `LLM_FILTER_MODEL` / `LLM_DEEP_MODEL` 可切换任意模型 |
| `FEISHU_WEBHOOK` | 推荐（与邮箱至少配一组） | 飞书群自定义机器人的 Webhook URL |
| `FEISHU_SECRET` | 可选 | 若飞书机器人启用了“签名校验”，填入对应密钥 |
| `SMTP_HOST` | 可选（邮件推送用） | 如 QQ 邮箱 `smtp.qq.com`、163 邮箱 `smtp.163.com`、Gmail `smtp.gmail.com` |
| `SMTP_PORT` | 可选 | QQ/163 SSL 填 `465`，Gmail STARTTLS 填 `587` |
| `SMTP_USE_SSL` | 可选 | `465` 端口填 `true`，`587` 端口填 `false` |
| `SMTP_USER` | 可选 | 发件人邮箱账号 |
| `SMTP_PASSWORD` | 可选 | **邮箱授权码 / App Password**（非网页登录密码） |
| `EMAIL_FROM` | 可选 | 发件人地址（通常同 `SMTP_USER`） |
| `EMAIL_TO` | 可选 | 收件人地址，多个邮箱用英文逗号 `,` 分隔 |

### 第 4 步：手动试跑验证

进入仓库 **`Actions → Daily Robot Report → Run workflow`**：
- 首次建议可填 `paper_ids: 2611.24940`（`mode: deep`）快速验证 Gemini 解析与飞书/邮件推送是否通畅；
- 验证无误后直接点默认 `mode: auto` 跑当日全量日报。之后每周一至周五北京时间 `10:00` 会全自动运行。

---

## 本地调试命令（可选）

```bash
cp .env.example .env && chmod 600 .env   # 编辑填入密钥
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 离线全流程演练（不消耗 Token、不发送真实推送）
.venv/bin/python main.py --dry-run --mock-llm --max-deep 3

# 单元测试
.venv/bin/pytest -v tests/test_pipeline.py

# 启动本地网页预览（http://127.0.0.1:8765）
bash ops/serve_web.sh
```
