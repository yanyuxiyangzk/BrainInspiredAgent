import { marked } from "marked";
import mermaid from "mermaid";
import { markedHighlight } from "marked-highlight";
import markedFootnote from "marked-footnote";
import markedAlert from "marked-alert";
import markedKatex from "marked-katex-extension";
import hljs from "highlight.js";

const groups = [
  {
    label: "开始",
    docs: [
      ["文档中心", "docs/README.md"],
      ["产品愿景与范围", "docs/product/vision-and-scope.md"],
      ["产品需求 PRD", "docs/product/prd.md"],
    ],
  },
  {
    label: "核心架构",
    docs: [
      ["系统架构", "docs/architecture/system-architecture.md"],
      ["DNA 技术架构", "docs/architecture/dna-architecture.md"],
      ["平台与领域应用分层", "docs/architecture/platform-domain-separation.md"],
      ["可进化 Workflow 与 Skill", "docs/architecture/evolvable-workflow-skill-architecture.md"],
      ["Loop Engineering 因子发现", "docs/architecture/factor-discovery-loop-architecture.md"],
      ["记忆系统", "docs/architecture/memory-system.md"],
      ["安全与治理", "docs/architecture/safety-and-governance.md"],
    ],
  },
  {
    label: "技术规范",
    docs: [
      ["事件协议", "docs/specifications/event-protocol.md"],
      ["Workflow 规范", "docs/specifications/workflow-spec.md"],
      ["Plan、Task 与 Error", "docs/specifications/plan-task-error-spec.md"],
      ["运行时数据与事务", "docs/specifications/runtime-data-and-transactions.md"],
      ["Skill 调用协议", "docs/specifications/skill-invocation-protocol.md"],
    ],
  },
  {
    label: "场景与质量",
    docs: [
      ["MVP 端到端场景", "docs/scenarios/mvp-end-to-end-scenarios.md"],
      ["P0 验收标准", "docs/quality/p0-acceptance-criteria.md"],
      ["测试与验收", "docs/quality/test-and-acceptance.md"],
      ["Markdown 渲染测试", "docs/quality/markdown-rendering-test.md"],
      ["可观测性与运维", "docs/operations/observability-and-operations.md"],
      ["MVP 运维 Runbook", "docs/operations/mvp-runbook.md"],
    ],
  },
  {
    label: "交付计划",
    docs: [
      ["实施路线图", "docs/delivery/roadmap.md"],
      ["开发任务规划", "docs/delivery/development-plan.md"],
      ["阶段 0 冻结就绪", "docs/delivery/stage-0-freeze-readiness.md"],
      ["T06 发布验收", "docs/delivery/t06-release-validation.md"],
      ["开放问题", "docs/delivery/open-questions.md"],
    ],
  },
  {
    label: "评审记录",
    docs: [
      ["需求评审", "docs/reviews/requirements-review-2026-08-16.md"],
      ["技术架构复查", "docs/reviews/technical-architecture-review-2026-08-16.md"],
    ],
  },
  {
    label: "架构决策",
    docs: [
      ["ADR-0001 控制平面边界", "docs/decisions/ADR-0001-deterministic-control-plane.md"],
      ["ADR-0002 运行模型", "docs/decisions/ADR-0002-runtime-model.md"],
      ["ADR-0003 MVP 产品基线", "docs/decisions/ADR-0003-mvp-product-baseline.md"],
      ["ADR-0004 可进化 Workflow", "docs/decisions/ADR-0004-capability-bound-evolvable-workflows.md"],
    ],
  },
  {
    label: "附录",
    docs: [["术语表", "docs/glossary.md"]],
  },
];

const tree = document.querySelector("#docTree");
const content = document.querySelector("#content");
const toc = document.querySelector("#pageToc");
const breadcrumbs = document.querySelector("#breadcrumbs");
const sidebar = document.querySelector("#sidebar");
const backdrop = document.querySelector("#backdrop");
const searchInput = document.querySelector("#searchInput");

const allDocs = groups.flatMap((group) => group.docs.map(([title, path]) => ({ title, path, group: group.label })));
const legacyPaths = {
  "docs/architecture/technical-architecture-overview.md": "docs/architecture/system-architecture.md",
};

marked.use({
  gfm: true,
  breaks: false,
});
marked.use(
  markedHighlight({
    langPrefix: "hljs language-",
    highlight(code, language) {
      const lang = language === "mermaid" || language === "text" ? "plaintext" : language;
      if (lang && hljs.getLanguage(lang)) return hljs.highlight(code, { language: lang }).value;
      return hljs.highlightAuto(code).value;
    },
  }),
  markedFootnote({ refMarkers: true, footnoteDivider: true }),
  markedAlert(),
  markedKatex({ throwOnError: false, nonStandard: true }),
);

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "neutral",
  themeVariables: {
    fontSize: "16px",
    primaryColor: "#eaf8f0",
    primaryTextColor: "#17212b",
    primaryBorderColor: "#26805c",
    lineColor: "#64748b",
    clusterBkg: "#f8fafc",
    clusterBorder: "#94a3b8",
  },
  fontFamily: 'Inter, "Noto Sans SC", "Microsoft YaHei", sans-serif',
  flowchart: {
    curve: "basis",
    htmlLabels: true,
    nodeSpacing: 34,
    rankSpacing: 48,
  },
});

function renderTree(filter = "") {
  const query = filter.trim().toLowerCase();
  tree.innerHTML = "";

  for (const group of groups) {
    const matching = group.docs.filter(([title]) => title.toLowerCase().includes(query));
    if (!matching.length) continue;

    const section = document.createElement("section");
    section.className = "tree-group";
    const heading = document.createElement("div");
    heading.className = "tree-heading";
    heading.textContent = group.label;
    section.append(heading);

    for (const [title, path] of matching) {
      const link = document.createElement("a");
      link.href = `#${path}`;
      link.dataset.path = path;
      link.textContent = title;
      section.append(link);
    }
    tree.append(section);
  }
  markActive();
}

function currentPath() {
  const requested = decodeURIComponent(location.hash.slice(1)).split("?", 1)[0];
  const normalized = legacyPaths[requested] || requested;
  return allDocs.some((doc) => doc.path === normalized) ? normalized : "docs/README.md";
}

function markActive() {
  const path = currentPath();
  tree.querySelectorAll("a").forEach((link) => link.classList.toggle("active", link.dataset.path === path));
}

function slugify(text, used) {
  const base = text.trim().toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-|-$/g, "") || "section";
  let slug = base;
  let counter = 2;
  while (used.has(slug)) slug = `${base}-${counter++}`;
  used.add(slug);
  return slug;
}

function buildToc() {
  toc.innerHTML = "";
  const used = new Set();
  const headings = content.querySelectorAll("h2, h3");
  headings.forEach((heading) => {
    heading.id = slugify(heading.textContent, used);
    const link = document.createElement("a");
    link.href = `#${currentPath()}?section=${encodeURIComponent(heading.id)}`;
    link.textContent = heading.textContent;
    link.className = heading.tagName === "H3" ? "toc-sub" : "";
    link.addEventListener("click", (event) => {
      event.preventDefault();
      heading.scrollIntoView({ behavior: "smooth", block: "start" });
      history.replaceState(null, "", `#${currentPath()}?section=${encodeURIComponent(heading.id)}`);
    });
    toc.append(link);
  });
}

function rewriteMarkdownLinks() {
  content.querySelectorAll("a[href]").forEach((link) => {
    const href = link.getAttribute("href");
    if (!href || /^(https?:|mailto:|#)/.test(href)) return;
    const base = currentPath().split("/").slice(0, -1).join("/");
    const normalized = new URL(href, `http://local/${base}/`).pathname.slice(1);
    if (normalized.endsWith(".md") && allDocs.some((doc) => doc.path === normalized)) {
      link.href = `#${normalized}`;
    }
  });

  content.querySelectorAll("img[src]").forEach((img) => {
    const src = img.getAttribute("src");
    if (!src || /^(https?:|data:|\/)/.test(src)) return;
    const base = currentPath().split("/").slice(0, -1).join("/");
    img.src = new URL(src, `http://local/${base}/`).pathname;
    img.loading = "lazy";
    img.decoding = "async";
  });
}

async function loadDocument() {
  const rawHash = decodeURIComponent(location.hash.slice(1));
  const [requestedPath, params = ""] = rawHash.split("?", 2);
  const path = legacyPaths[requestedPath] || (allDocs.some((doc) => doc.path === requestedPath) ? requestedPath : "docs/README.md");
  const doc = allDocs.find((item) => item.path === path);

  if (legacyPaths[requestedPath]) {
    history.replaceState(null, "", `#${path}`);
  }

  content.innerHTML = '<p class="loading">正在加载文档…</p>';
  try {
    const response = await fetch(`/${path}`, { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    content.innerHTML = marked.parse(await response.text());
    content.querySelectorAll("pre code.language-mermaid").forEach((code) => {
      const container = document.createElement("div");
      container.className = "mermaid";
      container.textContent = code.textContent;
      code.closest("pre").replaceWith(container);
    });
    content.querySelectorAll("pre code.language-text").forEach((code) => {
      const isSystemArchitecture = (code.textContent.includes("Loop Engine") || code.textContent.includes("LoopEngine")) && code.textContent.includes("Thalamus EventBus");
      const isRuntimeFlow = code.textContent.includes("CorticalSchedulingPolicy") && code.textContent.includes("CandidatePlan");
      if (isSystemArchitecture || isRuntimeFlow) {
        code.closest("pre").classList.add("architecture-ascii");
      }
    });
    await mermaid.run({ nodes: content.querySelectorAll(".mermaid") });
    rewriteMarkdownLinks();
    buildToc();
    breadcrumbs.textContent = `${doc.group} / ${doc.title}`;
    document.title = `${doc.title} · BIA 文档`;
    markActive();
    sidebar.classList.remove("open");
    backdrop.classList.remove("open");
    window.scrollTo({ top: 0 });

    const section = new URLSearchParams(params).get("section");
    if (section) requestAnimationFrame(() => document.getElementById(section)?.scrollIntoView());
  } catch (error) {
    content.innerHTML = `<h1>文档加载失败</h1><p>无法读取 <code>${path}</code>。</p><pre>${error.message}</pre>`;
  }
}

document.querySelector("#menuButton").addEventListener("click", () => {
  sidebar.classList.add("open");
  backdrop.classList.add("open");
});
backdrop.addEventListener("click", () => {
  sidebar.classList.remove("open");
  backdrop.classList.remove("open");
});
searchInput.addEventListener("input", () => renderTree(searchInput.value));
document.querySelector("#copyLinkButton").addEventListener("click", async () => {
  await navigator.clipboard.writeText(location.href);
  const button = document.querySelector("#copyLinkButton");
  button.textContent = "✓";
  setTimeout(() => { button.textContent = "⛓"; }, 1200);
});
window.addEventListener("hashchange", loadDocument);

renderTree();
loadDocument();
