const SUPPORTED_EXTENSIONS = new Set([
  "csv",
  "tsv",
  "txt",
  "parquet",
  "pq",
  "xlsx",
  "xls",
  "jsonl",
  "ndjson",
  "json",
  "sql",
  "ddl",
]);

const PAGE_ORDER = ["upload", "parse", "semantic", "visual"];
const HISTORY_PAGE_SIZE = 5;

const state = {
  page: "upload",
  run: null,
  result: null,
  reports: [],
  selectedResourceId: null,
  parseTab: "overview",
  chartIndex: 0,
  sourceMode: "folder",
  folderFiles: [],
  ignoredFileCount: 0,
  databaseConnection: null,
  pollTimer: null,
  modelSettings: null,
  history: [],
  historyPage: 0,
  settingsPrompted: false,
};

const elements = {
  healthBadge: document.querySelector("#healthBadge"),
  healthText: document.querySelector("#healthText"),
  navUpload: document.querySelector("#navUpload"),
  navParse: document.querySelector("#navParse"),
  navSemantic: document.querySelector("#navSemantic"),
  navVisual: document.querySelector("#navVisual"),
  openSettingsButton: document.querySelector("#openSettingsButton"),
  closeSettingsButton: document.querySelector("#closeSettingsButton"),
  settingsModal: document.querySelector("#settingsModal"),
  settingsStatus: document.querySelector("#settingsStatus"),
  settingsBaseUrl: document.querySelector("#settingsBaseUrl"),
  settingsModel: document.querySelector("#settingsModel"),
  settingsApiKey: document.querySelector("#settingsApiKey"),
  testSettingsButton: document.querySelector("#testSettingsButton"),
  saveSettingsButton: document.querySelector("#saveSettingsButton"),
  modelButtonStatus: document.querySelector("#modelButtonStatus"),
  modelStatePanel: document.querySelector("#modelStatePanel"),
  modelStateText: document.querySelector("#modelStateText"),
  uploadPage: document.querySelector("#uploadPage"),
  parsePage: document.querySelector("#parsePage"),
  semanticPage: document.querySelector("#semanticPage"),
  visualPage: document.querySelector("#visualPage"),
  uploadRunLabel: document.querySelector("#uploadRunLabel"),
  summarySource: document.querySelector("#summarySource"),
  summaryResourceCount: document.querySelector("#summaryResourceCount"),
  agentFiles: document.querySelector("#agentFiles"),
  chooseFolderButton: document.querySelector("#chooseFolderButton"),
  selectedFolderLabel: document.querySelector("#selectedFolderLabel"),
  folderScanNote: document.querySelector("#folderScanNote"),
  agentFileList: document.querySelector("#agentFileList"),
  fileCountLabel: document.querySelector("#fileCountLabel"),
  uploadHint: document.querySelector("#uploadHint"),
  startButton: document.querySelector("#startButton"),
  folderSourcePanel: document.querySelector("#folderSourcePanel"),
  databaseSourcePanel: document.querySelector("#databaseSourcePanel"),
  dbHost: document.querySelector("#dbHost"),
  dbPort: document.querySelector("#dbPort"),
  dbName: document.querySelector("#dbName"),
  dbUser: document.querySelector("#dbUser"),
  dbPassword: document.querySelector("#dbPassword"),
  connectDatabaseButton: document.querySelector("#connectDatabaseButton"),
  databaseTables: document.querySelector("#databaseTables"),
  databaseTableHint: document.querySelector("#databaseTableHint"),
  databaseTableList: document.querySelector("#databaseTableList"),
  dbMaxRows: document.querySelector("#dbMaxRows"),
  dbTimeStart: document.querySelector("#dbTimeStart"),
  dbTimeEnd: document.querySelector("#dbTimeEnd"),
  refreshHistoryButton: document.querySelector("#refreshHistoryButton"),
  historyList: document.querySelector("#historyList"),
  historyPrevButton: document.querySelector("#historyPrevButton"),
  historyNextButton: document.querySelector("#historyNextButton"),
  historyPageLabel: document.querySelector("#historyPageLabel"),
  runStatusBand: document.querySelector("#runStatusBand"),
  runStatus: document.querySelector("#runStatus"),
  runStep: document.querySelector("#runStep"),
  runIdLabel: document.querySelector("#runIdLabel"),
  runProgressText: document.querySelector("#runProgressText"),
  progressBar: document.querySelector("#progressBar"),
  parseResourceTitle: document.querySelector("#parseResourceTitle"),
  parseResourceCount: document.querySelector("#parseResourceCount"),
  parseResourceList: document.querySelector("#parseResourceList"),
  parseDetailPanel: document.querySelector("#parseDetailPanel"),
  parseEmpty: document.querySelector("#parseEmpty"),
  parseDetailContent: document.querySelector("#parseDetailContent"),
  toSemanticButton: document.querySelector("#toSemanticButton"),
  semanticResourceCount: document.querySelector("#semanticResourceCount"),
  semanticResourceList: document.querySelector("#semanticResourceList"),
  semanticDetailPanel: document.querySelector("#semanticDetailPanel"),
  semanticModelSource: document.querySelector("#semanticModelSource"),
  toVisualButton: document.querySelector("#toVisualButton"),
  visualResourceCount: document.querySelector("#visualResourceCount"),
  batchTable: document.querySelector("#batchTable"),
  visualResourceList: document.querySelector("#visualResourceList"),
  chartTitle: document.querySelector("#chartTitle"),
  chartSeriesSelect: document.querySelector("#chartSeriesSelect"),
  trendChart: document.querySelector("#trendChart"),
  chartMeta: document.querySelector("#chartMeta"),
  downloadSummaryButton: document.querySelector("#downloadSummaryButton"),
  downloadAllButton: document.querySelector("#downloadAllButton"),
  downloadCurrentButton: document.querySelector("#downloadCurrentButton"),
  toastRegion: document.querySelector("#toastRegion"),
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    throw new Error(payload?.detail || `请求失败：HTTP ${response.status}`);
  }
  return payload;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function toast(title, message, kind = "info") {
  const node = document.createElement("div");
  node.className = "toast";
  node.dataset.kind = kind;
  node.innerHTML = `<strong>${esc(title)}</strong><span>${esc(message)}</span>`;
  elements.toastRegion.append(node);
  window.setTimeout(() => node.remove(), 5000);
}

function setBusy(busy) {
  elements.startButton.disabled = busy;
  elements.connectDatabaseButton.disabled = busy;
}

function fileExtension(name) {
  const index = String(name).lastIndexOf(".");
  return index >= 0 ? name.slice(index + 1).toLowerCase() : "";
}

function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size)) return "-";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function formatNumber(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 2,
  }).format(number);
}

function formatPercent(value) {
  if (value === null || value === undefined || value === "") return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  const percent = number <= 1 ? number * 100 : number;
  return `${percent.toFixed(percent >= 10 ? 1 : 2)}%`;
}

function formatDate(value, withTime = true) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(date);
}

function kindCode(kind) {
  return {
    time_series: "TS",
    structured_table: "TB",
    schema: "SC",
  }[kind] || "RS";
}

function kindLabel(kind) {
  return {
    time_series: "时序资源",
    structured_table: "结构化表",
    schema: "表结构",
  }[kind] || kind || "资源";
}

function statusLabel(status) {
  return {
    QUEUED: "排队中",
    RUNNING: "解析中",
    COMPLETED: "已完成",
    FAILED: "失败",
    NORMAL: "正常",
    WATCH: "关注",
    POSSIBLE_FAULT: "疑似异常",
    HIGH_RISK: "高风险",
    UNKNOWN: "待判断",
  }[status] || status || "-";
}

function stepLabel(step) {
  const value = String(step || "");
  if (value === "uploaded") return "等待启动";
  if (value === "scan_input_resources") return "扫描数据资源";
  if (value === "completed") return "解析完成";
  if (value === "failed") return "解析失败";
  if (value.startsWith("parse_resource:")) {
    return `解析资源 ${value.split(":").slice(1).join(":")}`;
  }
  if (value.startsWith("invoke_resource_llm_analysis:")) {
    return `生成语义 ${value.split(":").slice(1).join(":")}`;
  }
  return value || "处理中";
}

function reportName(report) {
  return report?.display_name || report?.resource_name || report?.source?.relative_path || "未命名资源";
}

function reportRows(report) {
  return (
    report?.schema_profile?.row_count ??
    report?.time_axis?.sample_count ??
    report?.quality?.row_count ??
    null
  );
}

function reportMetricCount(report) {
  return (
    report?.metric_fields?.length ||
    report?.statistics?.field_statistics?.length ||
    report?.schema_profile?.columns?.length ||
    0
  );
}

function reportAnomalyCount(report) {
  return (report?.statistics?.metric_summaries || []).reduce(
    (total, item) => total + Number(item.anomaly_count || 0),
    0,
  );
}

function selectedReport() {
  return state.reports.find((report) => report.resource_id === state.selectedResourceId) || null;
}

function formatAttributeValue(value) {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

function updateNavigation() {
  const hasRun = Boolean(state.run);
  const hasResult = Boolean(state.result);
  elements.navParse.disabled = !hasRun;
  elements.navSemantic.disabled = !hasResult;
  elements.navVisual.disabled = !hasResult;
  elements.toSemanticButton.disabled = !hasResult;
  elements.toVisualButton.disabled = !hasResult;
  document.querySelectorAll(".nav-step").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === state.page);
  });
}

function canShowPage(page) {
  if (page === "upload") return true;
  if (page === "parse") return Boolean(state.run);
  return Boolean(state.result);
}

function showPage(page) {
  if (!canShowPage(page)) {
    toast("尚不可进入", page === "parse" ? "请先启动一次解析任务。" : "请等待解析完成。", "error");
    return;
  }
  state.page = page;
  elements.uploadPage.classList.toggle("hidden", page !== "upload");
  elements.parsePage.classList.toggle("hidden", page !== "parse");
  elements.semanticPage.classList.toggle("hidden", page !== "semantic");
  elements.visualPage.classList.toggle("hidden", page !== "visual");
  updateNavigation();
  if (page === "parse") renderParsePage();
  if (page === "semantic") renderSemanticPage();
  if (page === "visual") renderVisualPage();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderFolderFiles() {
  const allFiles = [...elements.agentFiles.files];
  state.folderFiles = allFiles.filter((file) =>
    SUPPORTED_EXTENSIONS.has(fileExtension(file.name)),
  );
  state.ignoredFileCount = allFiles.length - state.folderFiles.length;
  const visible = state.folderFiles.slice(0, 80);
  elements.agentFileList.innerHTML = visible
    .map(
      (file) => `
        <li class="file-chip">
          <span title="${esc(file.webkitRelativePath || file.name)}">${esc(
            file.webkitRelativePath || file.name,
          )}</span>
          <small>${formatBytes(file.size)}</small>
        </li>`,
    )
    .join("");
  if (!state.folderFiles.length) {
    elements.agentFileList.innerHTML =
      '<li class="file-chip"><span>尚未选择支持的数据文件</span><small>-</small></li>';
  } else if (state.folderFiles.length > visible.length) {
    elements.agentFileList.insertAdjacentHTML(
      "beforeend",
      `<li class="file-chip file-chip-more">另有 ${
        state.folderFiles.length - visible.length
      } 个文件</li>`,
    );
  }
  elements.folderScanNote.textContent =
    state.ignoredFileCount > 0
      ? `已扫描 ${allFiles.length} 个文件，其中 ${state.ignoredFileCount} 个格式不支持并已忽略。`
      : "支持 CSV、Excel、JSON、SQL 和 DDL，其他文件会自动忽略。";
  const firstRelativePath = state.folderFiles[0]?.webkitRelativePath || "";
  const rootFolder = firstRelativePath.split("/").filter(Boolean)[0];
  elements.selectedFolderLabel.textContent = rootFolder
    ? `${rootFolder} · ${state.folderFiles.length} 个文件`
    : "选择包含时序数据的文件夹";
  elements.fileCountLabel.textContent = String(state.folderFiles.length);
  elements.uploadHint.textContent = state.folderFiles.length
    ? `${state.folderFiles.length} 个文件待解析`
    : "等待选择数据源";
  updateUploadSummary();
}

function updateUploadSummary() {
  elements.summarySource.textContent =
    state.sourceMode === "database" ? "MySQL 数据表" : "文件夹";
  if (state.sourceMode === "database") {
    const count = elements.databaseTableList.querySelectorAll(
      "input[data-db-index]:checked",
    ).length;
    elements.summaryResourceCount.textContent = String(count);
    elements.uploadRunLabel.textContent = state.databaseConnection
      ? `${state.databaseConnection.database_name} · ${count} 张表`
      : "未连接";
    return;
  }
  elements.summaryResourceCount.textContent = String(state.folderFiles.length);
  elements.uploadRunLabel.textContent = state.folderFiles.length
    ? `${state.folderFiles.length} 个文件`
    : "未开始";
}

function switchSource(mode) {
  state.sourceMode = mode;
  document.querySelectorAll(".source-mode").forEach((button) => {
    button.classList.toggle("active", button.dataset.source === mode);
  });
  elements.folderSourcePanel.classList.toggle("hidden", mode !== "folder");
  elements.databaseSourcePanel.classList.toggle("hidden", mode !== "database");
  elements.uploadHint.textContent =
    mode === "folder"
      ? state.folderFiles.length
        ? `${state.folderFiles.length} 个文件待解析`
        : "等待选择数据源"
      : state.databaseConnection
        ? "数据库已连接"
        : "填写 MySQL 连接信息";
  updateUploadSummary();
}

async function connectDatabase() {
  const request = {
    host: elements.dbHost.value.trim(),
    port: Number(elements.dbPort.value || 3306),
    database: elements.dbName.value.trim(),
    username: elements.dbUser.value.trim(),
    password: elements.dbPassword.value,
  };
  if (!request.host || !request.database || !request.username || !request.password) {
    toast("信息不完整", "请填写主机、数据库、用户名和密码。", "error");
    return;
  }
  elements.connectDatabaseButton.disabled = true;
  try {
    state.databaseConnection = await api("/api/v1/database/connections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    elements.dbPassword.value = "";
    elements.databaseTables.classList.remove("hidden");
    elements.databaseTableHint.textContent = `${
      state.databaseConnection.database_name
    } · ${state.databaseConnection.tables.length} 张表`;
    renderDatabaseTables();
    elements.uploadHint.textContent = "数据库已连接";
    updateUploadSummary();
    toast("连接成功", "请选择需要解析的数据表。", "success");
  } catch (error) {
    state.databaseConnection = null;
    elements.databaseTables.classList.add("hidden");
    updateUploadSummary();
    toast("连接失败", error.message, "error");
  } finally {
    elements.connectDatabaseButton.disabled = false;
  }
}

function renderDatabaseTables() {
  const tables = state.databaseConnection?.tables || [];
  if (!tables.length) {
    elements.databaseTableList.innerHTML =
      '<div class="empty-state"><strong>没有可读取的数据表</strong><span>请确认账号具有读取权限。</span></div>';
    updateUploadSummary();
    return;
  }
  const dateTypes = new Set(["date", "datetime", "timestamp", "time"]);
  elements.databaseTableList.innerHTML = tables
    .map((table, index) => {
      const timeColumns = table.columns.filter((column) =>
        dateTypes.has(String(column.data_type).toLowerCase()),
      );
      const defaultTime = table.default_time_column || timeColumns[0]?.name || "";
      return `
        <div class="database-table-row">
          <label class="table-selector">
            <input type="checkbox" data-db-index="${index}" checked />
            <span>
              <strong>${esc(table.table_name)}</strong>
              <small>${esc(table.schema_name)} · ${table.columns.length} 个字段</small>
            </span>
          </label>
          <label class="time-field-selector">
            <span>时间字段</span>
            <select data-time-field="${index}" ${timeColumns.length ? "" : "disabled"}>
              ${
                timeColumns.length
                  ? timeColumns
                      .map(
                        (column) =>
                          `<option value="${esc(column.name)}" ${
                            column.name === defaultTime ? "selected" : ""
                          }>${esc(column.name)}</option>`,
                      )
                      .join("")
                  : '<option value="">无时间字段</option>'
              }
            </select>
          </label>
        </div>`;
    })
    .join("");
  elements.databaseTableList
    .querySelectorAll("input[data-db-index]")
    .forEach((input) => input.addEventListener("change", updateUploadSummary));
  updateUploadSummary();
}

function databaseSelections() {
  const selected = [];
  for (const checkbox of elements.databaseTableList.querySelectorAll(
    "input[data-db-index]:checked",
  )) {
    const index = checkbox.dataset.dbIndex;
    const table = state.databaseConnection.tables[Number(index)];
    const timeSelect = elements.databaseTableList.querySelector(
      `select[data-time-field="${index}"]`,
    );
    const timeColumn = timeSelect?.value || null;
    selected.push({
      schema_name: table.schema_name,
      table_name: table.table_name,
      time_column: timeColumn,
      time_start: timeColumn ? localDateTimeToIso(elements.dbTimeStart.value) : null,
      time_end: timeColumn ? localDateTimeToIso(elements.dbTimeEnd.value) : null,
    });
  }
  return selected;
}

function localDateTimeToIso(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

async function startDatabaseRun() {
  if (!state.databaseConnection) {
    toast("尚未连接", "请先连接 MySQL 并读取数据表。", "error");
    return;
  }
  const tables = databaseSelections();
  if (!tables.length) {
    toast("尚未选择", "至少选择一张数据表。", "error");
    return;
  }
  const start = elements.dbTimeStart.value;
  const end = elements.dbTimeEnd.value;
  if (start && end && new Date(start) > new Date(end)) {
    toast("时间范围无效", "开始时间不能晚于结束时间。", "error");
    return;
  }
  const request = {
    connection_id: state.databaseConnection.connection_id,
    tables,
    max_rows: Math.min(Number(elements.dbMaxRows.value || 500), 500),
  };
  setBusy(true);
  try {
    state.run = await api("/api/v1/agent/runs/from-database", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    beginRunSession();
  } catch (error) {
    toast("启动失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function startFolderRun() {
  if (!state.folderFiles.length) {
    toast("缺少文件", "请选择包含数据文件的文件夹。", "error");
    return;
  }
  const form = new FormData();
  for (const file of state.folderFiles) {
    form.append("files", file, file.name);
    form.append("relative_paths", file.webkitRelativePath || file.name);
  }
  setBusy(true);
  try {
    state.run = await api("/api/v1/agent/runs", { method: "POST", body: form });
    beginRunSession();
  } catch (error) {
    toast("启动失败", error.message, "error");
  } finally {
    setBusy(false);
  }
}

function startRun() {
  if (state.sourceMode === "database") {
    startDatabaseRun();
  } else {
    startFolderRun();
  }
}

function beginRunSession() {
  state.result = null;
  state.reports = [];
  state.selectedResourceId = null;
  elements.uploadRunLabel.textContent = state.run.run_id;
  renderRunStatus();
  renderParsePage();
  updateNavigation();
  showPage("parse");
  schedulePoll(0);
  loadHistory();
}

function schedulePoll(delay) {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(pollRun, delay);
}

async function pollRun() {
  if (!state.run?.run_id) return;
  try {
    state.run = await api(`/api/v1/agent/runs/${encodeURIComponent(state.run.run_id)}`);
    renderRunStatus();
    if (state.run.status === "COMPLETED") {
      await loadResult(state.run.run_id);
      toast("解析完成", `已生成 ${state.reports.length} 份统一资源 JSON。`, "success");
      loadHistory();
      return;
    }
    if (state.run.status === "FAILED") {
      renderParsePage();
      toast("解析失败", state.run.error || "Agent 运行失败。", "error");
      loadHistory();
      return;
    }
    schedulePoll(1000);
  } catch (error) {
    toast("状态查询失败", error.message, "error");
    schedulePoll(2000);
  }
}

async function loadResult(runId) {
  state.result = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}/result`);
  state.reports = state.result.reports || [];
  if (!state.reports.some((report) => report.resource_id === state.selectedResourceId)) {
    const preferred =
      state.reports.find(
        (report) => report.classification?.data_kind === "time_series",
      ) || state.reports[0];
    state.selectedResourceId = preferred?.resource_id || null;
  }
  state.chartIndex = 0;
  updateNavigation();
  renderParsePage();
  renderSemanticPage();
  renderVisualPage();
  const url = new URL(window.location.href);
  url.searchParams.set("run_id", runId);
  window.history.replaceState({}, "", url);
}

async function loadExistingRun(runId) {
  window.clearTimeout(state.pollTimer);
  try {
    state.run = await api(`/api/v1/agent/runs/${encodeURIComponent(runId)}`);
    state.result = null;
    state.reports = [];
    state.selectedResourceId = null;
    showPage("parse");
    renderRunStatus();
    renderParsePage();
    if (state.run.status === "COMPLETED") {
      await loadResult(runId);
    } else if (state.run.status !== "FAILED") {
      schedulePoll(1000);
    }
  } catch (error) {
    toast("无法打开运行结果", error.message, "error");
  }
}

function renderRunStatus() {
  if (!state.run) return;
  const progress = Math.max(0, Math.min(1, Number(state.run.progress || 0)));
  elements.runStatus.textContent = statusLabel(state.run.status);
  elements.runStatus.dataset.status = state.run.status;
  elements.runStep.textContent = state.run.error || stepLabel(state.run.current_step);
  elements.runIdLabel.textContent = state.run.run_id;
  elements.runProgressText.textContent = `${Math.round(progress * 100)}%`;
  elements.progressBar.style.width = `${Math.round(progress * 100)}%`;
  elements.parseResourceTitle.textContent =
    state.run.status === "COMPLETED" ? "解析资源" : "资源进度";
}

function renderResourceList(container) {
  container.innerHTML = state.reports
    .map((report) => {
      const active = report.resource_id === state.selectedResourceId;
      const kind = report.classification?.data_kind || "resource";
      return `
        <button class="resource-item ${active ? "active" : ""}" type="button"
          data-resource-id="${esc(report.resource_id)}">
          <span class="resource-code">${kindCode(kind)}</span>
          <span class="resource-copy">
            <strong title="${esc(reportName(report))}">${esc(reportName(report))}</strong>
            <small>${esc(kindLabel(kind))} · ${formatNumber(reportRows(report))} 行</small>
          </span>
          <span class="resource-state" title="${esc(statusLabel(report.overall_status || "COMPLETED"))}"></span>
        </button>`;
    })
    .join("");
  container.querySelectorAll("[data-resource-id]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedResourceId = button.dataset.resourceId;
      state.chartIndex = 0;
      renderParsePage();
      renderSemanticPage();
      renderVisualPage();
    });
  });
}

function renderParsePage() {
  renderRunStatus();
  elements.parseResourceCount.textContent = String(state.reports.length);
  renderResourceList(elements.parseResourceList);
  const report = selectedReport();
  if (!report) {
    elements.parseEmpty.classList.remove("hidden");
    elements.parseDetailContent.innerHTML = "";
    elements.toSemanticButton.disabled = true;
    return;
  }
  elements.parseEmpty.classList.add("hidden");
  elements.toSemanticButton.disabled = false;
  renderParseDetail(report);
}

function renderParseDetail(report) {
  const kind = report.classification?.data_kind || "resource";
  const timeAxis = report.time_axis;
  const quality = report.quality || {};
  const columns = report.schema_profile?.columns || [];
  const warnings = report.processing?.warnings || report.recommendations || [];
  const overview = `
    <div class="detail-section">
      <h3>识别结论</h3>
      <p>${esc(report.classification?.reason || "未提供识别说明。")}</p>
    </div>
    ${
      timeAxis
        ? `
        <div class="detail-section">
          <h3>时间轴</h3>
          <div class="fact-grid">
            <div><span>时间字段</span><strong>${esc(timeAxis.timestamp_field)}</strong></div>
            <div><span>采样粒度</span><strong>${esc(timeAxis.granularity || "-")}</strong></div>
            <div><span>采样点</span><strong>${formatNumber(timeAxis.sample_count)}</strong></div>
            <div><span>开始时间</span><strong>${esc(formatDate(timeAxis.start_time))}</strong></div>
            <div><span>结束时间</span><strong>${esc(formatDate(timeAxis.end_time))}</strong></div>
            <div><span>不规则比例</span><strong>${formatPercent(timeAxis.irregular_ratio)}</strong></div>
          </div>
        </div>`
        : ""
    }
    <div class="detail-section">
      <h3>数据质量</h3>
      <div class="fact-grid">
        <div><span>完整率</span><strong>${formatPercent(quality.completeness)}</strong></div>
        <div><span>问题数量</span><strong>${formatNumber(quality.issue_count)}</strong></div>
        <div><span>实体字段</span><strong>${esc((report.entity_fields || []).join("、") || "-")}</strong></div>
        <div><span>指标字段</span><strong>${formatNumber(reportMetricCount(report))}</strong></div>
        <div><span>异常数量</span><strong>${formatNumber(reportAnomalyCount(report))}</strong></div>
        <div><span>数据布局</span><strong>${esc(report.statistics?.layout || "-")}</strong></div>
      </div>
      ${
        quality.issues?.length
          ? `<ul class="issue-list" style="margin-top:10px">${quality.issues
              .map((issue) => `<li class="issue-item">${esc(issue)}</li>`)
              .join("")}</ul>`
          : ""
      }
    </div>
    <div class="detail-section">
      <h3>字段结构</h3>
      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>字段</th><th>类型</th><th>语义类型</th><th>空值</th><th>主键</th></tr></thead>
          <tbody>
            ${
              columns.length
                ? columns
                    .map(
                      (column) => `
                        <tr>
                          <td>${esc(column.name)}</td>
                          <td>${esc(column.data_type || "-")}</td>
                          <td>${esc(column.semantic_type || "-")}</td>
                          <td>${column.nullable ? "可为空" : "非空"}</td>
                          <td>${column.is_primary_key ? "是" : "否"}</td>
                        </tr>`,
                    )
                    .join("")
                : '<tr><td colspan="5" class="muted-inline">没有字段画像。</td></tr>'
            }
          </tbody>
        </table>
      </div>
    </div>
    ${
      warnings.length
        ? `
        <div class="detail-section">
          <h3>处理提示</h3>
          <ul class="issue-list">${warnings
            .slice(0, 12)
            .map((item) => `<li class="issue-item">${esc(item)}</li>`)
            .join("")}</ul>
        </div>`
        : ""
    }`;
  const json = `<pre class="json-view">${esc(JSON.stringify(report, null, 2))}</pre>`;
  elements.parseDetailContent.innerHTML = `
    <div class="detail-identity">
      <div>
        <span class="eyebrow">${esc(kindLabel(kind))}</span>
        <h2>${esc(reportName(report))}</h2>
        <div class="detail-path">${esc(report.source?.relative_path || "-")}</div>
        <div class="tag-row">
          <span class="tag teal">${esc(kindLabel(kind))}</span>
          <span class="tag">${esc(report.source?.file_format || "-")}</span>
          <span class="tag">${esc(report.overall_status || "UNKNOWN")}</span>
        </div>
      </div>
      <span class="status-pill" data-status="COMPLETED">${esc(statusLabel(report.overall_status || "COMPLETED"))}</span>
    </div>
    <div class="metric-strip">
      <div class="metric-cell"><span>数据行数</span><strong>${formatNumber(reportRows(report))}</strong></div>
      <div class="metric-cell"><span>字段数量</span><strong>${formatNumber(report.schema_profile?.column_count || 0)}</strong></div>
      <div class="metric-cell"><span>指标数量</span><strong>${formatNumber(reportMetricCount(report))}</strong></div>
      <div class="metric-cell"><span>完整率</span><strong>${formatPercent(quality.completeness)}</strong></div>
    </div>
    <div class="detail-tabs">
      <button class="detail-tab ${state.parseTab === "overview" ? "active" : ""}" data-parse-tab="overview" type="button">资源详情</button>
      <button class="detail-tab ${state.parseTab === "json" ? "active" : ""}" data-parse-tab="json" type="button">统一 JSON</button>
    </div>
    <div id="parseTabContent">${state.parseTab === "json" ? json : overview}</div>`;
  elements.parseDetailContent.querySelectorAll("[data-parse-tab]").forEach((button) => {
    button.addEventListener("click", () => {
      state.parseTab = button.dataset.parseTab;
      renderParseDetail(report);
    });
  });
}

function renderSemanticPage() {
  elements.semanticResourceCount.textContent = String(state.reports.length);
  renderResourceList(elements.semanticResourceList);
  const report = selectedReport();
  if (!report) {
    elements.toVisualButton.disabled = true;
    elements.semanticDetailPanel.innerHTML = `
      <div class="empty-state">
        <strong>暂无语义结果</strong>
        <span>完成解析后，这里会显示模型生成的资源语义。</span>
      </div>`;
    return;
  }
  elements.toVisualButton.disabled = false;
  const attributes = report.attributes || [];
  const modelName =
    attributes.find((attribute) => attribute.model_version)?.model_version || "模型分析";
  elements.semanticModelSource.textContent = modelName;
  const recommendations = report.recommendations || [];
  elements.semanticDetailPanel.innerHTML = `
    <div class="semantic-summary">
      <span class="eyebrow">业务摘要</span>
      <h2>${esc(reportName(report))}</h2>
      <p>${esc(report.summary || "模型未提供摘要。")}</p>
      <div class="tag-row">
        <span class="status-pill" data-status="${esc(report.overall_status || "UNKNOWN")}">
          ${esc(statusLabel(report.overall_status || "UNKNOWN"))}
        </span>
        <span class="tag">${attributes.length} 个属性</span>
        <span class="tag">${recommendations.length} 条建议</span>
      </div>
    </div>
    <section class="semantic-section">
      <h3>结构化业务属性</h3>
      <div class="attribute-list">
        ${
          attributes.length
            ? attributes
                .map(
                  (attribute) => `
                    <article class="attribute-card">
                      <div class="attribute-head">
                        <div>
                          <span class="eyebrow">${esc(attribute.category || "语义属性")}</span>
                          <strong>${esc(attribute.attribute_name || "未命名属性")}</strong>
                        </div>
                        <span class="confidence-badge">${formatPercent(attribute.confidence)}</span>
                      </div>
                      <div class="attribute-value">${esc(formatAttributeValue(attribute.value))}</div>
                      ${
                        attribute.description
                          ? `<div class="attribute-description">${esc(attribute.description)}</div>`
                          : ""
                      }
                      ${
                        attribute.evidence?.length || attribute.source_refs?.length
                          ? `<div class="evidence-tags">${[
                              ...(attribute.evidence || []),
                              ...(attribute.source_refs || []),
                            ]
                              .slice(0, 8)
                              .map((item) => `<span>${esc(item)}</span>`)
                              .join("")}</div>`
                          : ""
                      }
                    </article>`,
                )
                .join("")
            : '<div class="empty-state"><strong>没有属性结果</strong><span>当前资源未生成结构化语义属性。</span></div>'
        }
      </div>
    </section>
    <section class="semantic-section">
      <h3>建议</h3>
      ${
        recommendations.length
          ? `<ul class="recommendation-list">${recommendations
              .map((item) => `<li class="recommendation-item">${esc(item)}</li>`)
              .join("")}</ul>`
          : '<p class="muted-inline">当前没有额外建议。</p>'
      }
    </section>`;
}

function renderVisualPage() {
  elements.visualResourceCount.textContent = `${state.reports.length} 个资源`;
  renderBatchTable();
  renderResourceList(elements.visualResourceList);
  renderChart();
}

function renderBatchTable() {
  const head = `
    <tr>
      <th>资源</th>
      <th>类型</th>
      <th>数据行数</th>
      <th>字段</th>
      <th>指标</th>
      <th>完整率</th>
      <th>异常</th>
      <th>状态</th>
    </tr>`;
  elements.batchTable.querySelector("thead").innerHTML = head;
  elements.batchTable.querySelector("tbody").innerHTML = state.reports
    .map((report) => {
      const active = report.resource_id === state.selectedResourceId;
      return `
        <tr class="${active ? "active" : ""}" data-resource-id="${esc(report.resource_id)}">
          <td><strong>${esc(reportName(report))}</strong><br><small class="muted-inline">${esc(report.source?.relative_path || "-")}</small></td>
          <td>${esc(kindLabel(report.classification?.data_kind))}</td>
          <td>${formatNumber(reportRows(report))}</td>
          <td>${formatNumber(report.schema_profile?.column_count || 0)}</td>
          <td>${formatNumber(reportMetricCount(report))}</td>
          <td>${formatPercent(report.quality?.completeness)}</td>
          <td>${formatNumber(reportAnomalyCount(report))}</td>
          <td><span class="status-pill" data-status="${esc(report.overall_status || "UNKNOWN")}">${esc(statusLabel(report.overall_status || "UNKNOWN"))}</span></td>
        </tr>`;
    })
    .join("");
  elements.batchTable.querySelectorAll("tbody [data-resource-id]").forEach((row) => {
    row.addEventListener("click", () => {
      state.selectedResourceId = row.dataset.resourceId;
      state.chartIndex = 0;
      renderVisualPage();
      renderResourceList(elements.parseResourceList);
      renderResourceList(elements.semanticResourceList);
    });
  });
}

function renderChart() {
  const report = selectedReport();
  const series = report?.statistics?.chart_series || [];
  elements.chartTitle.textContent = report ? reportName(report) : "等待选择资源";
  if (!report || !series.length) {
    elements.chartSeriesSelect.innerHTML = "";
    elements.chartSeriesSelect.disabled = true;
    elements.trendChart.innerHTML =
      '<div class="empty-state"><strong>暂无可绘制的时间序列</strong><span>结构化表或表结构资源可能不包含趋势曲线。</span></div>';
    elements.chartMeta.textContent = "-";
    return;
  }
  elements.chartIndex = Math.min(state.chartIndex, series.length - 1);
  elements.chartSeriesSelect.disabled = false;
  elements.chartSeriesSelect.innerHTML = series
    .map(
      (item, index) =>
        `<option value="${index}" ${index === state.chartIndex ? "selected" : ""}>${esc(item.label)}</option>`,
    )
    .join("");
  elements.chartSeriesSelect.onchange = () => {
    state.chartIndex = Number(elements.chartSeriesSelect.value || 0);
    drawTrendChart(series[state.chartIndex]);
  };
  drawTrendChart(series[state.chartIndex]);
}

function drawTrendChart(series) {
  const root = elements.trendChart;
  root.innerHTML = "";
  const data = (series?.points || [])
    .map((point) => ({
      time: new Date(point.timestamp),
      value: Number(point.value),
    }))
    .filter((point) => !Number.isNaN(point.time.getTime()) && Number.isFinite(point.value));
  if (!data.length || !window.d3) {
    root.innerHTML =
      '<div class="empty-state"><strong>当前序列没有有效数据点</strong><span>请选择其他指标或资源。</span></div>';
    elements.chartMeta.textContent = "-";
    return;
  }
  const width = Math.max(root.clientWidth, 520);
  const height = Math.max(root.clientHeight, 390);
  const margin = { top: 26, right: 24, bottom: 42, left: 66 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const svg = d3.select(root).append("svg").attr("viewBox", `0 0 ${width} ${height}`);
  const canvas = svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`);
  const x = d3
    .scaleUtc()
    .domain(d3.extent(data, (point) => point.time))
    .range([0, innerWidth]);
  const valueExtent = d3.extent(data, (point) => point.value);
  const spread = Math.max(valueExtent[1] - valueExtent[0], Math.abs(valueExtent[1] || 1) * 0.02, 1);
  const y = d3
    .scaleLinear()
    .domain([valueExtent[0] - spread * 0.08, valueExtent[1] + spread * 0.08])
    .nice()
    .range([innerHeight, 0]);
  const axisBottom = d3
    .axisBottom(x)
    .ticks(Math.max(3, Math.min(7, Math.floor(innerWidth / 120))))
    .tickFormat(d3.utcFormat("%m-%d %H:%M"));
  const axisLeft = d3.axisLeft(y).ticks(6).tickFormat(d3.format(".4~g"));
  canvas
    .append("g")
    .attr("class", "chart-grid")
    .call(d3.axisLeft(y).ticks(6).tickSize(-innerWidth).tickFormat(""));
  canvas
    .append("g")
    .attr("class", "chart-axis")
    .attr("transform", `translate(0,${innerHeight})`)
    .call(axisBottom);
  canvas.append("g").attr("class", "chart-axis").call(axisLeft);
  const gradientId = `trendGradient-${state.chartIndex}`;
  const defs = svg.append("defs");
  const gradient = defs
    .append("linearGradient")
    .attr("id", gradientId)
    .attr("x1", "0")
    .attr("y1", "0")
    .attr("x2", "0")
    .attr("y2", "1");
  gradient.append("stop").attr("offset", "0%").attr("stop-color", "#0f766e").attr("stop-opacity", 0.2);
  gradient.append("stop").attr("offset", "100%").attr("stop-color", "#0f766e").attr("stop-opacity", 0.01);
  const area = d3
    .area()
    .x((point) => x(point.time))
    .y0(innerHeight)
    .y1((point) => y(point.value));
  const line = d3
    .line()
    .x((point) => x(point.time))
    .y((point) => y(point.value));
  canvas
    .append("path")
    .datum(data)
    .attr("class", "chart-area")
    .attr("fill", `url(#${gradientId})`)
    .attr("d", area);
  canvas.append("path").datum(data).attr("class", "chart-line").attr("d", line);
  if (data.length <= 30) {
    canvas
      .append("g")
      .selectAll("circle")
      .data(data)
      .join("circle")
      .attr("class", "chart-point")
      .attr("cx", (point) => x(point.time))
      .attr("cy", (point) => y(point.value))
      .attr("r", 2.5);
  }
  const tooltip = document.createElement("div");
  tooltip.className = "chart-tooltip hidden";
  root.append(tooltip);
  const focus = canvas.append("g").style("display", "none");
  focus.append("circle").attr("r", 4).attr("fill", "#0f766e").attr("stroke", "white").attr("stroke-width", 2);
  const overlay = canvas
    .append("rect")
    .attr("width", innerWidth)
    .attr("height", innerHeight)
    .attr("fill", "transparent")
    .style("cursor", "crosshair");
  const bisect = d3.bisector((point) => point.time).center;
  overlay
    .on("mouseenter", () => {
      focus.style("display", null);
      tooltip.classList.remove("hidden");
    })
    .on("mouseleave", () => {
      focus.style("display", "none");
      tooltip.classList.add("hidden");
    })
    .on("mousemove", (event) => {
      const [pointerX] = d3.pointer(event);
      const targetTime = x.invert(pointerX);
      const index = bisect(data, targetTime);
      const point = data[index];
      focus.attr("transform", `translate(${x(point.time)},${y(point.value)})`);
      tooltip.innerHTML = `<strong>${esc(formatNumber(point.value))}</strong><br>${esc(
        formatDate(point.time.toISOString()),
      )}`;
      tooltip.style.left = `${Math.min(root.clientWidth - 170, x(point.time) + margin.left + 10)}px`;
      tooltip.style.top = `${Math.max(8, y(point.value) + margin.top - 20)}px`;
    });
  const first = data[0];
  const last = data[data.length - 1];
  elements.chartMeta.textContent = `${series.label} · ${data.length} 个采样点 · ${formatDate(
    first.time.toISOString(),
  )} 至 ${formatDate(last.time.toISOString())}`;
}

function downloadBlob(filename, content, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function safeDownloadName(value) {
  return String(value || "resource")
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .slice(0, 120);
}

function downloadCurrentReport() {
  const report = selectedReport();
  if (!report) {
    toast("没有资源", "请先选择一个资源。", "error");
    return;
  }
  downloadBlob(
    `${safeDownloadName(reportName(report))}.json`,
    JSON.stringify(report, null, 2),
    "application/json;charset=utf-8",
  );
}

function downloadAllResults() {
  if (!state.result) return;
  downloadBlob(
    `${safeDownloadName(state.run?.run_id || "agent-result")}.json`,
    JSON.stringify(state.result, null, 2),
    "application/json;charset=utf-8",
  );
}

function csvCell(value) {
  const text = String(value ?? "");
  return `"${text.replaceAll('"', '""')}"`;
}

function downloadSummaryCsv() {
  if (!state.reports.length) {
    toast("没有结果", "当前没有可导出的资源。", "error");
    return;
  }
  const rows = [
    ["资源名称", "相对路径", "资源类型", "数据行数", "字段数", "指标数", "完整率", "异常数", "状态"],
    ...state.reports.map((report) => [
      reportName(report),
      report.source?.relative_path || "",
      kindLabel(report.classification?.data_kind),
      reportRows(report),
      report.schema_profile?.column_count || 0,
      reportMetricCount(report),
      formatPercent(report.quality?.completeness),
      reportAnomalyCount(report),
      statusLabel(report.overall_status || "UNKNOWN"),
    ]),
  ];
  const csv = `\ufeff${rows.map((row) => row.map(csvCell).join(",")).join("\r\n")}`;
  downloadBlob(
    `${safeDownloadName(state.run?.run_id || "resource-summary")}.csv`,
    csv,
    "text/csv;charset=utf-8",
  );
}

function renderHistory() {
  const pages = Math.max(1, Math.ceil(state.history.length / HISTORY_PAGE_SIZE));
  state.historyPage = Math.min(state.historyPage, pages - 1);
  const start = state.historyPage * HISTORY_PAGE_SIZE;
  const pageItems = state.history.slice(start, start + HISTORY_PAGE_SIZE);
  elements.historyPageLabel.textContent = `第 ${state.historyPage + 1} / ${pages} 页`;
  elements.historyPrevButton.disabled = state.historyPage <= 0;
  elements.historyNextButton.disabled = state.historyPage >= pages - 1;
  if (!pageItems.length) {
    elements.historyList.innerHTML =
      '<div class="empty-state"><strong>暂无本地解析记录</strong><span>完成一次解析后，记录会显示在这里。</span></div>';
    return;
  }
  elements.historyList.innerHTML = pageItems
    .map(
      (item) => `
        <article class="history-card" data-status="${esc(item.status)}">
          <div class="history-main">
            <strong title="${esc(item.label)}">${esc(item.label)}</strong>
            <small>${esc(formatDate(item.created_at))} · ${esc(item.run_id)}</small>
          </div>
          <div class="history-facts">
            <span class="mini-badge">${esc(item.input_file_count)} 个输入</span>
            <span class="mini-badge">${esc(item.report_count)} 个结果</span>
            <span class="status-pill" data-status="${esc(item.status)}">${esc(statusLabel(item.status))}</span>
          </div>
          <div class="history-actions">
            <button class="button ghost compact" type="button" data-open-run="${esc(item.run_id)}" ${
              item.status === "COMPLETED" ? "" : "disabled"
            }>查看</button>
            <button class="button ghost compact" type="button" data-hide-run="${esc(item.run_id)}">隐藏</button>
          </div>
        </article>`,
    )
    .join("");
  elements.historyList.querySelectorAll("[data-open-run]").forEach((button) => {
    button.addEventListener("click", () => loadExistingRun(button.dataset.openRun));
  });
  elements.historyList.querySelectorAll("[data-hide-run]").forEach((button) => {
    button.addEventListener("click", async () => {
      try {
        await api(`/api/v1/agent/runs/${encodeURIComponent(button.dataset.hideRun)}/hide`, {
          method: "POST",
        });
        await loadHistory();
        toast("已隐藏", "原始文件和解析结果未删除。", "success");
      } catch (error) {
        toast("隐藏失败", error.message, "error");
      }
    });
  });
}

async function loadHistory() {
  try {
    state.history = await api("/api/v1/agent/runs?limit=40");
    const pages = Math.max(1, Math.ceil(state.history.length / HISTORY_PAGE_SIZE));
    if (state.historyPage >= pages) state.historyPage = pages - 1;
    renderHistory();
  } catch (error) {
    elements.historyList.innerHTML = `
      <div class="empty-state">
        <strong>无法读取历史记录</strong>
        <span>${esc(error.message)}</span>
      </div>`;
  }
}

function updateModelUi() {
  const settings = state.modelSettings || {};
  const configured = Boolean(settings.configured);
  elements.modelButtonStatus.textContent = configured
    ? settings.source === "environment"
      ? "环境变量"
      : "今日可用"
    : "未配置";
  elements.modelStatePanel.dataset.state = configured ? "ok" : "error";
  elements.modelStateText.textContent = configured
    ? `${settings.model || "模型"} · ${
        settings.source === "environment" ? "环境变量" : "当日配置"
      }`
    : "未配置，解析可能失败";
  elements.settingsBaseUrl.value = settings.base_url || "";
  elements.settingsModel.value = settings.model || "";
  elements.settingsApiKey.value = "";
  elements.settingsApiKey.placeholder = configured
    ? `已配置 ${settings.key_masked || "********"}，留空沿用`
    : "请输入 API 密钥";
  if (configured) {
    elements.settingsStatus.dataset.state = "ok";
    elements.settingsStatus.textContent =
      settings.source === "environment"
        ? "当前使用环境变量中的模型配置。"
        : `当前模型配置今日有效${settings.expires_on ? `，有效期至 ${settings.expires_on}` : ""}。`;
  } else {
    elements.settingsStatus.dataset.state = "error";
    elements.settingsStatus.textContent = "尚未配置可用模型，解析任务会因缺少语义模型而失败。";
  }
}

async function loadModelSettings(openIfMissing = false) {
  try {
    state.modelSettings = await api("/api/v1/settings/model");
    updateModelUi();
    if (openIfMissing && !state.modelSettings.configured && !state.settingsPrompted) {
      state.settingsPrompted = true;
      window.setTimeout(openSettings, 350);
    }
  } catch (error) {
    elements.modelStatePanel.dataset.state = "error";
    elements.modelStateText.textContent = "无法读取模型配置";
    elements.modelButtonStatus.textContent = "读取失败";
    elements.settingsStatus.dataset.state = "error";
    elements.settingsStatus.textContent = error.message;
  }
}

function openSettings() {
  elements.settingsModal.classList.remove("hidden");
  window.setTimeout(() => elements.settingsApiKey.focus(), 30);
}

function closeSettings() {
  elements.settingsModal.classList.add("hidden");
}

function modelRequestPayload() {
  return {
    base_url: elements.settingsBaseUrl.value.trim(),
    model: elements.settingsModel.value.trim(),
    api_key: elements.settingsApiKey.value.trim() || null,
  };
}

async function testSettings() {
  const request = modelRequestPayload();
  elements.testSettingsButton.disabled = true;
  try {
    const response = await api("/api/v1/settings/model/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    elements.settingsStatus.dataset.state = "ok";
    elements.settingsStatus.textContent = response.message;
    toast("连接成功", response.message, "success");
  } catch (error) {
    elements.settingsStatus.dataset.state = "error";
    elements.settingsStatus.textContent = error.message;
    toast("连接失败", error.message, "error");
  } finally {
    elements.testSettingsButton.disabled = false;
  }
}

async function saveSettings() {
  const request = modelRequestPayload();
  elements.saveSettingsButton.disabled = true;
  try {
    state.modelSettings = await api("/api/v1/settings/model/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    });
    updateModelUi();
    toast("配置已保存", "当天解析任务将使用该模型配置。", "success");
    window.setTimeout(closeSettings, 250);
  } catch (error) {
    elements.settingsStatus.dataset.state = "error";
    elements.settingsStatus.textContent = error.message;
    toast("保存失败", error.message, "error");
  } finally {
    elements.saveSettingsButton.disabled = false;
  }
}

async function checkHealth() {
  try {
    await api("/health");
    elements.healthBadge.dataset.state = "ok";
    elements.healthText.textContent = "服务正常";
  } catch {
    elements.healthBadge.dataset.state = "error";
    elements.healthText.textContent = "服务不可用";
  }
}

function bindEvents() {
  document.querySelectorAll(".nav-step").forEach((button) => {
    button.addEventListener("click", () => showPage(button.dataset.page));
  });
  document.querySelectorAll("[data-page]").forEach((button) => {
    if (button.classList.contains("nav-step")) return;
    button.addEventListener("click", () => showPage(button.dataset.page));
  });
  document.querySelectorAll(".source-mode").forEach((button) => {
    button.addEventListener("click", () => switchSource(button.dataset.source));
  });
  elements.agentFiles.addEventListener("change", renderFolderFiles);
  elements.chooseFolderButton.addEventListener("click", () => elements.agentFiles.click());
  elements.connectDatabaseButton.addEventListener("click", connectDatabase);
  elements.startButton.addEventListener("click", startRun);
  elements.refreshHistoryButton.addEventListener("click", loadHistory);
  elements.historyPrevButton.addEventListener("click", () => {
    if (state.historyPage > 0) {
      state.historyPage -= 1;
      renderHistory();
    }
  });
  elements.historyNextButton.addEventListener("click", () => {
    const pages = Math.ceil(state.history.length / HISTORY_PAGE_SIZE);
    if (state.historyPage < pages - 1) {
      state.historyPage += 1;
      renderHistory();
    }
  });
  elements.openSettingsButton.addEventListener("click", openSettings);
  elements.closeSettingsButton.addEventListener("click", closeSettings);
  elements.settingsModal.addEventListener("click", (event) => {
    if (event.target === elements.settingsModal) closeSettings();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeSettings();
  });
  elements.testSettingsButton.addEventListener("click", testSettings);
  elements.saveSettingsButton.addEventListener("click", saveSettings);
  elements.toSemanticButton.addEventListener("click", () => showPage("semantic"));
  elements.toVisualButton.addEventListener("click", () => showPage("visual"));
  elements.downloadCurrentButton.addEventListener("click", downloadCurrentReport);
  elements.downloadAllButton.addEventListener("click", downloadAllResults);
  elements.downloadSummaryButton.addEventListener("click", downloadSummaryCsv);
  window.addEventListener("resize", () => {
    if (state.page === "visual") renderChart();
  });
}

async function bootstrap() {
  bindEvents();
  renderFolderFiles();
  updateNavigation();
  checkHealth();
  loadModelSettings(true);
  loadHistory();
  const restoredRunId = new URLSearchParams(window.location.search).get("run_id");
  if (restoredRunId) {
    await loadExistingRun(restoredRunId);
  }
}

bootstrap();
