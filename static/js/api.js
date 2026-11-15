/* API 封装：统一 fetch + 错误处理 */

async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, options);
  } catch (e) {
    throw new Error("无法连接服务器，请确认后端已启动");
  }
  if (!resp.ok) {
    let detail = `HTTP ${resp.status}`;
    try {
      const data = await resp.json();
      detail = data.detail || detail;
    } catch (_) { /* 保留状态码信息 */ }
    throw new Error(String(detail));
  }
  return resp.json();
}

export function apiGet(path) {
  return request(path);
}

export function apiPost(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function apiDelete(path) {
  return request(path, { method: "DELETE" });
}

/** 上传 ICC 原始字节（application/octet-stream） */
export function apiUploadICC(iccBytes, meta) {
  const qs = new URLSearchParams({
    name: meta.name || "未命名工艺",
    paper: meta.paper || "",
    ink: meta.ink || "",
    notes: meta.notes || "",
    filename: meta.filename || "",
  });
  return request(`/api/processes?${qs.toString()}`, {
    method: "POST",
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
