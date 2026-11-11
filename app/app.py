"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障队查看页：只展示已确认入库的记录
- /api/records GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /photos/...  已入库的原照
"""
import os
import sqlite3
import uuid as uuidlib
from datetime import datetime, timezone

from flask import Flask, g, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
DB_PATH = os.path.join(DATA_DIR, "records.db")
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024  # 原照最大 30MB

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid        TEXT NOT NULL UNIQUE,   -- 浏览器生成的幂等键
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL,          -- photos 目录下的文件名
    created_at  TEXT NOT NULL,          -- 现场采集时间（浏览器上报）
    uploaded_at TEXT NOT NULL           -- 入库时间（服务器）
);
"""


def init_db():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.after_request
def no_store_html(resp):
    # HTML 页面不做 HTTP 缓存，保证在线时拿到最新数据；
    # 离线可用性由 Service Worker 的 Cache Storage 负责。
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 页面 ----------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    # Service Worker 必须挂在根作用域下：从 / 路径提供，控制整个站点
    resp = send_from_directory(os.path.join(BASE_DIR, "static"), "sw.js",
                               mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/records")
def records_page():
    rows = get_db().execute(
        "SELECT * FROM records ORDER BY id DESC"
    ).fetchall()
    return render_template("records.html", records=rows)


# ---------------- API ----------------

@app.route("/api/records", methods=["GET"])
def api_list():
    rows = get_db().execute(
        "SELECT id, uuid, lat, lng, note, photo, created_at, uploaded_at "
        "FROM records ORDER BY id DESC"
    ).fetchall()
    return jsonify([
        {
            "id": r["id"],
            "uuid": r["uuid"],
            "lat": r["lat"],
            "lng": r["lng"],
            "note": r["note"],
            "photo_url": "/photos/" + r["photo"],
            "created_at": r["created_at"],
            "uploaded_at": r["uploaded_at"],
        }
        for r in rows
    ])


@app.route("/api/records", methods=["POST"])
def api_create():
    # ---- 校验 ----
    client_uuid = (request.form.get("uuid") or "").strip()
    try:
        client_uuid = str(uuidlib.UUID(client_uuid))  # 规范化
    except (ValueError, AttributeError):
        return jsonify({"ok": False, "error": "uuid 缺失或格式非法"}), 400

    try:
        lat = float(request.form.get("lat", ""))
        lng = float(request.form.get("lng", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "经纬度缺失或非法"}), 400
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return jsonify({"ok": False, "error": "经纬度超出范围"}), 400

    note = (request.form.get("note") or "").strip()[:500]
    created_at = (request.form.get("created_at") or "").strip() or \
        datetime.now(timezone.utc).isoformat()

    photo = request.files.get("photo")
    if photo is None or not photo.filename:
        return jsonify({"ok": False, "error": "缺少现场照片"}), 400

    db = get_db()

    # ---- 幂等：已入库则直接确认，不重复写入 ----
    existing = db.execute(
        "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
    ).fetchone()
    if existing:
        return jsonify({"ok": True, "duplicate": True, "id": existing["id"]})

    # ---- 保存原照（文件名用 uuid，天然去重）----
    ext = os.path.splitext(secure_filename(photo.filename))[1].lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"
    fname = client_uuid + ext
    photo.save(os.path.join(PHOTO_DIR, fname))

    # ---- 入库；唯一约束兜底并发/重试竞争 ----
    try:
        cur = db.execute(
            "INSERT INTO records (uuid, lat, lng, note, photo, created_at, uploaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_uuid, lat, lng, note, fname, created_at,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        try:
            os.remove(os.path.join(PHOTO_DIR, fname))
        except OSError:
            pass
        row = db.execute(
            "SELECT id FROM records WHERE uuid = ?", (client_uuid,)
        ).fetchone()
        return jsonify({"ok": True, "duplicate": True,
                        "id": row["id"] if row else None})

    return jsonify({"ok": True, "duplicate": False, "id": cur.lastrowid})


@app.route("/photos/<path:name>")
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
