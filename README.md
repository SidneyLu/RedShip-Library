# OCR Library

桌面端扫描文献 OCR 图书馆：Electron + React 前端，本地 FastAPI sidecar 负责 PDF 渲染与 DashScope / 阿里云 MaaS 视觉模型识别，结果与索引保存在可移植的 **Library** 目录。

## 功能概览

- **图书馆**：导入 PDF（多选）、文件夹分类、搜索筛选、批量 OCR
- **工作台**：PDF 与版面块对照、Markdown / 质检、页码跳转、指定页重跑
- **OCR 流水线**：页级并行、多文档并发、可选多进程 Worker；断点续跑（跳过已完成页）
- **稳健性**：连接池与自适应限流、网络中断 / 内容安全审核页可跳过、单文档可手动停止 OCR
- **可移植数据**：配置同一 Library 文件夹即可在另一台电脑看到全部文档与 OCR 结果

## 技术栈

| 层 | 技术 |
|----|------|
| 桌面壳 | Electron 33 |
| UI | React 19 · Vite · Tailwind · pdf.js |
| Sidecar | Python · FastAPI · SQLAlchemy · PyMuPDF · DashScope SDK |
| 数据 | SQLite（`library.db`）+ 文件系统 `docs/` |

默认 sidecar 端口：`127.0.0.1:18765`。

## 仓库结构

```
ocr-desktop/
├── electron/           # Electron 主进程 / preload
├── src/                # React 前端（图书馆 / 工作台 / 设置）
├── sidecar/            # Python OCR 服务
│   ├── main.py
│   ├── requirements.txt
│   └── ocr_app/
├── scripts/            # 如 build_sidecar.py（打包 sidecar）
├── resources/          # 打包用 ocr-sidecar（gitignore，需本地构建）
├── release/            # electron-builder 输出（gitignore）
└── DEPLOY.md           # 安装包换机说明
```

## 开发环境

### 依赖

- Node.js 18+（含 npm）
- Python 3.10+（推荐 conda 环境，例如名为 `OCR`）
- Windows 下开发时，Electron 会优先使用 `OCR` conda 环境的 Python 启动 sidecar

### 安装

```bash
npm install
# 或使用 conda 中的 pip
pip install -r sidecar/requirements.txt
```

可选：设置 Python 路径

```bash
set OCR_PYTHON=C:\Users\<you>\anaconda3\envs\OCR\python.exe
```

### 配置

1. 启动应用后打开 **设置**
2. 填写 **DashScope API Key** 与 **Base URL**（可用北京 MaaS 等兼容端点）
3. 设置 **数据目录**（Library 根路径，例如 `D:\Library`）并保存

密钥写入 Library 内 `secrets.json`，其余配置在 `settings.json`。

### 启动

```bash
# 终端 1：仅 Vite（浏览器调试 UI，需自行起 sidecar）
npm run dev

# 推荐：Electron + Vite；会自动拉起 sidecar
npm run electron:dev

# 仅 sidecar
npm run sidecar:dev
# 或
python sidecar/main.py --port 18765
```

## 打包

### 1. 构建 sidecar 可执行文件（若尚无）

使用项目内脚本将 Python 服务打成 `resources/ocr-sidecar/`（体积较大，含依赖）。安装包通过 `extraResources` 打入该目录。

### 2. 构建 Windows 安装包

```bash
npm run electron:build
```

产物：

```
release/OCR-Library-Setup-0.1.0.exe
```

详细换机步骤见 [DEPLOY.md](./DEPLOY.md)。

## Library 数据目录

应用与数据分离。完整 Library 通常包含：

| 路径 | 说明 |
|------|------|
| `library.db` | 文档与任务索引 |
| `docs/<id>/` | `source.pdf`、layout、markdown、缩略图等 |
| `settings.json` | 模型、DPI、并发、Base URL |
| `secrets.json` | API Key（勿提交到 Git） |
| `folders.json` | 文件夹分类（如有） |

### 换机

1. 安装 `OCR-Library-Setup-*.exe`
2. 拷贝整个 Library 文件夹到新电脑
3. 设置 → 选择该目录 → 保存
4. 列表不全时：设置中 **从 docs/ 重建索引**

首次保存后，`%LOCALAPPDATA%\OcrLibrary\settings.json` 会写入指向该 Library 的指针。

## 常用设置项

| 项 | 含义 |
|----|------|
| Vision 模型 | 如 `qwen3.5-flash` |
| DPI | PDF 渲染分辨率（默认 300，无损上传） |
| 页级 / 文档 / API 并发 | 控制吞吐与稳定性 |
| OCR Worker 进程数 | `1` = 进程内；`2+` = 多进程页任务池 |

## 脚本

| 命令 | 说明 |
|------|------|
| `npm run dev` | Vite 开发服务器 |
| `npm run electron:dev` | Electron 开发模式 |
| `npm run build` | 前端 + Electron 编译 |
| `npm run electron:build` | Windows NSIS 安装包 |
| `npm run sidecar:dev` | 启动 Python sidecar |
| `npm run sidecar:install` | 安装 sidecar 依赖 |

## 许可证与隐私

- 项目默认 `private`；按需自行补充许可证
- 勿将 `secrets.json`、`.env` 或含 API Key 的配置提交到版本库（见 `.gitignore`）
