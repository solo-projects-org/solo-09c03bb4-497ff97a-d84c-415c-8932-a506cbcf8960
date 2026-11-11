/* 林道巡护采集页逻辑：离线保存（IndexedDB）+ 回网补传（确认后才删除） */
"use strict";

/* ---------- Service Worker 注册（首次在线打开后，断网也能加载本页） ---------- */
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js")   // 根路径脚本，默认作用域 "/"
    .catch(err => console.error("SW 注册失败:", err));
}

/* ---------- IndexedDB：待传记录仓库 ---------- */
const DB_NAME = "forest-patrol";
const STORE = "pending";

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = () => {
      req.result.createObjectStore(STORE, { keyPath: "uuid" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function idbAdd(rec) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).put(rec);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

async function idbAll() {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const req = db.transaction(STORE, "readonly").objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

async function idbDelete(uuid) {
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).delete(uuid);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
  });
}

/* ---------- 页面元素 ---------- */
const $ = id => document.getElementById(id);
const latInput = $("lat"), lngInput = $("lng"), noteInput = $("note");
const photoInput = $("photo"), preview = $("preview");
const pendingList = $("pendingList"), pendingCount = $("pendingCount");
const syncStatus = $("syncStatus"), netStatus = $("netStatus");

/* ---------- 网络状态提示 ---------- */
function refreshNetStatus() {
  if (navigator.onLine) {
    netStatus.textContent = "🟢 在线：可补传待传记录";
    netStatus.className = "net-status online";
  } else {
    netStatus.textContent = "🔴 离线：可正常填写保存，记录暂存本机";
    netStatus.className = "net-status offline";
  }
}
window.addEventListener("online", refreshNetStatus);
window.addEventListener("offline", refreshNetStatus);
refreshNetStatus();

/* ---------- 照片预览 ---------- */
function showPreview(blob) {
  preview.src = URL.createObjectURL(blob);
  preview.hidden = false;
}
photoInput.addEventListener("change", () => {
  if (photoInput.files[0]) showPreview(photoInput.files[0]);
});

/* 使用随附样例照片（样例已被 Service Worker 缓存，离线也能取到） */
$("sampleBtn").addEventListener("click", async () => {
  try {
    const resp = await fetch("/static/sample-photo.png");
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const blob = await resp.blob();
    const file = new File([blob], "sample-photo.png", { type: "image/png" });
    const dt = new DataTransfer();
    dt.items.add(file);
    photoInput.files = dt.files;
    showPreview(blob);
  } catch (e) {
    alert("样例照片加载失败：" + e.message);
  }
});

/* ---------- 定位 ---------- */
$("geoBtn").addEventListener("click", () => {
  if (!navigator.geolocation) return alert("此浏览器不支持定位");
  navigator.geolocation.getCurrentPosition(
    pos => {
      latInput.value = pos.coords.latitude.toFixed(6);
      lngInput.value = pos.coords.longitude.toFixed(6);
    },
    err => alert("定位失败：" + err.message),
    { enableHighAccuracy: true, timeout: 10000 }
  );
});

/* ---------- 保存到待传（离线可用） ---------- */
$("collectForm").addEventListener("submit", async () => {
  const lat = parseFloat(latInput.value);
  const lng = parseFloat(lngInput.value);
  if (Number.isNaN(lat) || Number.isNaN(lng)) return alert("请填写经纬度");
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180)
    return alert("经纬度超出范围");
  const file = photoInput.files[0];
  if (!file) return alert("请选择或拍摄现场照片");

  const rec = {
    uuid: crypto.randomUUID(),          // 幂等键：重传同一笔只入库一次
    lat, lng,
    note: noteInput.value.trim(),
    createdAt: new Date().toISOString(),
    photo: file,                        // File/Blob 可直接存入 IndexedDB
    photoName: file.name || "photo.jpg",
  };
  await idbAdd(rec);

  latInput.value = lngInput.value = noteInput.value = "";
  photoInput.value = "";
  preview.hidden = true;
  await renderPending();
});

/* ---------- 渲染待传列表 ---------- */
async function renderPending() {
  const items = await idbAll();
  items.sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  pendingCount.textContent = items.length;
  pendingList.innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.className = "pending-item";
    const img = document.createElement("img");
    img.src = URL.createObjectURL(it.photo);
    img.alt = "待传照片";
    img.className = "thumb";
    const info = document.createElement("div");
    info.innerHTML =
      `<strong>${it.lat.toFixed(6)}, ${it.lng.toFixed(6)}</strong><br>` +
      `<small>${it.createdAt.slice(0, 19).replace("T", " ")}</small>` +
      (it.note ? `<br>${it.note.replace(/[<>&]/g, c =>
        ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]))}` : "");
    li.append(img, info);
    pendingList.appendChild(li);
  }
}

/* ---------- 补传：收到服务器确认才移出待传 ---------- */
$("syncBtn").addEventListener("click", async () => {
  const items = await idbAll();
  if (!items.length) {
    syncStatus.textContent = "没有待传记录。";
    return;
  }
  syncStatus.textContent = "补传中…";
  let ok = 0, dup = 0, fail = 0;

  for (const it of items) {
    const fd = new FormData();
    fd.append("uuid", it.uuid);
    fd.append("lat", it.lat);
    fd.append("lng", it.lng);
    fd.append("note", it.note || "");
    fd.append("created_at", it.createdAt);
    fd.append("photo", it.photo, it.photoName || it.uuid + ".jpg");

    try {
      const resp = await fetch("/api/records", { method: "POST", body: fd });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      if (!data.ok) throw new Error(data.error || "服务器拒绝");
      // 关键：只有拿到服务器确认后才删除本地待传记录；
      // 若确认在途中丢失，记录仍在本地，重传时服务器按 uuid 去重。
      await idbDelete(it.uuid);
      data.duplicate ? dup++ : ok++;
    } catch (e) {
      console.warn("补传失败，保留待传:", it.uuid, e);
      fail++;
    }
  }

  await renderPending();
  syncStatus.textContent =
    `补传完成：新入库 ${ok} 笔` +
    (dup ? `，服务器已有 ${dup} 笔（未重复入库）` : "") +
    (fail ? `，失败 ${fail} 笔（仍保留在待传）` : "");
});

/* ---------- 启动 ---------- */
renderPending();
