const $ = (selector) => document.querySelector(selector);
const bridge = window.AstrBotPluginView;
const gallery = $("#gallery");
const template = $("#avatar-template");
const accountSelect = $("#account-select");
const fileInput = $("#file-input");
const previewDialog = $("#preview-dialog");
const renameDialog = $("#rename-dialog");
const deleteDialog = $("#delete-dialog");
let accounts = [];
let activeKey = "";
let currentFiles = [];
let lastSelection = null;
let cooldownEnd = 0;
let loading = false;
let busy = false;
let generation = 0;
let previewGeneration = 0;
let dialogItem = null;
const previews = new Map();

function notify(message, kind = "info") {
  const notice = $("#notice");
  notice.textContent = message;
  notice.dataset.kind = kind;
  notice.hidden = false;
}

function setBusy(value) {
  busy = value;
  accountSelect.disabled = value || !accounts.length;
  $("#upload-button").disabled = value || !activeKey || loading;
  $("#empty-upload").disabled = value || !activeKey || loading;
  $("#refresh-button").disabled = value || loading;
  gallery.querySelectorAll(".rename-button, .delete-button").forEach((button) => {
    button.disabled = value;
  });
  updateCooldown();
}

function updateCooldown() {
  const remaining = Math.max(0, Math.ceil((cooldownEnd - Date.now()) / 1000));
  $("#cooldown-status").textContent = !activeKey
    ? "—"
    : remaining
      ? `更换冷却还剩 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒`
      : "可以更换头像";
  gallery.querySelectorAll(".apply-button").forEach((button) => {
    const previous = button.dataset.file === lastSelection;
    button.disabled = busy || remaining > 0 || previous;
    button.textContent = previous ? "上次设置的头像" : remaining ? "冷却中" : "设为头像";
  });
}

function emptyState(title, description, allowUpload = false) {
  gallery.replaceChildren();
  $("#empty-title").textContent = title;
  $("#empty-description").textContent = description;
  $("#empty-upload").hidden = !allowUpload;
  $("#empty-state").hidden = false;
  $("#image-count").textContent = "0";
}

function sizeLabel(bytes) {
  return bytes >= 1024 * 1024
    ? `${(bytes / 1024 / 1024).toFixed(2)} MiB`
    : `${Math.max(1, Math.round(bytes / 1024))} KiB`;
}

async function fillThumbnails(cards, key, revision) {
  // A small worker pool bounds bridge traffic even for a large library.
  let index = 0;
  async function worker() {
    while (index < cards.length && generation === revision) {
      const { card, file } = cards[index++];
      const cacheKey = `${key}/${file.file_name}`;
      try {
        let url = previews.get(cacheKey);
        if (!url) {
          const response = await bridge.apiGet("image", {
            account_key: key,
            file_name: file.file_name,
          });
          url = response.data_url;
          previews.set(cacheKey, url);
        }
        if (generation !== revision) return;
        card.querySelector("img").src = url;
        card.querySelector(".image-placeholder").hidden = true;
      } catch {
        if (generation !== revision) return;
        card.querySelector(".image-placeholder").textContent = "预览不可用，点击重试";
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, cards.length) }, worker));
}

function renderFiles(revision) {
  gallery.replaceChildren();
  $("#image-count").textContent = String(currentFiles.length);
  $("#empty-state").hidden = Boolean(currentFiles.length);
  if (!currentFiles.length) {
    emptyState("这个账号还没有头像", "上传图片或拖到这里，即可加入头像库。", true);
    return;
  }
  const key = activeKey;
  const cards = [];
  for (const file of currentFiles) {
    const card = template.content.firstElementChild.cloneNode(true);
    const previous = file.file_name === lastSelection;
    card.classList.toggle("selected", previous);
    card.querySelector(".selection-mark").hidden = !previous;
    card.querySelector("img").alt = file.file_name;
    card.querySelector(".file-name").textContent = file.file_name;
    card.querySelector(".file-name").title = file.file_name;
    card.querySelector(".file-size").textContent = sizeLabel(file.size_bytes);
    const item = { account_key: key, file_name: file.file_name };
    const imageButton = card.querySelector(".image-button");
    imageButton.setAttribute("aria-label", `预览 ${file.file_name}`);
    imageButton.addEventListener("click", () => showPreview(item));
    card.querySelector(".rename-button").addEventListener("click", () => openRename(item));
    card.querySelector(".delete-button").addEventListener("click", () => openDelete(item));
    const applyButton = card.querySelector(".apply-button");
    applyButton.dataset.file = file.file_name;
    applyButton.addEventListener("click", () => applyAvatar(item));
    gallery.append(card);
    cards.push({ card, file });
  }
  updateCooldown();
  void fillThumbnails(cards, key, revision);
}

async function loadLibrary() {
  if (!activeKey) return;
  const revision = ++generation;
  const key = activeKey;
  loading = true;
  setBusy(busy);
  emptyState("正在读取头像库", "请稍候。");
  try {
    const result = await bridge.apiGet("avatars", { account_key: key });
    if (generation !== revision) return;
    currentFiles = result.files;
    lastSelection = result.last_selection;
    cooldownEnd = Date.now() + result.cooldown_remaining * 1000;
    renderFiles(revision);
    if (lastSelection && !result.last_selection_exists) {
      $("#cooldown-status").title = `上次设置的 ${lastSelection} 已从图库移除。`;
    } else {
      $("#cooldown-status").title = "此状态记录本插件上次设置的头像。";
    }
  } catch (error) {
    if (generation !== revision) return;
    emptyState("头像库读取失败", error.message);
    notify(error.message, "error");
  } finally {
    if (generation === revision) {
      loading = false;
      setBusy(busy);
    }
  }
}

async function chooseAccount(key) {
  activeKey = key;
  lastSelection = null;
  cooldownEnd = 0;
  const account = accounts.find((item) => item.account_key === key);
  $("#library-path").textContent = account?.library_dir || "—";
  $("#account-settings").textContent = account
    ? `${account.ai_enabled ? "AI 自主已开启" : "AI 自主已关闭"} · ${account.cron ? `定时 ${account.cron}（${account.timezone}）` : "定时未开启"}`
    : "在插件配置中添加 QQ 账号后开始使用。";
  await loadLibrary();
}

async function loadAccounts() {
  loading = true;
  setBusy(busy);
  try {
    const result = await bridge.apiGet("accounts");
    accounts = result.accounts;
    accountSelect.replaceChildren();
    if (!accounts.length) {
      activeKey = "";
      const option = new Option("尚未配置 QQ 账号", "");
      accountSelect.append(option);
      emptyState("先添加一个 QQ 账号", "打开插件配置，填写已有的 OneBot 平台实例 ID 和机器人 QQ 号。");
      $("#account-settings").textContent = "配置保存并重载插件后，点击刷新即可读取。";
      $("#library-path").textContent = "添加账号后自动创建。";
      return;
    }
    for (const account of accounts) {
      accountSelect.append(new Option(`${account.platform_id} · ${account.qq_id}`, account.account_key));
    }
    const key = accounts.some((account) => account.account_key === activeKey)
      ? activeKey
      : accounts[0].account_key;
    accountSelect.value = key;
    await chooseAccount(key);
  } catch (error) {
    emptyState("无法读取账号", error.message);
    notify(error.message, "error");
  } finally {
    loading = false;
    setBusy(busy);
  }
}

async function uploadFiles(files) {
  if (!activeKey || busy || loading || !files.length) return;
  const key = activeKey;
  setBusy(true);
  $("#upload-progress").hidden = false;
  $("#upload-errors").replaceChildren();
  let succeeded = 0;
  for (let index = 0; index < files.length; index++) {
    const file = files[index];
    $("#upload-progress-text").textContent = `正在上传 ${index + 1} / ${files.length}：${file.name}`;
    try {
      if (!/\.(png|jpe?g|webp)$/i.test(file.name)) throw new Error("支持 PNG、JPEG 或 WebP 图片。");
      if (!file.size || file.size > 5 * 1024 * 1024) throw new Error("图片不能为空，大小不能超过 5 MiB。");
      await bridge.upload(`upload/${key}`, file);
      previews.delete(`${key}/${file.name}`);
      succeeded++;
    } catch (error) {
      const item = document.createElement("li");
      item.textContent = `${file.name}：${error.message}`;
      $("#upload-errors").append(item);
    }
  }
  $("#upload-progress-text").textContent = `上传完成：${succeeded} 张成功${succeeded < files.length ? `，${files.length - succeeded} 张未上传` : ""}。`;
  await loadLibrary();
  setBusy(false);
  fileInput.value = "";
}

async function showPreview(item) {
  const revision = ++previewGeneration;
  $("#preview-title").textContent = item.file_name;
  $("#preview-image").hidden = true;
  $("#preview-loading").textContent = "正在加载大图…";
  $("#preview-loading").hidden = false;
  if (!previewDialog.open) previewDialog.showModal();
  try {
    const result = await bridge.apiGet("image", { ...item, full: "1" });
    if (!previewDialog.open || revision !== previewGeneration) return;
    $("#preview-image").src = result.data_url;
    $("#preview-image").alt = item.file_name;
    $("#preview-image").hidden = false;
    $("#preview-loading").hidden = true;
  } catch (error) {
    if (revision === previewGeneration) $("#preview-loading").textContent = error.message;
  }
}

function openRename(item) {
  if (busy) return;
  dialogItem = item;
  const dot = item.file_name.lastIndexOf(".");
  $("#rename-input").value = item.file_name.slice(0, dot);
  $("#rename-suffix").textContent = item.file_name.slice(dot);
  $("#rename-error").hidden = true;
  renameDialog.showModal();
  $("#rename-input").select();
}

function openDelete(item) {
  if (busy) return;
  dialogItem = item;
  $("#delete-description").textContent = item.file_name;
  $("#delete-error").hidden = true;
  deleteDialog.showModal();
}

async function applyAvatar(item) {
  if (busy) return;
  setBusy(true);
  notify(`正在将 ${item.file_name} 设置为 QQ 头像…`);
  try {
    const result = await bridge.apiPost("apply", item);
    notify(result.message, result.changed ? "success" : "info");
  } catch (error) {
    notify(error.message, "error");
  } finally {
    await loadLibrary();
    setBusy(false);
  }
}

$("#rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const item = { ...dialogItem };
  const newName = $("#rename-input").value.trim() + $("#rename-suffix").textContent;
  $("#rename-submit").disabled = true;
  setBusy(true);
  try {
    await bridge.apiPost("rename", { ...item, new_name: newName });
    previews.delete(`${item.account_key}/${item.file_name}`);
    renameDialog.close();
    notify(`已重命名为 ${newName}。`, "success");
    await loadLibrary();
  } catch (error) {
    $("#rename-error").textContent = error.message;
    $("#rename-error").hidden = false;
  } finally {
    $("#rename-submit").disabled = false;
    setBusy(false);
  }
});

$("#delete-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const item = { ...dialogItem };
  $("#delete-submit").disabled = true;
  setBusy(true);
  try {
    await bridge.apiPost("delete", item);
    previews.delete(`${item.account_key}/${item.file_name}`);
    deleteDialog.close();
    notify(`已从图库删除 ${item.file_name}。`, "success");
    await loadLibrary();
  } catch (error) {
    $("#delete-error").textContent = error.message;
    $("#delete-error").hidden = false;
  } finally {
    $("#delete-submit").disabled = false;
    setBusy(false);
  }
});

$(".cancel-rename").addEventListener("click", () => renameDialog.close());
$(".cancel-delete").addEventListener("click", () => deleteDialog.close());
$(".close-preview").addEventListener("click", () => previewDialog.close());
$("#upload-button").addEventListener("click", () => fileInput.click());
$("#empty-upload").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => void uploadFiles(Array.from(fileInput.files)));
accountSelect.addEventListener("change", () => {
  $("#notice").hidden = true;
  void chooseAccount(accountSelect.value);
});
$("#refresh-button").addEventListener("click", () => {
  previews.clear();
  void loadAccounts();
});

let dragDepth = 0;
const dropZone = $("#drop-zone");
dropZone.addEventListener("dragenter", (event) => {
  event.preventDefault();
  if (!activeKey || busy || loading) return;
  dragDepth++;
  dropZone.classList.add("dragging");
});
dropZone.addEventListener("dragover", (event) => event.preventDefault());
dropZone.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) dropZone.classList.remove("dragging");
});
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  dragDepth = 0;
  dropZone.classList.remove("dragging");
  void uploadFiles(Array.from(event.dataTransfer.files));
});

if (!bridge) {
  emptyState("请从 AstrBot 打开头像库", "在插件详情页中打开此视图，即可管理头像。");
  $("#account-settings").textContent = "尚未连接 AstrBot。";
  $("#refresh-button").disabled = true;
} else {
  const context = await bridge.ready();
  const updateTheme = (value) => {
    document.documentElement.dataset.theme = value.isDark ? "dark" : "light";
  };
  updateTheme(context);
  const off = bridge.onContext(updateTheme);
  const timer = setInterval(updateCooldown, 1000);
  window.addEventListener("beforeunload", () => {
    off();
    clearInterval(timer);
  });
  await loadAccounts();
}
