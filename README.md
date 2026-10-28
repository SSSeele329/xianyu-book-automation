# 闲鱼二手书运营自动化系统

一个面向二手书卖家的本地运营工作台源码，覆盖图书选品、货源匹配、价格计算和在售商品维护。系统通过本地 Web 操作台驱动浏览器自动化；没有托管网站、内置账号、平台凭据或线上服务。

## 功能范围

- **图书选品与整理：**从教材和考试类书目来源收集条目，以 ISBN 去重、筛除已在售商品，并保存选品进度。
- **货源匹配与价格计算：**查询孔夫子旧书网的图书信息，根据品相、货源排序和定价规则生成建议；匹配不足时保留人工确认路径。
- **在售商品维护：**按店铺和商品范围检查并批量维护标题、SKU、价格等信息。
- **任务运行管理：**提供本地任务状态、日志、进度和异常恢复能力，浏览器登录态与业务数据保存在本机。

## 技术结构

- Python 本地服务与操作台
- Playwright 浏览器自动化
- 孔夫子旧书网图书检索与商品信息处理
- 基于业务规则的选品、货源排序和定价
- 本地状态存储、任务进度管理与人工复核流程

源码按两个独立模块组织：`src/book_automation/` 负责选品与选品库工作流；`src/listing_optimizer/` 负责闲鱼在售商品优化。商业版授权系统、签名密钥管理、客户构建流水线和内部审计资料未包含在本仓库中。

## 本地运行

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/SSSeele329/xianyu-book-automation.git
cd xianyu-book-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

启动选品工作台：

```bash
python src/book_automation/local_web_app.py
```

启动在售商品优化工作台：

```bash
python src/listing_optimizer/local_web_app.py
```

两个模块可分别运行。首次运行时，在本地操作台按提示配置你有权使用的账号。选品模块如需连接自有 ERP，可通过环境变量提供配置：

```bash
export CYXY_ERP_URL="https://your-erp.example"
export CYXY_ERP_USERNAME="your-account"
export CYXY_ERP_PASSWORD="your-password"
```

需要 AI 辅助能力时，可在本地设置 `GEMINI_API_KEY`。不要把账号、密码、Cookie、API Key 或真实用户数据提交到 Git。

## 数据与安全

- 账号、Cookie、浏览器配置、任务状态和日志只应保存在本机；这些运行数据已加入忽略规则。
- 使用前请确认相关平台和 ERP 的服务条款、账号权限、自动化频率及数据使用许可。
- 批量改价或修改商品信息前，请人工核对筛选范围与预览结果。
- 本项目提供自动化能力，不保证外部页面结构、接口或平台规则持续不变。

## 许可证

本仓库代码采用 MIT License，详见 [`LICENSE`](LICENSE)。该许可证不授予第三方网站、商标、数据或 API 的使用权；相关权利与限制仍以各自权利方规则为准。
