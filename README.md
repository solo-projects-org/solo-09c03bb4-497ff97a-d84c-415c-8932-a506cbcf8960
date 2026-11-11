# 林道巡护 · 倒木离线采集系统

巡护员在林道上（无信号）发现倒木时，用浏览器记录经纬度、拍摄现场照片，先存本机；
回站联网后一键补传，坐标与原照写入 SQLite。几名巡护员可能拍到同一棵倒木，
数据员在清障页对两笔记录做「同一棵 / 不同棵」核定，清障队按棵清点、安排清障。

## 功能与实现

| 需求 | 实现 |
| --- | --- |
| 首次在线打开后，断网刷新也能填写 | Service Worker 安装时缓存应用外壳（页面/JS/CSS/样例照片），断网时导航请求回退到缓存 |
| 待传记录不丢 | 记录（含照片 Blob）存入浏览器 IndexedDB，刷新、关机重启均在 |
| 回网点补传，坐标+原照入 SQLite | `POST /api/records`（multipart），照片落盘、元数据入库 |
| 收到确认才移出待传 | 浏览器仅在收到服务器 `{"ok": true}` 响应后才删除 IndexedDB 中的该笔 |
| 确认丢失，重传只入库一次 | 每笔记录保存时生成 UUID 作幂等键；`records.uuid` 唯一约束，重复提交返回 `duplicate: true` 直接确认，不重复写行、不重复存照片 |
| 入库后才供清障队查看 | `/records` 清障页只渲染 SQLite 中已确认的记录 |
| 两笔记录核定同树/不同棵，判断入 SQLite | `POST /api/judgments` 写入 `judgments` 表；记录对规范化 `(a<b)` 加唯一约束，原始巡护记录与照片始终保留 |
| 按棵列出，间接相连自动归并 | 分组不落库：每次由现存「同一棵」核定做并查集现算，传递相连的记录归为一棵，未归并的单独成组；每棵可展开查看组内记录与原照 |
| 已判不同棵不得同组，并发也不写入矛盾 | 核定在 `BEGIN IMMEDIATE` 事务内「重读全部核定 → 冲突检查 → 写入」，SQLite 写锁把两个窗口的提交串行化；冲突返回 `409` 并在 `conflict.records` 中指出冲突的记录编号，本次不写入 |
| 可撤销任一核定，按剩余重算 | `DELETE /api/judgments/<id>`；分组始终由现存核定现算，撤销后自动重新分组 |
| 随附样例照片 | `app/static/sample-photo.png`（演示时无相机可点"使用样例照片"，离线也能取到） |

## 启动

```bash
docker compose up --build
```

浏览器打开 <http://localhost:8000>（Service Worker 要求 localhost 或 HTTPS 环境）。

SQLite 与已入库原照持久化在命名卷 `patrol-data`（容器内 `/data`）。

## 演示：断网采集 → 回网补传

1. **首次在线打开** `http://localhost:8000`，页面右下角无提示即已就绪（Service Worker 已缓存全部外壳资源）。
2. **模拟断网**（任选其一）：
   - 浏览器 DevTools → Network → 勾选 **Offline**；或
   - `docker compose stop`（更真实地模拟"林道上没信号"）。
3. **刷新页面** —— 页面照常打开。填写经纬度（或点"获取当前定位"），选择照片
   （演示可点"使用随附样例照片"），点 **保存到待传记录**。可多采几笔，再次刷新待传列表仍在。
4. **恢复网络**：取消 Offline，或 `docker compose start`。
5. 点 **📤 补传全部待传记录** —— 每笔收到服务器确认后才从待传列表移除。
6. 打开 **/records**（清障队查看页）即可看到已入库的坐标与原照。

**验证幂等**：补传途中若在服务器写入后断网（确认丢失），该笔仍留在待传；
再次补传时服务器按 UUID 判重，返回 `duplicate: true`，浏览器正常移出待传，
SQLite 中始终只有一行。

## 演示：按棵清点（归并与撤销）

1. 按上节步骤用样例照片录入几笔（建议 3 笔以上），打开 **/records** 清障页。
2. 在「同树核定」中选两笔记录，核定 选 **同一棵** 或 **不同棵** 提交：
   - 判定同一棵的记录（含经其他核定间接相连的）自动归为一棵，展开可查看组内记录与原照；
   - 与已有核定矛盾的提交被拒绝，页面提示冲突的记录编号（如：已判不同棵的两笔不能并入同一棵）；
   - 两个窗口同时提交相反结论也不会都写入——后到的事务读到先到的结果后被拒绝。
3. 「核定记录」列表中可 **撤销** 任一条核定，分组按剩余核定自动重算；
   原始巡护记录与照片不受影响。

## 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
DATA_DIR=./data python3 app/app.py        # 或：gunicorn --chdir app -b 0.0.0.0:8000 app:app
```

## 目录结构

```
├── docker-compose.yml      # 一键启动，卷持久化 /data
├── Dockerfile              # python:3.12-slim + gunicorn
├── requirements.txt
├── app/
│   ├── app.py              # Flask 路由 + SQLite（uuid 唯一约束幂等）
│   ├── templates/
│   │   ├── index.html      # 采集页（巡护员）
│   │   └── records.html    # 清障页：同树核定 + 按棵分组 + 核定撤销（清障队）
│   └── static/
│       ├── app.js          # IndexedDB 待传队列 + 补传逻辑
│       ├── sw.js           # Service Worker（离线外壳缓存）
│       ├── style.css
│       └── sample-photo.png# 随附样例照片
└── tools/
    └── make_sample_photo.py# 样例照片生成脚本（纯标准库）
```

## API

- `GET  /api/records` — 已入库记录 JSON 列表
- `POST /api/records` — 补传（multipart：`uuid`, `lat`, `lng`, `note`, `created_at`, `photo`）
  - 成功：`{"ok": true, "duplicate": false, "id": N}`
  - 重复（同一 uuid）：`{"ok": true, "duplicate": true, "id": N}` — 浏览器同样视为确认
  - 校验失败：`400 {"ok": false, "error": "..."}`
- `GET  /api/judgments` — 核定列表
- `POST /api/judgments` — 同树核定（JSON 或表单：`a`, `b` 为记录编号，`verdict` 为 `same`/`different`）
  - 成功：`{"ok": true, "id": N, "duplicate": false}`；同对同结论重复提交返回 `duplicate: true`
  - 冲突：`409 {"ok": false, "error": "...", "conflict": {"records": [x, y]}}` — 未写入，`records` 为冲突的记录编号
  - 校验失败：`400`；数据库繁忙：`503`（可重试）
- `DELETE /api/judgments/<id>` — 撤销一条核定，分组按剩余核定自动重算
- `GET  /photos/<file>` — 已入库原照
