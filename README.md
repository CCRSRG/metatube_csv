# MetaTube CSV Server

将本地 CSV 数据作为 Jellyfin [MetaTube 插件](https://github.com/metatube-community/jellyfin-plugin-metatube) 的元数据来源。

## 工作原理

```
Jellyfin → MetaTube 插件 → CSV Server → 本地 SQLite 数据库（CSV 导入）
                                      ↘ 查不到时回退 → 真实 MetaTube Server
```

- CSV 首次启动时导入 SQLite 数据库，之后重启**秒开**
- CSV 文件更新后**自动检测**并重新导入
- 支持 6 级模糊搜索（精确 → 去后缀 → 模糊 → 标题 → 去横杠 → 组合）
- 本地查不到时可自动回退到真实 MetaTube Server（影片、演员、图片均支持）
- 支持 Bearer Token 认证（可选）
- 支持图片角标叠加（如中文字幕标记）
- 固定使用 `utf-8-sig` 读取 CSV，兼容 UTF-8 BOM / 无 BOM

## 快速开始

### Docker 部署（推荐）

```bash
# 1. 准备目录
mkdir -p data
cp 你的数据.csv data/BB_Magnet.csv

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 设置 CSV 路径、数据库路径和回退服务器地址

# 3. 构建并启动
docker compose up -d --build

# 4. 清理无效镜像
docker image prune -f

# 5. 查看日志
docker logs -f metatube-csv-server
```

### 本地运行

```bash
pip install -r requirements.txt
python metatube_csv_server.py --csv BB_Magnet.csv --port 8000
```

## 配置 Jellyfin

1. 安装 [MetaTube 插件](https://github.com/metatube-community/jellyfin-plugin-metatube)
2. 插件设置中 **Server** 填写：`http://你的服务器IP:8000`
3. 如果启用了 Token 认证，在 **Token** 中填入相同的密钥；否则留空
4. 保存并刮削媒体库

## 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--csv` | CSV 数据文件路径（必需） | — |
| `--db` | SQLite 数据库路径 | 与 CSV 同目录同名 `.db` |
| `--host` | 监听地址 | `0.0.0.0` |
| `--port` | 监听端口 | `8000` |
| `--fallback` | 真实 MetaTube Server 地址 | 空（不回退） |
| `--token` | Bearer Token 认证密钥 | 空（不启用认证） |
| `--reimport` | 强制重新导入 CSV | — |

> **自动重新导入**：当 CSV 文件的修改时间比数据库更新时，会自动重新导入，无需手动指定 `--reimport`。

> **Token 认证范围**：首页 `/` 和图片接口 `/v1/images/*` 免认证（原始 MetaTube 插件的图片请求不发送 Token），其他 API 接口支持请求头 `Authorization: Bearer <token>` 和查询参数 `?token=<token>` 两种方式。

## 环境变量（Docker 部署）

Docker 部署时通过 `.env` 文件配置，参考 `.env.example`：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CSV_PATH` | CSV 数据文件路径 | `/data/BB_Magnet.csv` |
| `DB_PATH` | SQLite 数据库路径（可选） | 与 CSV 同名 `.db` |
| `FALLBACK_URL` | 真实 MetaTube Server 地址 | 空（不回退） |
| `TOKEN` | Bearer Token 认证密钥 | 空（不启用认证） |

> **时区配置**：Docker 容器默认为 UTC 时区，已在 `docker-compose.yml` 中设置 `TZ=Asia/Shanghai`，如需修改请编辑该文件。

## CSV 格式

参考 `csv_template.csv`。

编码要求：

- CSV 请使用 `utf-8-sig`

字段说明：

| 列名 | 说明 | 示例 |
|------|------|------|
| 番号 | 唯一标识（必需） | `IENF-xxx` |
| 原始链接 | 来源页面 | `https://xxx.com/v/xxx` |
| 翻译标题 | 返回给 Jellyfin 的优先标题；为空时回退到 `当前标题` | `中文标题` |
| 当前标题 | 原始站点标题；当 `翻译标题` 为空时使用 | `最高級美女 xxx...` |
| 原标题 | 原始标题；当前主要作为保底字段 | `Original Title` |
| 发布日期 | 上映日期 | `2024-01-15` |
| 时长 | 影片时长 | `127 分鍾` |
| 简介 | 详情简介，对应 API 的 `summary` | `影片简介...` |
| 导演 | 导演名 | `xxx` |
| 片商 | 制作商 | `アイエナジー` |
| 发行商 | 发行商，对应 API 的 `label` | `S1` |
| 系列 | 系列名称 | `高級ソープ` |
| 类别 | 分类标签，逗号分隔 | `xx, xxx` |
| 演员 | 演员名，逗号分隔 | `浅風ゆい♀` |
| 评分 | 评分 | `3.77` |
| 封面图 | 封面图 URL | `https://...covers/xxx.jpg` |
| 预告片 | 预告片 URL | `https://...preview.mp4` |
| 预览图 | 预览图基础 URL | `https://...sample_l_20.jpg` |
| 预览图数量 | 预览图总数；服务端会生成从 `_0` 开始的预览图列表 | `21` |

说明：

- `title` 返回顺序为：`翻译标题 -> 当前标题 -> 原标题`
- `label` 对应 CSV 的 `发行商`
- `summary` 对应 CSV 的 `简介`
- 若 CSV 中存在重复表头，服务端会保留并按字段优先级取第一个非空值

## API 接口

| 接口 | 路径 | 说明 |
|------|------|------|
| 首页 | `GET /` | 服务状态 |
| 搜索影片 | `GET /v1/movies/search?q=关键词` | 多级模糊搜索 |
| 影片详情 | `GET /v1/movies/{provider}/{id}` | 完整元数据 |
| 搜索演员 | `GET /v1/actors/search?q=演员名` | 从影片中提取 |
| 演员详情 | `GET /v1/actors/{provider}/{id}` | 基本信息（支持回退） |
| 图片代理 | `GET /v1/images/{type}/{provider}/{id}` | 代理转发图片（支持回退） |
| 翻译 | `GET /v1/translate?q=文本` | 返回原文 |

## 常用操作

```bash
# 构建清理
docker compose up -d --build && docker image prune -f

# 查看服务状态
curl http://127.0.0.1:8000/

# 搜索测试
curl "http://127.0.0.1:8000/v1/movies/search?q=IENF-431"

# 详情测试
curl "http://127.0.0.1:8000/v1/movies/csv/IENF-431"

# 搜索测试（启用 Token 认证时）
# 方式一：请求头（推荐）
curl -H "Authorization: Bearer my-secret-token" \
  "http://127.0.0.1:8000/v1/movies/search?q=IENF-431"
# 方式二：查询参数（方便浏览器直接访问）
# http://127.0.0.1:8000/v1/movies/search?q=IENF-431&token=my-secret-token

# 详情测试（带 token）
curl "http://127.0.0.1:8000/v1/movies/csv/IENF-431?token=my-secret-token"

# 更新 CSV 后重新导入（自动检测 CSV 更新，重启即可）
docker compose restart

# 强制重新导入
docker compose exec metatube-csv python metatube_csv_server.py \
  --csv /data/BB_Magnet.csv --reimport
docker compose restart

# 查看数据库内容
docker exec metatube-csv-server python -c "
import sqlite3
conn = sqlite3.connect('/data/BB_Magnet.db')
for row in conn.execute('SELECT id, title, maker FROM movies LIMIT 5'):
    print(f'{row[0]} | {row[1]} | {row[2]}')
conn.close()
"
```

## 项目文件

```
metatube_csv/
├── metatube_csv_server.py   # 主程序
├── requirements.txt         # Python 依赖
├── Dockerfile               # Docker 镜像构建
├── docker-compose.yml       # Docker Compose 配置
├── .env.example             # 环境变量模板（复制为 .env 使用）
├── .gitignore               # Git 忽略文件
├── .dockerignore            # Docker 构建排除文件
├── csv_template.csv         # CSV 格式模板
├── badges/                  # 角标图片目录
│   └── zimu.png             # 中文字幕角标（首次使用时自动从 GitHub 下载）
├── README.md                # 本文档
└── data/                    # 数据目录（运行时创建）
    ├── BB_Magnet.csv        # 你的 CSV 数据
    └── BB_Magnet.db          # SQLite 数据库（自动生成）
```

## 依赖

- Python 3.9+
- FastAPI
- Uvicorn
- httpx
- Pillow（图片裁剪和角标叠加）
