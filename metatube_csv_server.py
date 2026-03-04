"""
MetaTube CSV Server — 伪造的 MetaTube 后端服务

读取本地 CSV 数据文件，导入 SQLite 数据库，模拟 MetaTube Server 的 API 接口，
使 Jellyfin 的 MetaTube 插件能够从 CSV 中搜索和获取元数据。

使用方法：
    pip install fastapi uvicorn httpx Pillow
    python metatube_csv_server.py --csv data.csv --port 8000
"""

import argparse
import csv
import io
import json
import re
import os
import sqlite3
import sys
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image

import httpx
from fastapi import FastAPI, Query, Response
from fastapi.responses import JSONResponse
import uvicorn

# ============================================================
# 版本号
# ============================================================
VERSION = "2.0.0"

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("metatube-csv")


# ============================================================
# 配置类（替代全局变量）
# ============================================================
@dataclass
class AppConfig:
    """应用配置，集中管理所有可配置项"""
    provider: str = "csv"
    db_path: str = ""
    fallback_server: str = ""
    token: str = ""  # Bearer Token，空则不验证


config = AppConfig()

# CSV 列名 → 内部字段名的映射
COLUMN_MAP = {
    "原始链接": "homepage",
    "当前标题": "title",
    "原标题": "original_title",
    "番号": "number",
    "发布日期": "release_date",
    "时长": "runtime",
    "导演": "director",
    "片商": "maker",
    "系列": "series",
    "类别": "genres",
    "演员": "actors",
    "评分": "score",
    "封面图": "cover_url",
    "预告片": "preview_video_url",
    "预览图": "preview_images_base",
    "预览图数量": "preview_images_count",
}


# ============================================================
# 工具函数
# ============================================================
def strip_number_suffix(number: str) -> str:
    """
    提取番号核心部分（字母+数字），去除所有后缀，如:
    EYAN-197-U      → EYAN-197
    JUL-968-C_X1080X → JUL-968
    ABP-123-UC      → ABP-123
    SSIS-001-FHD    → SSIS-001
    FC2-PPV-1234567 → FC2-PPV-1234567（多段格式保留）
    """
    number = number.strip().upper()
    # 匹配核心番号格式：字母(+字母/横杠)+数字
    # 支持 FC2-PPV-1234567 这种多段格式
    match = re.match(r'^([A-Z][\w]*(?:-[A-Z]+)*-\d+)', number)
    if match:
        return match.group(1)
    # 回退：只匹配 字母-数字
    match = re.match(r'^([A-Z]+-\d+)', number)
    if match:
        return match.group(1)
    return number


def escape_like(value: str) -> str:
    """转义 LIKE 查询中的特殊字符（%, _, \）"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse_runtime(raw: str) -> int:
    """解析时长字符串，提取分钟数。如 '127 分鍾' → 127"""
    if not raw:
        return 0
    match = re.search(r"(\d+)", raw)
    return int(match.group(1)) if match else 0


def parse_date(raw: str) -> Optional[str]:
    """解析日期字符串，返回 ISO 格式"""
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw.strip(), "%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        return None


def parse_list(raw: str) -> list[str]:
    """解析逗号分隔的列表"""
    if not raw:
        return []
    items = re.split(r"[,，]", raw)
    return [item.strip() for item in items if item.strip()]


def clean_actor_name(name: str) -> str:
    """清理演员名字，去除 ♀♂ 等性别标记"""
    return re.sub(r"[♀♂]", "", name).strip()


def generate_preview_images(base_url: str, count_str: str) -> list[str]:
    """根据预览图基础 URL 和数量生成所有预览图 URL"""
    if not base_url or not count_str:
        return []
    try:
        count = int(count_str)
    except ValueError:
        return []
    match = re.search(r"(_l_)(\d+)(\.jpg)", base_url)
    if match:
        prefix = base_url[: match.start(2)]
        suffix = base_url[match.end(2) :]
        return [f"{prefix}{i}{suffix}" for i in range(1, count + 1)]
    return [base_url]


def detect_encoding(filepath: Path) -> str:
    """读取文件头部检测编码"""
    raw = filepath.read_bytes()[:8192]
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return "utf-16"
    if raw[:3] == b"\xef\xbb\xbf":
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("gbk")
        return "gbk"
    except UnicodeDecodeError:
        pass
    return "utf-8"


def crop_image(image_data: bytes, ratio: float, pos: float = -1, quality: int = 90) -> tuple[bytes, str]:
    """
    根据目标宽高比裁剪图片。
    ratio: 目标宽高比（宽/高），如 0.67 表示 2:3 竖版海报
    pos: 裁剪位置（0.0~1.0），-1 表示自动（默认右半部分）
    quality: JPEG 质量（1-100）
    返回 (裁剪后的图片字节, content_type)
    """
    img = Image.open(io.BytesIO(image_data))
    w, h = img.size
    current_ratio = w / h

    # 如果目标比例和当前比例接近，无需裁剪
    if abs(current_ratio - ratio) < 0.05:
        return image_data, "image/jpeg"

    if ratio < current_ratio:
        # 需要裁剪宽度（横图→竖图）
        new_w = int(h * ratio)
        if pos < 0:
            # 默认取右半部分（封面主体通常在右侧）
            pos = 1.0
        # pos=0 取最左，pos=1 取最右
        max_offset = w - new_w
        x_offset = int(max_offset * max(0.0, min(1.0, pos)))
        crop_box = (x_offset, 0, x_offset + new_w, h)
    else:
        # 需要裁剪高度
        new_h = int(w / ratio)
        if pos < 0:
            pos = 0.5
        max_offset = h - new_h
        y_offset = int(max_offset * max(0.0, min(1.0, pos)))
        crop_box = (0, y_offset, w, y_offset + new_h)

    img = img.crop(crop_box)

    # 输出为 JPEG
    output = io.BytesIO()
    # 转换为 RGB（处理 RGBA/P 等模式）
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue(), "image/jpeg"


# 角标图片缓存（避免重复下载）
_badge_cache: dict[str, bytes] = {}

# 脚本所在目录
_script_dir = Path(__file__).parent


async def _load_badge_image(badge_ref: str) -> Optional[bytes]:
    """
    加载角标图片数据。
    支持三种来源（按优先级）：
      1. 本地文件路径（绝对路径）
      2. 短文件名 → 脚本同目录 badges/ 子目录
      3. 完整 HTTP URL → 远程下载
    加载成功后缓存到内存，避免重复 IO/网络请求。
    """
    if badge_ref in _badge_cache:
        return _badge_cache[badge_ref]

    badge_data: Optional[bytes] = None

    if badge_ref.startswith(("http://", "https://")):
        # 远程 URL
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(badge_ref)
                if resp.status_code == 200:
                    badge_data = resp.content
                else:
                    logger.warning("角标下载失败 [%d]: %s", resp.status_code, badge_ref)
        except Exception as e:
            logger.warning("角标下载异常: %s", str(e))
    else:
        # 本地文件：先检查绝对路径，再检查 badges/ 子目录
        candidates = [
            Path(badge_ref),
            _script_dir / "badges" / badge_ref,
        ]
        for candidate in candidates:
            if candidate.is_file():
                badge_data = candidate.read_bytes()
                logger.info("从本地加载角标: %s", candidate)
                break

        # 本地找不到 → 尝试从 MetaTube 原始项目 GitHub 下载
        if not badge_data:
            github_url = f"https://raw.githubusercontent.com/metatube-community/metatube-sdk-go/main/imageutil/badge/{badge_ref}"
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    resp = await client.get(github_url)
                    if resp.status_code == 200:
                        badge_data = resp.content
                        # 保存到本地 badges/ 目录以便下次直接使用
                        local_path = _script_dir / "badges" / badge_ref
                        local_path.parent.mkdir(parents=True, exist_ok=True)
                        local_path.write_bytes(badge_data)
                        logger.info("从 GitHub 下载角标并缓存到本地: %s", local_path)
            except Exception as e:
                logger.warning("从 GitHub 下载角标失败: %s", str(e))

    if badge_data:
        _badge_cache[badge_ref] = badge_data
    return badge_data


async def overlay_badge(image_data: bytes, badge_ref: str, quality: int = 90) -> tuple[bytes, str]:
    """
    将角标图片叠加到封面图左上角。
    badge_ref: 角标来源（完整 URL / 本地路径 / 短文件名如 'zimu.png'）
    quality: JPEG 输出质量
    返回 (合成后的图片字节, content_type)
    """
    badge_data = await _load_badge_image(badge_ref)
    if not badge_data:
        logger.warning("无法加载角标 '%s'，返回原图", badge_ref)
        return image_data, "image/jpeg"

    # 打开封面图和角标图
    cover = Image.open(io.BytesIO(image_data))
    badge = Image.open(io.BytesIO(badge_data)).convert("RGBA")

    # 按封面高度的 20% 缩放角标（与原始 Go 后端保持一致）
    badge_target_h = int(cover.height * 0.2)
    badge_scale = badge_target_h / badge.height
    badge_target_w = int(badge.width * badge_scale)
    badge = badge.resize((badge_target_w, badge_target_h), Image.LANCZOS)

    # 确保封面图为 RGBA 以支持透明叠加
    if cover.mode != "RGBA":
        cover = cover.convert("RGBA")

    # 叠加到左上角
    cover.paste(badge, (0, 0), badge)

    # 输出为 JPEG（去掉 Alpha 通道）
    output = io.BytesIO()
    cover.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
    return output.getvalue(), "image/jpeg"


# ============================================================
# SQLite 数据库操作
# ============================================================
@contextmanager
def get_db():
    """获取 SQLite 连接（上下文管理器，确保异常时也能关闭）"""
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    """初始化数据库表和索引"""
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS movies (
                id TEXT PRIMARY KEY,
                number TEXT NOT NULL,
                title TEXT DEFAULT '',
                original_title TEXT DEFAULT '',
                homepage TEXT DEFAULT '',
                release_date TEXT,
                runtime INTEGER DEFAULT 0,
                director TEXT DEFAULT '',
                maker TEXT DEFAULT '',
                series TEXT DEFAULT '',
                genres TEXT DEFAULT '[]',
                actors TEXT DEFAULT '[]',
                score REAL DEFAULT 0.0,
                cover_url TEXT DEFAULT '',
                preview_video_url TEXT DEFAULT '',
                preview_images TEXT DEFAULT '[]',
                label TEXT DEFAULT '',
                summary TEXT DEFAULT ''
            )
        """)
        # 数据库迁移：为旧表补充缺失的列
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(movies)").fetchall()}
        if "original_title" not in existing_cols:
            conn.execute("ALTER TABLE movies ADD COLUMN original_title TEXT DEFAULT ''")
            logger.info("数据库迁移：添加 original_title 列")
        # 创建搜索索引
        conn.execute("CREATE INDEX IF NOT EXISTS idx_number ON movies(number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON movies(title)")
        conn.commit()


def import_csv_to_db(csv_path: str) -> int:
    """将 CSV 数据逐行导入 SQLite 数据库"""
    path = Path(csv_path)
    if not path.exists():
        logger.error("CSV 文件不存在: %s", csv_path)
        sys.exit(1)

    encoding = detect_encoding(path)
    logger.info("检测到编码: %s", encoding)

    count = 0

    with get_db() as conn, open(path, "r", encoding=encoding, errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # 映射字段
            movie = {}
            for csv_col, field_name in COLUMN_MAP.items():
                val = row.get(csv_col, "")
                movie[field_name] = val.strip().replace("\x00", "") if val else ""

            number = movie.get("number", "").strip()
            if not number:
                continue

            number_upper = number.upper()
            release_date = parse_date(movie.get("release_date", ""))
            runtime = parse_runtime(movie.get("runtime", ""))
            genres = parse_list(movie.get("genres", ""))
            actors = [clean_actor_name(a) for a in parse_list(movie.get("actors", ""))]

            score = 0.0
            try:
                raw_score = movie.get("score", "")
                if raw_score:
                    score = float(raw_score)
            except (ValueError, TypeError):
                score = 0.0

            preview_images = generate_preview_images(
                movie.get("preview_images_base", ""),
                movie.get("preview_images_count", ""),
            )

            conn.execute(
                """INSERT OR REPLACE INTO movies
                   (id, number, title, original_title, homepage, release_date, runtime,
                    director, maker, series, genres, actors, score,
                    cover_url, preview_video_url, preview_images, label, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    number_upper,
                    number_upper,
                    movie.get("title", ""),
                    movie.get("original_title", ""),
                    movie.get("homepage", ""),
                    release_date,
                    runtime,
                    movie.get("director", ""),
                    movie.get("maker", ""),
                    movie.get("series", ""),
                    json.dumps(genres, ensure_ascii=False),
                    json.dumps(actors, ensure_ascii=False),
                    score,
                    movie.get("cover_url", ""),
                    movie.get("preview_video_url", ""),
                    json.dumps(preview_images, ensure_ascii=False),
                    "",
                    "",
                ),
            )
            count += 1
            # 每 10000 条提交一次，平衡速度和内存
            if count % 10000 == 0:
                conn.commit()
                logger.info("已导入 %d 条记录...", count)

        conn.commit()

    logger.info("CSV 导入完成，共 %d 条记录", count)
    return count


def row_to_search_result(row: sqlite3.Row) -> dict:
    """将数据库行转换为 MovieSearchResult 格式"""
    cover = row["cover_url"] or ""
    thumb = cover.replace("/covers/", "/thumbs/") if cover else ""
    return {
        "id": row["id"],
        "provider": config.provider,
        "homepage": row["homepage"] or "",
        "actors": json.loads(row["actors"]) if row["actors"] else [],
        "cover_url": cover,
        "number": row["number"],
        "release_date": row["release_date"] or "0001-01-01T00:00:00Z",
        "score": row["score"] or 0.0,
        "thumb_url": thumb,
        "title": row["title"] or "",
    }


def row_to_info(row: sqlite3.Row) -> dict:
    """将数据库行转换为完整的 MovieInfo 格式"""
    result = row_to_search_result(row)
    cover = row["cover_url"] or ""
    thumb = cover.replace("/covers/", "/thumbs/") if cover else ""
    result.update({
        "original_title": row["original_title"] or "",
        "big_cover_url": cover,
        "big_thumb_url": thumb,
        "director": row["director"] or "",
        "genres": json.loads(row["genres"]) if row["genres"] else [],
        "maker": row["maker"] or "",
        "preview_images": json.loads(row["preview_images"]) if row["preview_images"] else [],
        "preview_video_hls_url": "",
        "preview_video_url": row["preview_video_url"] or "",
        "label": row["label"] or "",
        "runtime": row["runtime"] or 0,
        "series": row["series"] or "",
        "summary": row["summary"] or "",
    })
    return result


# ============================================================
# FastAPI 应用
# ============================================================
app = FastAPI(title="MetaTube CSV Server", version=VERSION)


@app.middleware("http")
async def auth_middleware(request, call_next):
    """
    Bearer Token 认证中间件。
    仅当 config.token 非空时启用认证，
    首页 / 免认证（用于健康检查）。
    支持两种传递方式：
      1. 请求头: Authorization: Bearer <token>
      2. 查询参数: ?token=<token>（方便浏览器调试）
    """
    if config.token:
        # 首页免认证
        if request.url.path != "/":
            provided_token = ""
            # 优先从请求头获取
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer "):
                provided_token = auth_header[7:]
            else:
                # 回退到查询参数
                provided_token = request.query_params.get("token", "")

            if not provided_token:
                return JSONResponse(
                    status_code=401,
                    content={"data": None, "error": {"code": 401, "message": "Missing authorization token"}},
                )
            if provided_token != config.token:
                return JSONResponse(
                    status_code=401,
                    content={"data": None, "error": {"code": 401, "message": "Invalid authorization token"}},
                )
    return await call_next(request)


def success_response(data):
    """构造成功响应"""
    return JSONResponse(content={"data": data, "error": None})


def error_response(code: int, message: str, status_code: int = 404):
    """构造错误响应"""
    return JSONResponse(
        content={"data": None, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


async def proxy_to_fallback(path: str, params: dict) -> Optional[dict]:
    """
    将请求转发到真实的 MetaTube Server。
    返回原始 JSON 响应，或 None（如果回退服务不可用）。
    """
    if not config.fallback_server:
        return None
    try:
        url = f"{config.fallback_server.rstrip('/')}{path}"
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                logger.info("回退到真实服务端成功: %s", path)
                return data
    except Exception as e:
        logger.warning("回退请求失败: %s (%s: %s)", path, type(e).__name__, str(e))
    return None


# ============================================================
# API 路由
# ============================================================
@app.get("/v1/movies/search")
async def search_movies(
    q: str = Query("", description="搜索关键词"),
    provider: str = Query("", description="数据源"),
    fallback: str = Query("True", description="是否回退搜索"),
):
    """搜索影片 — 在 SQLite 中按番号和标题搜索"""
    query = q.strip()
    if not query:
        return success_response([])

    query_upper = query.upper()
    # 去除横杠空格的版本，用于模糊匹配
    query_clean = re.sub(r"[-_\s]", "", query_upper)

    results = []

    with get_db() as conn:
        # 1. 精确匹配番号
        row = conn.execute("SELECT * FROM movies WHERE id = ?", (query_upper,)).fetchone()
        if row:
            results.append(row_to_search_result(row))

        # 2. 去后缀匹配（如 EYAN-197-U → EYAN-197）
        if not results:
            stripped = strip_number_suffix(query_upper)
            if stripped != query_upper:
                row = conn.execute("SELECT * FROM movies WHERE id = ?", (stripped,)).fetchone()
                if row:
                    results.append(row_to_search_result(row))
                    logger.info("后缀剥离匹配: %s → %s", query_upper, stripped)

        # 3. 模糊匹配番号（LIKE，转义特殊字符）
        if not results:
            escaped = escape_like(query_upper)
            rows = conn.execute(
                "SELECT * FROM movies WHERE id LIKE ? ESCAPE '\\' OR number LIKE ? ESCAPE '\\' LIMIT 20",
                (f"%{escaped}%", f"%{escaped}%"),
            ).fetchall()
            results.extend(row_to_search_result(r) for r in rows)

        # 4. 标题匹配（LIKE，转义特殊字符）
        if not results:
            escaped_title = escape_like(query)
            rows = conn.execute(
                "SELECT * FROM movies WHERE title LIKE ? ESCAPE '\\' LIMIT 20",
                (f"%{escaped_title}%",),
            ).fetchall()
            results.extend(row_to_search_result(r) for r in rows)

        # 5. 去横杠匹配（如 IENF431 匹配 IENF-431）
        if not results and query_clean:
            rows = conn.execute(
                "SELECT * FROM movies WHERE REPLACE(REPLACE(id, '-', ''), '_', '') = ?",
                (query_clean,),
            ).fetchall()
            results.extend(row_to_search_result(r) for r in rows)

        # 6. 去后缀 + 去横杠匹配
        if not results:
            stripped_clean = re.sub(r"[-_\s]", "", strip_number_suffix(query_upper))
            if stripped_clean != query_clean:
                rows = conn.execute(
                    "SELECT * FROM movies WHERE REPLACE(REPLACE(id, '-', ''), '_', '') = ?",
                    (stripped_clean,),
                ).fetchall()
                results.extend(row_to_search_result(r) for r in rows)

    # 7. 本地查不到 → 回退到真实 MetaTube Server
    if not results and config.fallback_server:
        logger.info("本地未找到 '%s'，回退到真实服务端...", query)
        fallback_data = await proxy_to_fallback(
            "/v1/movies/search",
            {"q": q, "provider": provider, "fallback": fallback},
        )
        if fallback_data and fallback_data.get("data"):
            return JSONResponse(content=fallback_data)

    logger.info("搜索 '%s' → 找到 %d 条结果", query, len(results))
    return success_response(results)


@app.get("/v1/movies/{provider}/{movie_id:path}")
async def get_movie_info(
    provider: str,
    movie_id: str,
    lazy: str = Query("True", description="是否懒加载"),
):
    """获取影片详情"""
    movie_id_upper = movie_id.strip().upper()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id_upper,)).fetchone()
        if row:
            logger.info("获取详情: %s", movie_id_upper)
            return success_response(row_to_info(row))

    # 本地查不到 → 回退到真实 MetaTube Server
    if config.fallback_server:
        logger.info("本地未找到 '%s'，回退到真实服务端...", movie_id)
        # 先搜索获取真实的 provider 和 id
        search_data = await proxy_to_fallback(
            "/v1/movies/search",
            {"q": movie_id, "provider": "", "fallback": "true"},
        )
        if search_data and search_data.get("data"):
            results = search_data["data"]
            if results:
                real_provider = results[0].get("provider", "")
                real_id = results[0].get("id", movie_id)
                logger.info("真实服务端匹配: provider=%s, id=%s", real_provider, real_id)
                # 用真实 provider 获取详情
                fallback_data = await proxy_to_fallback(
                    f"/v1/movies/{real_provider}/{real_id}",
                    {"lazy": lazy},
                )
                if fallback_data and fallback_data.get("data"):
                    return JSONResponse(content=fallback_data)

    logger.warning("影片未找到: %s/%s", provider, movie_id)
    return error_response(404, f"Movie not found: {movie_id}")


@app.get("/v1/actors/search")
async def search_actors(
    q: str = Query("", description="搜索关键词"),
    provider: str = Query("", description="数据源"),
    fallback: str = Query("True", description="是否回退搜索"),
):
    """搜索演员 — 从影片数据中提取匹配的演员"""
    query = q.strip()
    if not query:
        return success_response([])

    with get_db() as conn:
        # 搜索 actors 字段中包含关键词的影片（转义 LIKE 特殊字符）
        escaped = escape_like(query)
        rows = conn.execute(
            "SELECT actors, cover_url FROM movies WHERE actors LIKE ? ESCAPE '\\' LIMIT 50",
            (f"%{escaped}%",),
        ).fetchall()

    results = []
    seen = set()
    for row in rows:
        actors = json.loads(row["actors"]) if row["actors"] else []
        for actor_name in actors:
            if query.lower() in actor_name.lower() or actor_name.lower() in query.lower():
                if actor_name not in seen:
                    seen.add(actor_name)
                    images = [row["cover_url"]] if row["cover_url"] else []
                    results.append({
                        "id": actor_name,
                        "provider": config.provider,
                        "homepage": "",
                        "images": images,
                        "name": actor_name,
                    })

    # 本地查不到 → 回退到真实 MetaTube Server
    if not results and config.fallback_server:
        logger.info("本地未找到演员 '%s'，回退到真实服务端...", query)
        fallback_data = await proxy_to_fallback(
            "/v1/actors/search",
            {"q": q, "provider": provider, "fallback": fallback},
        )
        if fallback_data and fallback_data.get("data"):
            return JSONResponse(content=fallback_data)

    logger.info("搜索演员 '%s' → 找到 %d 条结果", query, len(results))
    return success_response(results)


@app.get("/v1/actors/{provider}/{actor_id:path}")
async def get_actor_info(provider: str, actor_id: str):
    """获取演员详情"""
    # 优先回退到真实服务端获取完整数据
    if config.fallback_server:
        fallback_data = await proxy_to_fallback(
            f"/v1/actors/{provider}/{actor_id}", {},
        )
        if fallback_data and fallback_data.get("data"):
            logger.info("从真实服务端获取演员详情: %s", actor_id)
            return JSONResponse(content=fallback_data)

    # 返回基本信息
    return success_response({
        "id": actor_id,
        "provider": provider,
        "homepage": "",
        "images": [],
        "name": actor_id,
        "aliases": [],
        "birthday": "0001-01-01T00:00:00Z",
        "blood_type": "",
        "cup_size": "",
        "debut_date": "0001-01-01T00:00:00Z",
        "height": 0,
        "hobby": "",
        "skill": "",
        "measurements": "",
        "nationality": "",
        "summary": "",
    })


@app.get("/v1/images/{image_type}/{provider}/{image_id:path}")
async def get_image(
    image_type: str,
    provider: str,
    image_id: str,
    url: str = Query("", description="图片 URL"),
    ratio: float = Query(-1, description="宽高比"),
    pos: float = Query(-1, description="裁剪位置"),
    auto: str = Query("False", description="自动选择"),
    badge: str = Query("", description="角标"),
    quality: int = Query(90, description="图片质量"),
):
    """图片代理接口"""
    target_url = url
    if not target_url:
        movie_id = image_id.strip().upper()
        with get_db() as conn:
            row = conn.execute("SELECT cover_url FROM movies WHERE id = ?", (movie_id,)).fetchone()
        if row:
            target_url = row["cover_url"] or ""

    # 本地找不到图片 URL → 回退到真实 MetaTube Server
    if not target_url and config.fallback_server:
        logger.info("本地未找到图片，回退到真实服务端: %s/%s/%s", image_type, provider, image_id)
        fallback_params = {
            "url": url, "ratio": ratio, "pos": pos,
            "auto": auto, "badge": badge, "quality": quality,
        }
        try:
            fallback_url = f"{config.fallback_server}/v1/images/{image_type}/{provider}/{image_id}"
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(fallback_url, params=fallback_params)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "image/jpeg")
                    return Response(
                        content=resp.content,
                        media_type=content_type,
                        headers={"Cache-Control": "max-age=86400"},
                    )
        except Exception as e:
            logger.warning("图片回退失败: %s", str(e))

    if not target_url:
        return error_response(404, "Image not found")

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://javdb.com/",
            }
            resp = await client.get(target_url, headers=headers)
            if resp.status_code != 200:
                logger.warning("图片下载失败 [%d]: %s", resp.status_code, target_url)
                return error_response(resp.status_code, "Image download failed")

            image_data = resp.content
            content_type = resp.headers.get("content-type", "image/jpeg")

            # primary 类型未指定 ratio 时，默认使用 2:3 竖版海报比例
            if ratio <= 0 and image_type == "primary":
                ratio = 0.67

            # 根据 ratio 和 pos 参数裁剪图片（用于 Primary/poster 竖版海报）
            if ratio > 0:
                try:
                    image_data, content_type = crop_image(image_data, ratio, pos, quality)
                except Exception as e:
                    logger.warning("图片裁剪失败，返回原图: %s", str(e))

            # 叠加角标（如中文字幕标记）
            if badge:
                try:
                    image_data, content_type = await overlay_badge(image_data, badge, quality)
                    logger.info("已叠加角标: %s", badge)
                except Exception as e:
                    logger.warning("角标叠加失败，返回原图: %s", str(e))

            return Response(
                content=image_data,
                media_type=content_type,
                headers={"Cache-Control": "max-age=86400"},
            )
    except Exception as e:
        logger.error("图片代理错误: %s", str(e))
        return error_response(500, f"Image proxy error: {str(e)}", status_code=500)


@app.get("/v1/translate")
async def translate(
    q: str = Query(""),
    to: str = Query(""),
):
    """翻译接口 — 直接返回原文"""
    return success_response({"translated_text": q})


@app.get("/")
async def root():
    """首页 — 显示服务状态"""
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]
    return {
        "service": "MetaTube CSV Server",
        "version": VERSION,
        "movies_loaded": count,
        "status": "running",
    }


# ============================================================
# 入口
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="MetaTube CSV Server — 伪造的 MetaTube 后端服务")
    parser.add_argument("--csv", required=True, help="CSV 数据文件路径")
    parser.add_argument("--db", default="", help="SQLite 数据库路径（默认与 CSV 同目录）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--reimport", action="store_true", help="强制重新导入 CSV（忽略已有数据库）")
    parser.add_argument("--fallback", default="", help="真实 MetaTube Server 地址，本地查不到时回退（如 http://metatube:8080）")
    parser.add_argument("--token", default="", help="Bearer Token 认证密钥（空则不启用认证）")
    args = parser.parse_args()

    # 配置应用
    if args.db:
        config.db_path = args.db
    else:
        config.db_path = str(Path(args.csv).with_suffix(".db"))

    config.fallback_server = args.fallback.rstrip("/") if args.fallback else ""
    if config.fallback_server:
        logger.info("回退服务端: %s", config.fallback_server)

    config.token = args.token or ""
    if config.token:
        logger.info("已启用 Bearer Token 认证")

    # 初始化数据库
    init_db()

    # 判断是否需要导入 CSV
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM movies").fetchone()[0]

    # 自动检测 CSV 文件是否比数据库更新
    csv_mtime = os.path.getmtime(args.csv) if os.path.exists(args.csv) else 0
    db_mtime = os.path.getmtime(config.db_path) if os.path.exists(config.db_path) else 0
    csv_newer = csv_mtime > db_mtime

    if count == 0 or args.reimport or csv_newer:
        if args.reimport and count > 0:
            logger.info("强制重新导入，清空现有数据...")
            with get_db() as conn:
                conn.execute("DELETE FROM movies")
                conn.commit()
        elif csv_newer and count > 0:
            logger.info("CSV 文件比数据库更新，自动重新导入...")
            with get_db() as conn:
                conn.execute("DELETE FROM movies")
                conn.commit()
        logger.info("开始导入 CSV: %s", args.csv)
        import_csv_to_db(args.csv)
    else:
        logger.info("数据库已有 %d 条记录，跳过 CSV 导入（使用 --reimport 强制重新导入）", count)

    # 启动 Web 服务
    logger.info("服务启动: http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
