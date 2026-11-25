/* API 封装：统一 fetch + 错误处理 */

function detailMessage(detail, fallback) {
  if (detail == null || detail === "") return fallback;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === "string") return item;
        if (item && typeof item === "object") return item.message || item.msg || "";
        return "";
      })
      .filter(Boolean);
    return parts.length ? parts.join("；") : fallback;
  }
  if (typeof detail === "object") {
    const code = detail.code || detail.error_code;
    const message = detail.message || detail.msg;
    if (typeof message === "string" && message) {
      return code && !message.includes(String(code)) ? `${message}（${code}）` : message;
    }
    if (detail.detail !== undefined && detail.detail !== null && detail.detail !== detail) {
      const nested = detailMessage(detail.detail, fallback);
      return code && !nested.includes(String(code)) ? `${nested}（${code}）` : nested;
    }
    if (code) return `${fallback}（${code}）`;
    try {
      const serialized = JSON.stringify(detail);
      return serialized && serialized !== "{}" ? serialized : fallback;
    } catch (_) {
      return fallback;
    }
  }
  return String(detail);
}

function errorFromDetail(detail, fallback) {
  const err = new Error(detailMessage(detail, fallback));
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    err.code = detail.code || detail.error_code || "";
    err.detail = detail;
  }
  return err;
}

async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    throw new Error("无法连接服务器，请确认后端已启动");
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      detail = data.detail ?? detail;
    } catch (_) { /* 保留状态码信息 */ }
    throw errorFromDetail(detail, `HTTP ${resp.status}`);
  }
  return resp.json();
}

export function apiGet(path, options = {}) {
  return request(path, options);
}

export function apiPost(path, body, options = {}) {
  return request(path, {
    ...options,
    method: "POST",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    body: JSON.stringify(body),
  });
}

/** POST 原始字节（query 参数携带元数据，Content-Type: application/octet-stream） */
export function apiPostRaw(path, query, bytes, options = {}) {
  const qs = new URLSearchParams(query);
  return request(`${path}?${qs.toString()}`, {
    ...options,
    method: "POST",
    headers: { "Content-Type": "application/octet-stream", ...(options.headers || {}) },
    body: bytes,
  });
}

export function apiDelete(path, options = {}) {
  return request(path, { ...options, method: "DELETE" });
}

export function apiPatch(path, body, options = {}) {
  return request(path, {
    ...options,
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    body: JSON.stringify(body),
  });
}

/** 上传 ICC 原始字节（application/octet-stream） */
export function apiUploadICC(iccBytes, meta, { onProgress } = {}) {
  const qs = new URLSearchParams({
    name: meta.name || "未命名工艺",
    paper: meta.paper || "",
    ink: meta.ink || "",
    notes: meta.notes || "",
    filename: meta.filename || "",
    measurement_condition: meta.measurement_condition || "",
  });
  if (!onProgress) {
    return request(`/api/processes?${qs.toString()}`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: iccBytes,
    });
  }
  // 带进度：批量上传队列用 XHR upload.onprogress
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/processes?${qs.toString()}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(xhr.responseText)); }
        catch (_) { resolve(null); }
      } else {
        let detail = `HTTP ${xhr.status}`;
        try { detail = JSON.parse(xhr.responseText).detail ?? detail; } catch (_) {}
        reject(errorFromDetail(detail, `HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("无法连接服务器，请确认后端已启动"));
    xhr.send(iccBytes);
  });
}

/** 替换工艺的 ICC 文件（sha 变更，色域/变换缓存自动失效） */
export function apiReplaceICC(pid, iccBytes, filename = "") {
  const qs = new URLSearchParams({ filename: filename || "" });
  return request(`/api/processes/${pid}/icc?${qs.toString()}`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream" },
    body: iccBytes,
  });
}

/** 读取文件为 base64（data URI） */
export function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error("文件读取失败"));
    reader.readAsDataURL(file);
  });
}

/** 读取文件为 ArrayBuffer（ICC 上传用） */
export function fileToArrayBuffer(file) {
  return file.arrayBuffer();
}

/** 触发浏览器下载 */
export function downloadDataUri(dataUri, filename) {
  const a = document.createElement("a");
  a.href = dataUri;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/** 下载 CSV 文本 */
export function downloadCsv(text, filename) {
  const blob = new Blob(["\ufeff" + text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  downloadDataUri(url, filename);
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}
