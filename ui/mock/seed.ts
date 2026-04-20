/**
 * Mock data seeded for the dev admin UI (M0-M5 pre-integration).
 *
 * Field shape mirrors the eventual gateway payloads documented in
 * plan §7 (plugins) and §8 (agents / logs). Plugin / agent names are
 * corlinman-style plugin samples so the UI feels honest.
 *
 * TODO(M6): delete in favour of live gateway responses.
 */

export type PluginStatus = "loaded" | "disabled" | "error";

export interface MockPlugin {
  name: string;
  version: string;
  status: PluginStatus;
  manifest_path: string;
  origin: "Bundled" | "Global" | "Workspace" | "Config";
  plugin_type: "synchronous" | "asynchronous" | "messagePreprocessor";
  capabilities: string[];
  description: string;
  last_touched_at: string;
  error?: string;
}

export const MOCK_PLUGINS: MockPlugin[] = [
  {
    name: "DailyNote",
    version: "2.0.0",
    status: "loaded",
    manifest_path: "Plugin/DailyNote/plugin-manifest.json",
    origin: "Workspace",
    plugin_type: "synchronous",
    capabilities: ["create", "update"],
    description: "日记系统 (创建与更新)",
    last_touched_at: "2026-04-19T14:22:10Z",
  },
  {
    name: "FileOperator",
    version: "1.0.1",
    status: "loaded",
    manifest_path: "Plugin/FileOperator/plugin-manifest.json",
    origin: "Workspace",
    plugin_type: "synchronous",
    capabilities: [
      "ReadFile",
      "WriteFile",
      "ListDirectory",
      "SearchFiles",
      "DownloadFile",
      "CopyFile",
      "MoveFile",
      "DeleteFile",
      "ApplyDiff",
    ],
    description: "强大的文件系统操作插件，支持 PDF / Word / 表格解析。",
    last_touched_at: "2026-04-18T06:40:03Z",
  },
  {
    name: "WeatherInfoNow",
    version: "0.9.2",
    status: "loaded",
    manifest_path: "Plugin/WeatherInfoNow/plugin-manifest.json",
    origin: "Bundled",
    plugin_type: "synchronous",
    capabilities: ["query"],
    description: "实时天气查询（和风天气 API）。",
    last_touched_at: "2026-04-20T01:05:22Z",
  },
  {
    name: "RAGDiaryPlugin",
    version: "1.2.0",
    status: "loaded",
    manifest_path: "Plugin/RAGDiaryPlugin/plugin-manifest.json",
    origin: "Workspace",
    plugin_type: "messagePreprocessor",
    capabilities: ["inject_context"],
    description: "为每次对话注入相关日记片段（向量检索 + BM25 融合）。",
    last_touched_at: "2026-04-20T02:12:55Z",
  },
  {
    name: "ArxivDailyPapers",
    version: "0.3.4",
    status: "disabled",
    manifest_path: "Plugin/ArxivDailyPapers/plugin-manifest.json",
    origin: "Bundled",
    plugin_type: "asynchronous",
    capabilities: ["fetch_daily"],
    description: "每日抓取 arXiv 新论文，推送到指定频道。",
    last_touched_at: "2026-04-17T22:00:00Z",
  },
  {
    name: "ChromeBridge",
    version: "1.0.0",
    status: "disabled",
    manifest_path: "Plugin/ChromeBridge/plugin-manifest.json",
    origin: "Workspace",
    plugin_type: "synchronous",
    capabilities: ["open_tab", "extract_text", "screenshot"],
    description: "通过扩展桥接 Chrome，允许 AI 控制当前浏览器窗口。",
    last_touched_at: "2026-04-15T10:30:42Z",
  },
  {
    name: "ComfyUIGen",
    version: "2.4.1",
    status: "loaded",
    manifest_path: "Plugin/ComfyUIGen/plugin-manifest.json",
    origin: "Bundled",
    plugin_type: "asynchronous",
    capabilities: ["text_to_image", "image_to_image"],
    description: "本地 ComfyUI 工作流调用（Flux / SDXL）。",
    last_touched_at: "2026-04-19T18:44:11Z",
  },
  {
    name: "BilibiliFetch",
    version: "0.5.7",
    status: "error",
    manifest_path: "Plugin/BilibiliFetch/plugin-manifest.json",
    origin: "Workspace",
    plugin_type: "synchronous",
    capabilities: ["fetch_video_info", "fetch_comments"],
    description: "B 站视频 / 评论抓取。",
    last_touched_at: "2026-04-20T03:01:08Z",
    error: "manifest entryPoint.command 指向不存在的脚本 fetch.js (ENOENT)",
  },
  {
    name: "FlashDeepSearch",
    version: "1.1.2",
    status: "loaded",
    manifest_path: "Plugin/FlashDeepSearch/plugin-manifest.json",
    origin: "Workspace",
    plugin_type: "asynchronous",
    capabilities: ["search", "summarize"],
    description: "多引擎深度搜索（Tavily + SerpAPI + 本地 RAG）。",
    last_touched_at: "2026-04-20T01:55:17Z",
  },
  {
    name: "DailyHot",
    version: "0.2.0",
    status: "disabled",
    manifest_path: "Plugin/DailyHot/plugin-manifest.json",
    origin: "Bundled",
    plugin_type: "asynchronous",
    capabilities: ["fetch_hot"],
    description: "抓取每日热搜（知乎 / 微博 / V2EX 等）。",
    last_touched_at: "2026-04-10T08:22:00Z",
  },
  {
    name: "DeepWiki",
    version: "0.8.0",
    status: "error",
    manifest_path: "~/.corlinman/plugins/deepwiki/manifest.toml",
    origin: "Workspace",
    plugin_type: "synchronous",
    capabilities: ["search_wiki"],
    description: "在本地 wiki 仓库中检索并返回条目。",
    last_touched_at: "2026-04-19T11:08:42Z",
    error: "Python 依赖缺失：ModuleNotFoundError: No module named 'chromadb'",
  },
];

export interface MockAgent {
  name: string;
  file_path: string;
  bytes: number;
  last_modified: string;
}

export const MOCK_AGENTS: MockAgent[] = [
  {
    name: "Aemeath",
    file_path: "Agent/Aemeath.txt",
    bytes: 18234,
    last_modified: "2026-04-20T09:32:11Z",
  },
  {
    name: "Nova",
    file_path: "Agent/Nova.txt",
    bytes: 12508,
    last_modified: "2026-04-19T23:05:42Z",
  },
  {
    name: "DreamNova",
    file_path: "Agent/DreamNova.txt",
    bytes: 14902,
    last_modified: "2026-04-18T16:20:18Z",
  },
  {
    name: "Hornet",
    file_path: "Agent/Hornet.txt",
    bytes: 9420,
    last_modified: "2026-04-17T08:41:02Z",
  },
  {
    name: "Metis",
    file_path: "Agent/Metis.txt",
    bytes: 11288,
    last_modified: "2026-04-15T12:10:33Z",
  },
  {
    name: "ThemeMaidCoco",
    file_path: "Agent/ThemeMaidCoco.txt",
    bytes: 7315,
    last_modified: "2026-04-12T20:55:47Z",
  },
];

export interface MockLogEvent {
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  subsystem:
    | "gateway"
    | "plugins"
    | "scheduler"
    | "rag"
    | "channels.qq"
    | "python.ai";
  trace_id: string;
  message: string;
}

/** Ring of log templates used to generate infinite fake stream events. */
export const LOG_TEMPLATES: Array<Omit<MockLogEvent, "ts" | "trace_id">> = [
  {
    level: "info",
    subsystem: "gateway",
    message: "POST /v1/chat/completions 200 in 842ms (model=gpt-4o-mini)",
  },
  {
    level: "debug",
    subsystem: "plugins",
    message: "registry: loaded manifest DailyNote v2.0.0 (Workspace)",
  },
  {
    level: "info",
    subsystem: "channels.qq",
    message: "OneBot ws frame received: group_message from 987654321",
  },
  {
    level: "warn",
    subsystem: "rag",
    message: "vector store query exceeded 500ms budget (took 713ms)",
  },
  {
    level: "error",
    subsystem: "plugins",
    message: "BilibiliFetch: manifest entryPoint points to missing fetch.js",
  },
  {
    level: "info",
    subsystem: "scheduler",
    message: "cron tick fired: job=daily_report_push at 07:00 CST",
  },
  {
    level: "debug",
    subsystem: "python.ai",
    message: "embedding request batched: 16 inputs (bge-m3)",
  },
  {
    level: "info",
    subsystem: "gateway",
    message: "admin: user resolved session=sid_01HY... role=admin",
  },
  {
    level: "warn",
    subsystem: "channels.qq",
    message: "keyword trigger throttled: group=123456 cooldown 30s",
  },
  {
    level: "debug",
    subsystem: "rag",
    message: "bm25 rerank complete: kept top-5 of 42 candidates",
  },
];

/** Generate a cheap trace id that looks like a W3C traceparent fragment. */
export function genTraceId(): string {
  const hex = () => Math.floor(Math.random() * 16).toString(16);
  let out = "";
  for (let i = 0; i < 16; i++) out += hex();
  return out;
}

// --- Approvals (S5 T4 addition) -------------------------------------------
// Shape mirrors Rust `ApprovalOut` in
// rust/crates/corlinman-gateway/src/routes/admin/approvals.rs so the admin
// page consumes mock + real gateway payloads without branching.

export interface MockApproval {
  id: string;
  plugin: string;
  tool: string;
  session_key: string;
  args_json: string;
  requested_at: string;
  decided_at: string | null;
  decision: string | null;
}

export const MOCK_PENDING_APPROVALS: MockApproval[] = [
  {
    id: "apv_01HXYZA",
    plugin: "FileOperator",
    tool: "WriteFile",
    session_key: "qq:group:123456",
    args_json: JSON.stringify({
      path: "./Daily/2026-04-20.md",
      content:
        "# 2026-04-20\n\n- 调试 approvals 页\n- 跟 corlinman-gateway 对接 SSE\n",
      mode: "overwrite",
    }),
    requested_at: "2026-04-20T06:11:02Z",
    decided_at: null,
    decision: null,
  },
  {
    id: "apv_01HXYZB",
    plugin: "FileOperator",
    tool: "DeleteFile",
    session_key: "qq:private:88442211",
    args_json: JSON.stringify({ path: "/tmp/scratch.log" }),
    requested_at: "2026-04-20T06:14:48Z",
    decided_at: null,
    decision: null,
  },
  {
    id: "apv_01HXYZC",
    plugin: "ChromeBridge",
    tool: "open_tab",
    session_key: "web:admin:nova",
    args_json: JSON.stringify({
      url: "https://example.com/reports/q1",
      focus: true,
    }),
    requested_at: "2026-04-20T06:18:20Z",
    decided_at: null,
    decision: null,
  },
];

export const MOCK_HISTORY_APPROVALS: MockApproval[] = [
  {
    id: "apv_01HXXX1",
    plugin: "FileOperator",
    tool: "WriteFile",
    session_key: "qq:group:123456",
    args_json: JSON.stringify({
      path: "./Daily/2026-04-19.md",
      content: "# 2026-04-19\n- 今日已完成：…\n",
    }),
    requested_at: "2026-04-19T23:59:11Z",
    decided_at: "2026-04-19T23:59:41Z",
    decision: "approved",
  },
  {
    id: "apv_01HXXX2",
    plugin: "FileOperator",
    tool: "DeleteFile",
    session_key: "qq:group:654321",
    args_json: JSON.stringify({ path: "/etc/hosts" }),
    requested_at: "2026-04-19T18:02:00Z",
    decided_at: "2026-04-19T18:02:22Z",
    decision: "denied",
  },
];
