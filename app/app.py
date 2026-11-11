"""林道巡护倒木采集系统（Flask + SQLite）

- /            采集页（巡护员）：离线可用，待传记录存浏览器 IndexedDB
- /records     清障页（清障队）：按棵列出倒木，同树核定 / 撤销 / 分组查看原照
- /api/records         GET 列表 / POST 补传（uuid 幂等，同一笔只入库一次）
- /api/judgments       GET 列表 / POST 同树核定（事务内查冲突，矛盾拒绝）
- /api/judgments/<id>  DELETE 撤销一条核定，分组按剩余核定自动重算
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

-- 数据员的同树核定：两笔已入库记录是否同一棵倒木。
-- 「按棵分组」不落库，始终由现存核定现算，撤销任一条后自动重算。
CREATE TABLE IF NOT EXISTS judgments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_a    INTEGER NOT NULL REFERENCES records (id),
    record_b    INTEGER NOT NULL REFERENCES records (id),
    verdict     TEXT NOT NULL CHECK (verdict IN ('same', 'different')),
    created_at  TEXT NOT NULL,
    CHECK (record_a < record_b),        -- 记录对规范化：(a,b) 与 (b,a) 同义
    UNIQUE (record_a, record_b)         -- 同一对只留一条核定：并发写入的兜底
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
        # isolation_level=None：关闭隐式事务，核定的 BEGIN IMMEDIATE
        # 临界区完全由代码显式控制（见 insert_judgment）。
        g.db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA busy_timeout=5000")
        g.db.execute("PRAGMA foreign_keys=ON")
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


# ---------------- 同树核定：分组与冲突 ----------------

class JudgmentConflict(Exception):
    """本次核定与已入库核定矛盾；records 为冲突涉及的记录编号。"""

    def __init__(self, records, message):
        super().__init__(message)
        self.records = list(records)
        self.message = message


def _union_find(pairs):
    """由「同一棵」记录对构建并查集，返回 find(x) 闭包。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    return find


def group_records(records, judgments):
    """按现存核定把记录分成若干棵：经「同一棵」核定（含多手传递）相连的
    记录归为一组；未参与核定的记录各自单独成组。按组内最小编号排序。"""
    find = _union_find(
        (j["record_a"], j["record_b"]) for j in judgments
        if j["verdict"] == "same"
    )
    buckets = {}
    for r in records:
        buckets.setdefault(find(r["id"]), []).append(r)
    return sorted(buckets.values(), key=lambda grp: grp[0]["id"])


def insert_judgment(db, id_a, id_b, verdict):
    """写入一条同树核定，返回 (judgment_id, duplicate)。

    「读取现有核定 → 冲突检查 → 写入」放在同一个 BEGIN IMMEDIATE 事务里：
    SQLite 写锁把并发提交串行化，后进入的事务读到的是前者已提交的状态，
    因此两个窗口同时提交也不会写入互相矛盾的判断。
    冲突时抛 JudgmentConflict（携带冲突记录编号），本次不写入。
    """
    a, b = (id_a, id_b) if id_a < id_b else (id_b, id_a)

    db.execute("BEGIN IMMEDIATE")
    try:
        rows = db.execute(
            "SELECT id, record_a, record_b, verdict FROM judgments"
        ).fetchall()

        # 同一对记录已有核定：同结论 = 重复确认（幂等），反结论 = 冲突
        for r in rows:
            if r["record_a"] == a and r["record_b"] == b:
                if r["verdict"] == verdict:
                    db.commit()
                    return r["id"], True
                raise JudgmentConflict(
                    [a, b],
                    "记录 #%d 与 #%d 已核定为「%s」，与本次核定矛盾"
                    % (a, b, "同一棵" if r["verdict"] == "same" else "不同棵"),
                )

        find = _union_find(
            (r["record_a"], r["record_b"]) for r in rows
            if r["verdict"] == "same"
        )

        if verdict == "same":
            # 合并 a、b 所在两组后，任何横跨两组的「不同棵」核定都会被违反。
            # 不变式：现存 different 核定的两端必不在同组（否则当时的
            # same 核定就不会通过），故两端根集合相等即构成冲突。
            for r in rows:
                if r["verdict"] == "different" and \
                        {find(r["record_a"]), find(r["record_b"])} == \
                        {find(a), find(b)}:
                    x, y = r["record_a"], r["record_b"]
                    raise JudgmentConflict(
                        [x, y],
                        "与已有核定冲突：记录 #%d 与 #%d 已核定为不同棵，"
                        "不能并入同一棵" % (x, y),
                    )
        else:
            # 两笔已被「同一棵」核定（可能经多手传递）连在一起
            if find(a) == find(b):
                raise JudgmentConflict(
                    [a, b],
                    "与已有核定冲突：记录 #%d 与 #%d 已被「同一棵」核定相连，"
                    "不能核定为不同棵" % (a, b),
                )

        cur = db.execute(
            "INSERT INTO judgments (record_a, record_b, verdict, created_at)"
            " VALUES (?, ?, ?, ?)",
            (a, b, verdict, datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
        return cur.lastrowid, False
    except JudgmentConflict:
        db.rollback()
        raise
    except sqlite3.IntegrityError:
        # UNIQUE(record_a, record_b) 兜底：并发下同对记录只进一条
        db.rollback()
        row = db.execute(
            "SELECT id, verdict FROM judgments"
            " WHERE record_a = ? AND record_b = ?",
            (a, b),
        ).fetchone()
        if row is not None:
            if row["verdict"] == verdict:
                return row["id"], True
            raise JudgmentConflict(
                [a, b],
                "与已有核定冲突：记录 #%d 与 #%d 已存在相反结论的核定" % (a, b),
            )
        raise  # 非记录对唯一约束引发的完整性错误，按 500 处理
    except Exception:
        db.rollback()
        raise


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
    db = get_db()
    records = db.execute("SELECT * FROM records ORDER BY id").fetchall()
    judgments = db.execute(
        "SELECT * FROM judgments ORDER BY id DESC"
    ).fetchall()
    groups = group_records(records, judgments)
    return render_template("records.html", records=records,
                           judgments=judgments, groups=groups)


# ---------------- API：巡护记录 ----------------

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


# ---------------- API：同树核定 ----------------

@app.route("/api/judgments", methods=["GET"])
def api_judgments_list():
    rows = get_db().execute(
        "SELECT id, record_a, record_b, verdict, created_at "
        "FROM judgments ORDER BY id DESC"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/judgments", methods=["POST"])
def api_judgments_create():
    data = request.get_json(silent=True) or request.form
    try:
        id_a = int(data.get("a", ""))
        id_b = int(data.get("b", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "记录编号缺失或非法"}), 400
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("same", "different"):
        return jsonify({"ok": False,
                        "error": "verdict 只能是 same 或 different"}), 400
    if id_a == id_b:
        return jsonify({"ok": False,
                        "error": "同一笔记录无需核定，请选择两笔不同记录"}), 400

    db = get_db()
    missing = [
        rid for rid in sorted({id_a, id_b})
        if db.execute("SELECT 1 FROM records WHERE id = ?",
                      (rid,)).fetchone() is None
    ]
    if missing:
        return jsonify({"ok": False, "error": "记录 " +
                        "、".join("#%d" % i for i in missing) +
                        " 不存在（尚未入库）"}), 400

    try:
        jid, duplicate = insert_judgment(db, id_a, id_b, verdict)
    except JudgmentConflict as c:
        return jsonify({
            "ok": False,
            "error": c.message,
            "conflict": {"records": c.records},   # 冲突涉及的记录编号
        }), 409
    except sqlite3.OperationalError:
        return jsonify({"ok": False, "error": "数据库繁忙，请稍后重试"}), 503
    return jsonify({"ok": True, "id": jid, "duplicate": duplicate})


@app.route("/api/judgments/<int:jid>", methods=["DELETE"])
def api_judgments_delete(jid):
    # 撤销一条核定；分组不落库，随剩余核定自动重算
    cur = get_db().execute("DELETE FROM judgments WHERE id = ?", (jid,))
    if cur.rowcount == 0:
        return jsonify({"ok": False, "error": "该核定不存在或已撤销"}), 404
    return jsonify({"ok": True})


@app.route("/photos/<path:name>")
def photo_file(name):
    return send_from_directory(PHOTO_DIR, name)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
