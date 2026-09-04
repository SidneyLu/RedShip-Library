# OCR Library 安装与换机

## 本机构建产物

安装包路径：

`release/OCR-Library-Setup-0.1.0.exe`

## 新电脑部署

1. 安装 `OCR-Library-Setup-*.exe`
2. 将原电脑的 **整个 Library 文件夹**（含 `library.db`、`docs/`、`settings.json`、`secrets.json`）拷到新电脑，路径可相同或不同（例如 `D:\Library`）
3. 打开应用 → **设置** → **数据目录** → 选择该 Library 文件夹 → **保存**
4. 若文档列表不全：设置里点 **从 docs/ 重建索引**
5. 若 OCR 不可用：在设置中确认 DashScope API Key / Base URL（密钥随 `secrets.json` 一并拷贝时通常已可用）

## Library 目录应包含

| 文件/目录 | 作用 |
|-----------|------|
| `library.db` | 文档索引与任务状态 |
| `docs/` | 每本 PDF、layout、markdown、缩略图 |
| `settings.json` | 模型、并发、端点等 |
| `secrets.json` | API Key（勿泄露） |
| `folders.json` | 文件夹分类（如有） |

## 注意

- 不要只拷 `docs/` 而不拷 `library.db`（除非打算重建索引）
- 应用本体与 Library 分离；换机只需安装包 + Library 文件夹
- 首次保存数据目录后，本机 `%LOCALAPPDATA%\OcrLibrary\settings.json` 会写入指向该 Library 的指针
