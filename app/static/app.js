const state = {
  batch: null,
  agent: null,
  events: [],
  results: [],
  sqlFiles: [],
  dataFiles: [],
  knownDataFiles: [],
  descriptions: {},
  fieldDescriptions: {},
  mappings: {},
  selectedCode: null,
  mode: "agent",
  pollTimer: null,
  polling: false,
};

const statusLabels = {
  UPLOADED: "已上传",
  PREVIEW_READY: "待配置",
  PENDING: "等待中",
  PARSING: "解析中",
  RUNNING: "运行中",
  QUEUED: "排队中",
  REVIEW_REQUIRED: "待确认",
  COMPLETED: "已完成",
  FAILED: "失败",
  CONFIRMED: "已确认",
};

const eventLabels = {
  AGENT_STARTED: "Agent 启动",
  PROVIDER_SELECTED: "提供方选择",
  RESOURCE_STARTED: "资源开始解析",
  RESOURCE_COMPLETED: "资源解析完成",
  RESOURCE_FAILED: "资源解析失败",
  AGENT_FINISHED: "Agent 完成",
  AGENT_FAILED: "Agent 失败",
};

const reviewLabels = {
  SQL_PARSE_WARNING: "SQL 解析警告",
  DATA_FILE_UNMATCHED: "数据未配对",
  PROFILE_MISSING_COLUMNS: "字段缺失",
  PROFILE_UNMATCHED_COLUMNS: "多余数据字段",
  SMALL_SAMPLE: "样本量较小",
  FIELD_SEMANTICS_PENDING: "字段待确认",
  RESOURCE_FAILED: "资源解析失败",
};

const elements = {
  healthBadge: document.querySelector("#healthBadge"),
  healthText: document.querySelector("#healthText"),
  batchLookup: document.querySelector("#batchLookup"),
  loadBatchButton: document.querySelector("#loadBatchButton"),
  uploadSection: document.querySelector("#uploadSection"),
  toggleUploadButton: document.querySelector("#toggleUploadButton"),
  sqlDialect: document.querySelector("#sqlDialect"),
  sqlFiles: document.querySelector("#sqlFiles"),
  dataFiles: document.querySelector("#dataFiles"),
  sqlDropzone: document.querySelector("#sqlDropzone"),
  dataDropzone: document.querySelector("#dataDropzone"),
  sqlPickButton: document.querySelector("#sqlPickButton"),
  dataPickButton: document.querySelector("#dataPickButton"),
  sqlFileList: document.querySelector("#sqlFileList"),
  dataFileList: document.querySelector("#dataFileList"),
  uploadHint: document.querySelector("#uploadHint"),
  uploadButton: document.querySelector("#uploadButton"),
  workspace: document.querySelector("#workspace"),
  batchIdText: document.querySelector("#batchIdText"),
  copyBatchButton: document.querySelector("#copyBatchButton"),
  batchStatusBadge: document.querySelector("#batchStatusBadge"),
  batchProgressLabel: document.querySelector("#batchProgressLabel"),
  resultCount: document.querySelector("#resultCount"),
  agentAttentionDot: document.querySelector("#agentAttentionDot"),
  modeSelector: document.querySelector("#modeSelector"),
  requireApiRow: document.querySelector("#requireApiRow"),
  requireApiCheckbox: document.querySelector("#requireApiCheckbox"),
  startRunButton: document.querySelector("#startRunButton"),
  progressBar: document.querySelector("#progressBar"),
  resourceSummary: document.querySelector("#resourceSummary"),
  resourceList: document.querySelector("#resourceList"),
  configEmpty: document.querySelector("#configEmpty"),
  configContent: document.querySelector("#configContent"),
  selectedResourceCode: document.querySelector("#selectedResourceCode"),
  selectedResourceName: document.querySelector("#selectedResourceName"),
  pairingPill: document.querySelector("#pairingPill"),
  pairingSelect: document.querySelector("#pairingSelect"),
  resourceDescription: document.querySelector("#resourceDescription"),
  fieldCountText: document.querySelector("#fieldCountText"),
  expandFieldsButton: document.querySelector("#expandFieldsButton"),
  fieldEditorList: document.querySelector("#fieldEditorList"),
  resultsEmpty: document.querySelector("#resultsEmpty"),
  resultsContent: document.querySelector("#resultsContent"),
  reviewCount: document.querySelector("#reviewCount"),
  reviewList: document.querySelector("#reviewList"),
  eventCount: document.querySelector("#eventCount"),
  eventTimeline: document.querySelector("#eventTimeline"),
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

function attr(value) {
  return esc(value).replaceAll("`", "&#096;");
}

function labelForStatus(status, kind = "") {
  if (status === "PENDING" && kind === "semantic") return "待确认";
  return statusLabels[status] || status || "-";
}

function formatTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return "-";
  }
  return Number(value).toLocaleString("zh-CN", {
    maximumFractionDigits: digits,
  });
}

function formatRate(value) {
  if (value === null || value === undefined) return "-";
  return `${(Number(value) * 100).toFixed(1)}%`;
}

function toast(title, message, kind = "info") {
  const node = document.createElement("div");
  node.className = "toast";
  node.dataset.kind = kind;
  node.innerHTML = `<strong>${esc(title)}</strong><span>${esc(message)}</span>`;
  elements.toastRegion.append(node);
  window.setTimeout(() => node.remove(), 5000);
}

function sameFile(a, b) {
  return a.name === b.name && a.size === b.size && a.lastModified === b.lastModified;
}

function setFiles(kind, files) {
  const current = kind === "sql" ? state.sqlFiles : state.dataFiles;
  const merged = [...current];
  for (const file of Array.from(files)) {
    if (!merged.some((item) => sameFile(item, file))) merged.push(file);
  }
  if (kind === "sql") state.sqlFiles = merged;
  else state.dataFiles = merged;
  renderFileLists();
}

function removeFile(kind, index) {
  if (kind === "sql") state.sqlFiles.splice(index, 1);
  else state.dataFiles.splice(index, 1);
  renderFileLists();
}

function renderFileLists() {
  const render = (target, files, kind) => {
    target.innerHTML = files
      .map(
        (file, index) => `
          <li class="file-chip">
            <span title="${attr(file.name)}">${esc(file.name)}</span>
            <button type="button" aria-label="移除 ${attr(file.name)}" data-file-kind="${kind}" data-file-index="${index}">×</button>
          </li>`,
      )
      .join("");
  };
  render(elements.sqlFileList, state.sqlFiles, "sql");
  render(elements.dataFileList, state.dataFiles, "data");
  const total = state.sqlFiles.length + state.dataFiles.length;
  elements.uploadHint.textContent = total
    ? `${state.sqlFiles.length} 个 SQL，${state.dataFiles.length} 个数据文件`
    : "等待选择文件";
}

function wireDropzone(dropzone, input, kind) {
  dropzone.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    input.click();
  });
  dropzone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropzone.classList.add("dragging");
  });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragging"));
  dropzone.addEventListener("drop", (event) => {
    event.preventDefault();
    dropzone.classList.remove("dragging");
    const files = Array.from(event.dataTransfer.files).filter((file) => {
      if (kind === "sql") return file.name.toLowerCase().endsWith(".sql");
      return true;
    });
    if (files.length) setFiles(kind, files);
  });
}

async function uploadBatch() {
  if (!state.sqlFiles.length) {
    toast("缺少 SQL 文件", "至少选择一个 .sql 文件。", "error");
    return;
  }
  if (!state.dataFiles.length) {
    toast("缺少数据文件", "至少选择一个数据文件。", "error");
    return;
  }

  const form = new FormData();
  form.append("sql_dialect", elements.sqlDialect.value);
  for (const file of state.sqlFiles) form.append("sql_files", file, file.name);
  for (const file of state.dataFiles) form.append("data_files", file, file.name);

  elements.uploadButton.disabled = true;
  elements.uploadHint.textContent = "正在上传并解析 SQL...";
  try {
    const created = await api("/api/v1/batches", { method: "POST", body: form });
    applyCreatedBatch(created);
    toast("批次已创建", "SQL 预览与文件配对已生成。", "success");
    const issueCount =
      (created.unmatched_sql_files?.length || 0) +
      (created.unmatched_data_files?.length || 0) +
      (created.resources || []).filter((item) => item.parse_error).length;
    if (issueCount) {
      toast("存在配对或解析问题", `发现 ${issueCount} 项，请在配置区检查。`, "error");
    }
  } catch (error) {
    toast("上传失败", error.message, "error");
  } finally {
    elements.uploadButton.disabled = false;
    renderFileLists();
  }
}

function applyCreatedBatch(created) {
  state.batch = created;
  state.agent = null;
  state.events = [];
  state.results = [];
  state.selectedCode = created.resources?.[0]?.resource_code || null;
  state.knownDataFiles = [
    ...new Set([
      ...(created.resources || []).map((item) => item.data_file).filter(Boolean),
      ...(created.unmatched_data_files || []),
    ]),
  ];
  state.mappings = {};
  state.descriptions = {};
  state.fieldDescriptions = {};
  elements.batchLookup.value = created.batch_id;
  elements.workspace.classList.remove("hidden");
  elements.toggleUploadButton.classList.remove("hidden");
  elements.uploadSection.classList.add("collapsed");
  elements.toggleUploadButton.textContent = "展开";
  switchTab("config");
  render();
}

async function loadBatch() {
  const batchId = elements.batchLookup.value.trim();
  if (!batchId) {
    toast("缺少批次编号", "请输入 batch_id。", "error");
    return;
  }

  elements.loadBatchButton.disabled = true;
  try {
    const batch = await api(`/api/v1/batches/${encodeURIComponent(batchId)}`);
    state.batch = batch;
    state.selectedCode = batch.resources?.[0]?.resource_code || null;
    state.knownDataFiles = [
      ...new Set((batch.resources || []).map((item) => item.data_file).filter(Boolean)),
    ];
    state.mappings = {};
    state.descriptions = {};
    state.fieldDescriptions = {};
    state.results = [];
    state.agent = null;
    state.events = [];

    try {
      state.results = (
        await api(`/api/v1/batches/${encodeURIComponent(batchId)}/results`)
      ).results || [];
    } catch {
      state.results = [];
    }
    try {
      state.agent = await api(
        `/api/v1/batches/${encodeURIComponent(batchId)}/agent-runs/latest`,
      );
      state.mode = "agent";
      state.events = (
        await api(
          `/api/v1/batches/${encodeURIComponent(batchId)}/agent-runs/latest/events`,
        )
      ).events || [];
    } catch {
      state.agent = null;
    }

    elements.workspace.classList.remove("hidden");
    elements.toggleUploadButton.classList.remove("hidden");
    elements.uploadSection.classList.add("collapsed");
    elements.toggleUploadButton.textContent = "展开";
    render();
    if (isRunning()) schedulePoll(0);
  } catch (error) {
    toast("载入失败", error.message, "error");
  } finally {
    elements.loadBatchButton.disabled = false;
  }
}

function isRunning() {
  const batchStatus = state.batch?.status;
  const agentStatus = state.agent?.status;
  return (
    ["UPLOADED", "PARSING"].includes(batchStatus) ||
    ["QUEUED", "RUNNING"].includes(agentStatus)
  );
}

function isTerminal() {
  if (state.agent) {
    return ["COMPLETED", "REVIEW_REQUIRED", "FAILED"].includes(state.agent.status);
  }
  return ["COMPLETED", "FAILED"].includes(state.batch?.status);
}

function render() {
  if (!state.batch) return;
  renderBatchHeader();
  renderResourceList();
  renderConfig();
  renderAgent();
  renderResults();
}

function renderBatchHeader() {
  const batch = state.batch;
  elements.batchIdText.textContent = batch.batch_id;
  elements.batchStatusBadge.textContent = labelForStatus(
    state.agent?.status || batch.status,
  );
  elements.batchStatusBadge.dataset.status = state.agent?.status || batch.status;
  const completed = batch.completed_resources || 0;
  const total = batch.total_resources || batch.resources?.length || 0;
  elements.batchProgressLabel.textContent = `${completed} / ${total}`;
  const progress = state.agent?.progress ?? (total ? completed / total : 0);
  elements.progressBar.style.width = `${Math.round((progress || 0) * 100)}%`;
}

function resourceStatus(resource) {
  return resource.status || "PENDING";
}

function renderResourceList() {
  const resources = state.batch.resources || [];
  elements.resourceSummary.textContent = String(resources.length);
  elements.resourceList.innerHTML = resources
    .map((resource) => {
      const status = resourceStatus(resource);
      const active = resource.resource_code === state.selectedCode ? " active" : "";
      return `
        <button class="resource-item${active}" type="button" data-resource-code="${attr(
          resource.resource_code,
        )}">
          <span class="resource-state" data-status="${attr(status)}"></span>
          <span class="resource-item-text">
            <strong>${esc(resource.resource_code)}</strong>
            <span>${esc(resource.data_file || "未配对数据文件")}</span>
          </span>
          <small>${esc(labelForStatus(status))}</small>
        </button>`;
    })
    .join("");
}

function selectedResource() {
  return (state.batch?.resources || []).find(
    (item) => item.resource_code === state.selectedCode,
  );
}

function fieldNamesFor(resource) {
  if (resource.output_fields?.length) return resource.output_fields;
  const result = state.results.find(
    (item) => item.resource_code === resource.resource_code,
  );
  return result?.fields?.map((field) => field.name) || [];
}

function renderConfig() {
  const resource = selectedResource();
  if (!resource) {
    elements.configEmpty.classList.remove("hidden");
    elements.configContent.classList.add("hidden");
    return;
  }
  elements.configEmpty.classList.add("hidden");
  elements.configContent.classList.remove("hidden");

  elements.selectedResourceCode.textContent = resource.resource_code;
  elements.selectedResourceName.textContent = resource.sql_file;
  const matchedBy = resource.matched_by || (resource.data_file ? "auto" : "none");
  elements.pairingPill.textContent =
    matchedBy === "manual"
      ? "人工配对"
      : matchedBy === "auto"
        ? "自动配对"
        : "未配对";
  elements.pairingPill.dataset.matched = matchedBy;

  const dataFiles = [
    ...new Set([
      ...state.knownDataFiles,
      ...[resource.data_file].filter(Boolean),
    ]),
  ];
  elements.pairingSelect.innerHTML = [
    `<option value="">不配对数据文件</option>`,
    ...dataFiles.map(
      (name) =>
        `<option value="${attr(name)}" ${
          name === resource.data_file ? "selected" : ""
        }>${esc(name)}</option>`,
    ),
  ].join("");

  const key = resource.sql_file || resource.resource_code;
  elements.resourceDescription.value =
    state.descriptions[key] || state.descriptions[resource.resource_code] || "";

  const fieldNames = fieldNamesFor(resource);
  elements.fieldCountText.textContent = `${fieldNames.length} 个输出字段`;
  const descriptions = state.fieldDescriptions[key] || {};
  elements.fieldEditorList.innerHTML = fieldNames.length
    ? fieldNames
        .map((fieldName) => {
          const resultField = state.results
            .find((item) => item.resource_code === resource.resource_code)
            ?.fields?.find((field) => field.name === fieldName);
          const expression = resultField?.source_expression || "SQL 输出字段";
          const value = descriptions[fieldName] || resultField?.description || "";
          return `
            <details class="field-editor">
              <summary>
                <span class="field-name-cell">
                  <strong>${esc(fieldName)}</strong>
                  <small>${esc(resultField?.display_name || "待语义补全")}</small>
                </span>
                <span class="field-expression" title="${attr(expression)}">${esc(
                  expression,
                )}</span>
                <span class="semantic-pill" data-status="${attr(
                  resultField?.semantic_status || "PENDING",
                )}">${esc(
                  labelForStatus(
                    resultField?.semantic_status || "PENDING",
                    "semantic",
                  ),
                )}</span>
              </summary>
              <div class="field-editor-body">
                <label>
                  字段业务说明
                  <textarea rows="2" data-field-name="${attr(
                    fieldName,
                  )}" placeholder="填写字段含义、口径或单位">${esc(value)}</textarea>
                </label>
              </div>
            </details>`;
        })
        .join("")
    : `<div class="empty-state small"><strong>没有字段预览</strong><span>SQL 可能解析失败，或结果尚未生成。</span></div>`;
}

function renderAgent() {
  const run = state.agent;
  const reviews = run?.review_items || [];
  const events = state.events || [];
  elements.reviewCount.textContent = String(reviews.length);
  elements.eventCount.textContent = String(events.length);
  elements.agentAttentionDot.classList.toggle(
    "hidden",
    !reviews.length && run?.status !== "REVIEW_REQUIRED",
  );

  elements.reviewList.innerHTML = reviews.length
    ? reviews
        .map(
          (item) => `
            <article class="review-item" data-severity="${attr(item.severity)}">
              <div class="review-item-head">
                <span class="severity-pill" data-severity="${attr(item.severity)}">${esc(
                  item.severity,
                )}</span>
                <span class="inline-note">${esc(
                  reviewLabels[item.code] || item.code,
                )}</span>
              </div>
              <p>${esc(item.message)}</p>
              <div class="review-meta">
                ${item.resource_code ? `<span>资源：${esc(item.resource_code)}</span>` : ""}
                ${item.field_name ? `<span>字段：${esc(item.field_name)}</span>` : ""}
              </div>
            </article>`,
        )
        .join("")
    : `<div class="empty-state small"><strong>暂无待确认项</strong><span>Agent 运行后显示审查结果。</span></div>`;

  elements.eventTimeline.innerHTML = events.length
    ? events
        .map(
          (event) => `
            <article class="timeline-item" data-event-type="${attr(event.event_type)}">
              <span class="timeline-marker" aria-hidden="true"></span>
              <div class="timeline-copy">
                <strong>${esc(eventLabels[event.event_type] || event.event_type)}</strong>
                <p>${esc(event.message)}</p>
                <time>${esc(formatTime(event.timestamp))}</time>
              </div>
            </article>`,
        )
        .join("")
    : `<div class="empty-state small"><strong>暂无执行记录</strong><span>增强 Agent 运行时记录关键步骤。</span></div>`;
}

function renderResults() {
  const results = state.results || [];
  elements.resultCount.textContent = String(results.length);
  elements.resultsEmpty.classList.toggle("hidden", results.length > 0);
  elements.resultsContent.innerHTML = results
    .map((result) => renderResourceResult(result))
    .join("");
}

function renderResourceResult(result) {
  const profile = result.profile || {};
  const summary = result.sql_summary || {};
  return `
    <article class="result-resource">
      <header class="result-resource-head">
        <div class="result-title-row">
          <div>
            <h3>${esc(result.name || result.resource_code)}</h3>
            <p>${esc(result.description || "暂无资源描述")}</p>
          </div>
          <span class="status-pill" data-status="COMPLETED">${esc(
            result.semantic_provider || "unknown",
          )}</span>
        </div>
        <div class="tag-row">
          ${(result.tags || [])
            .map((tag) => `<span class="tag">${esc(tag)}</span>`)
            .join("")}
        </div>
      </header>

      <div class="result-stats">
        <div class="result-stat">
          <span>资源编号</span>
          <strong>${esc(result.resource_code)}</strong>
        </div>
        <div class="result-stat">
          <span>数据行数</span>
          <strong>${formatNumber(profile.row_count, 0)}</strong>
        </div>
        <div class="result-stat">
          <span>输出字段</span>
          <strong>${result.fields?.length || 0}</strong>
        </div>
        <div class="result-stat">
          <span>数据文件</span>
          <strong title="${attr(profile.data_file || "")}">${esc(
            profile.data_file || "未配对",
          )}</strong>
        </div>
      </div>

      <div class="sql-summary">
        <div class="summary-item">
          <span>方言</span>
          <strong>${esc(summary.dialect || "-")}</strong>
        </div>
        <div class="summary-item">
          <span>来源表</span>
          <strong>${esc((result.source_tables || []).join(", ") || "-")}</strong>
        </div>
        <div class="summary-item">
          <span>聚合</span>
          <strong>${summary.has_aggregation ? "是" : "否"}</strong>
        </div>
        <div class="summary-item">
          <span>JOIN</span>
          <strong>${summary.joins?.length || 0}</strong>
        </div>
        <div class="summary-item">
          <span>WHERE / HAVING</span>
          <strong>${summary.filters?.length || 0}</strong>
        </div>
        <div class="summary-item">
          <span>CTE</span>
          <strong>${esc((summary.ctes || []).join(", ") || "-")}</strong>
        </div>
        <div class="summary-item">
          <span>聚合函数</span>
          <strong>${esc((summary.aggregate_functions || []).join(", ") || "-")}</strong>
        </div>
        <div class="summary-item">
          <span>生成时间</span>
          <strong>${esc(formatTime(result.generated_at))}</strong>
        </div>
      </div>

      <div class="fields-list">
        ${(result.fields || []).map((field) => renderFieldResult(field)).join("")}
      </div>
    </article>`;
}

function renderFieldResult(field) {
  const stats = field.statistics || {};
  return `
    <article class="field-result">
      <div class="field-result-main">
        <div class="field-result-name">
          <strong>${esc(field.name)}</strong>
          <span>${esc(field.display_name || "未补全")}</span>
        </div>
        <div class="field-result-meta">
          <strong>${esc(field.business_type || field.data_type || "-")}</strong>
          <span>${esc(field.data_type || "-")}${
            field.unit ? ` · ${esc(field.unit)}` : ""
          }</span>
        </div>
        <div>
          <div class="field-description">${esc(
            field.description || "暂无字段说明",
          )}</div>
          <div class="tag-row">
            <span class="semantic-pill" data-status="${attr(
              field.semantic_status || "CONFIRMED",
            )}">${esc(
              labelForStatus(field.semantic_status || "CONFIRMED", "semantic"),
            )}</span>
            ${(field.tags || [])
              .map((tag) => `<span class="tag">${esc(tag)}</span>`)
              .join("")}
          </div>
        </div>
      </div>
      <div class="field-evidence">
        <div class="evidence-item">
          <span>来源表达式</span>
          <strong title="${attr(field.source_expression || "")}">${esc(
            field.source_expression || "-",
          )}</strong>
        </div>
        <div class="evidence-item">
          <span>空值率</span>
          <strong>${formatRate(stats.null_rate)}</strong>
        </div>
        <div class="evidence-item">
          <span>唯一率</span>
          <strong>${formatRate(stats.distinct_rate)}</strong>
        </div>
        <div class="evidence-item">
          <span>脱敏样例</span>
          <strong title="${attr((field.examples || []).join(", "))}">${esc(
            (field.examples || []).slice(0, 3).join(", ") || "-",
          )}</strong>
        </div>
      </div>
    </article>`;
}

function switchTab(tabName) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `tab-${tabName}`);
  });
}

function persistCurrentResource() {
  const resource = selectedResource();
  if (!resource) return;
  const key = resource.sql_file || resource.resource_code;
  state.descriptions[key] = elements.resourceDescription.value;
  state.fieldDescriptions[key] ||= {};
  elements.fieldEditorList
    .querySelectorAll("textarea[data-field-name]")
    .forEach((input) => {
      state.fieldDescriptions[key][input.dataset.fieldName] = input.value;
    });
}

async function startRun() {
  const batch = state.batch;
  const resource = selectedResource();
  if (!batch || !resource) return;

  persistCurrentResource();
  const resourceDescriptions = {};
  const fieldDescriptions = {};
  for (const item of batch.resources || []) {
    const key = item.sql_file || item.resource_code;
    const description = (state.descriptions[key] || "").trim();
    if (description) resourceDescriptions[key] = description;
    const fields = state.fieldDescriptions[key] || {};
    const cleaned = Object.fromEntries(
      Object.entries(fields).filter(([, value]) => String(value || "").trim()),
    );
    if (Object.keys(cleaned).length) fieldDescriptions[key] = cleaned;
  }

  const body = {
    resource_descriptions: resourceDescriptions,
    field_descriptions: fieldDescriptions,
  };
  if (Object.keys(state.mappings).length) body.mappings = state.mappings;

  try {
    elements.startRunButton.disabled = true;
    elements.startRunButton.textContent = "正在启动...";
    if (state.mode === "agent") {
      body.require_api = elements.requireApiCheckbox.checked;
      state.agent = await api(
        `/api/v1/batches/${encodeURIComponent(batch.batch_id)}/agent-runs`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      state.events = [];
      toast("Agent 已启动", "解析任务正在后台运行。", "success");
    } else {
      state.batch = await api(
        `/api/v1/batches/${encodeURIComponent(batch.batch_id)}/start`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      state.agent = null;
      state.events = [];
      toast("解析已启动", "解析任务正在后台运行。", "success");
    }
    render();
    schedulePoll(200);
  } catch (error) {
    toast("启动失败", error.message, "error");
  } finally {
    elements.startRunButton.disabled = false;
    elements.startRunButton.textContent = "开始解析";
  }
}

function schedulePoll(delay) {
  window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(pollBatch, delay);
}

async function pollBatch() {
  if (!state.batch || state.polling) return;
  state.polling = true;
  const batchId = state.batch.batch_id;
  try {
    state.batch = await api(`/api/v1/batches/${encodeURIComponent(batchId)}`);
    if (state.mode === "agent" || state.agent) {
      state.agent = await api(
        `/api/v1/batches/${encodeURIComponent(batchId)}/agent-runs/latest`,
      );
      state.events = (
        await api(
          `/api/v1/batches/${encodeURIComponent(batchId)}/agent-runs/latest/events`,
        )
      ).events || [];
    }

    if (
      isTerminal() ||
      state.batch.completed_resources > 0 ||
      state.batch.failed_resources > 0
    ) {
      state.results = (
        await api(`/api/v1/batches/${encodeURIComponent(batchId)}/results`)
      ).results || [];
    }

    render();
    if (!isTerminal()) {
      schedulePoll(state.mode === "agent" ? 900 : 1300);
    } else if (state.agent?.status === "REVIEW_REQUIRED") {
      toast("Agent 需要确认", "结果已生成，请检查待确认项。", "error");
    } else if (
      state.batch.status === "FAILED" ||
      state.agent?.status === "FAILED"
    ) {
      toast(
        "解析失败",
        state.agent?.error || state.batch.error || "请检查输入。",
        "error",
      );
    } else {
      toast("解析完成", "结构化结果已生成。", "success");
    }
  } catch (error) {
    toast("状态查询失败", error.message, "error");
    if (!isTerminal()) schedulePoll(2500);
  } finally {
    state.polling = false;
  }
}

async function checkHealth() {
  try {
    const health = await api("/health");
    elements.healthBadge.dataset.state = "ok";
    elements.healthText.textContent =
      health.semantic_provider === "openai_compatible" ? "云端 API" : "规则引擎";
  } catch {
    elements.healthBadge.dataset.state = "error";
    elements.healthText.textContent = "服务不可用";
  }
}

function bindEvents() {
  elements.sqlPickButton.addEventListener("click", () => elements.sqlFiles.click());
  elements.dataPickButton.addEventListener("click", () =>
    elements.dataFiles.click(),
  );
  elements.sqlFiles.addEventListener("change", (event) => {
    setFiles("sql", event.target.files);
    event.target.value = "";
  });
  elements.dataFiles.addEventListener("change", (event) => {
    setFiles("data", event.target.files);
    event.target.value = "";
  });
  wireDropzone(elements.sqlDropzone, elements.sqlFiles, "sql");
  wireDropzone(elements.dataDropzone, elements.dataFiles, "data");

  elements.uploadButton.addEventListener("click", uploadBatch);
  elements.loadBatchButton.addEventListener("click", loadBatch);
  elements.batchLookup.addEventListener("keydown", (event) => {
    if (event.key === "Enter") loadBatch();
  });
  elements.toggleUploadButton.addEventListener("click", () => {
    const collapsed = elements.uploadSection.classList.toggle("collapsed");
    elements.toggleUploadButton.textContent = collapsed ? "展开" : "收起";
  });
  elements.copyBatchButton.addEventListener("click", async () => {
    if (!state.batch) return;
    await navigator.clipboard.writeText(state.batch.batch_id);
    toast("已复制", state.batch.batch_id, "success");
  });

  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });

  elements.modeSelector.addEventListener("change", (event) => {
    state.mode = event.target.value;
    elements.requireApiRow.classList.toggle("hidden", state.mode !== "agent");
  });

  elements.resourceList.addEventListener("click", (event) => {
    const item = event.target.closest("[data-resource-code]");
    if (!item) return;
    persistCurrentResource();
    state.selectedCode = item.dataset.resourceCode;
    renderResourceList();
    renderConfig();
  });

  elements.resourceDescription.addEventListener("input", () => {
    const resource = selectedResource();
    if (!resource) return;
    const key = resource.sql_file || resource.resource_code;
    state.descriptions[key] = elements.resourceDescription.value;
  });

  elements.fieldEditorList.addEventListener("input", (event) => {
    const input = event.target.closest("textarea[data-field-name]");
    const resource = selectedResource();
    if (!input || !resource) return;
    const key = resource.sql_file || resource.resource_code;
    state.fieldDescriptions[key] ||= {};
    state.fieldDescriptions[key][input.dataset.fieldName] = input.value;
  });

  elements.pairingSelect.addEventListener("change", () => {
    const resource = selectedResource();
    if (!resource) return;
    const key = resource.sql_file || resource.resource_code;
    const value = elements.pairingSelect.value;
    state.mappings[key] = value;
    resource.data_file = value || null;
    resource.matched_by = value ? "manual" : "none";
    if (value && !state.knownDataFiles.includes(value)) {
      state.knownDataFiles.push(value);
    }
    renderResourceList();
    renderConfig();
  });

  elements.expandFieldsButton.addEventListener("click", () => {
    const details = [...elements.fieldEditorList.querySelectorAll("details")];
    const shouldOpen = details.some((item) => !item.open);
    details.forEach((item) => {
      item.open = shouldOpen;
    });
    elements.expandFieldsButton.textContent = shouldOpen ? "全部收起" : "全部展开";
  });

  elements.startRunButton.addEventListener("click", startRun);
}

bindEvents();
renderFileLists();
checkHealth();
