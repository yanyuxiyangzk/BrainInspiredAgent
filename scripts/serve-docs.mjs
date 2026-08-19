import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const root = resolve(process.cwd());
const port = Number(process.env.BIA_DOCS_PORT || 4173);
const host = process.env.BIA_DOCS_HOST || "0.0.0.0";

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".md": "text/markdown; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
};

function resolveRequestPath(rawUrl) {
  const pathname = decodeURIComponent(new URL(rawUrl, "http://local").pathname);
  if (pathname === "/") return "/docs-site/index.html";
  if (pathname === "/docs-site") return "/docs-site/index.html";
  if (pathname.startsWith("/assets/")) return `/docs${pathname}`;
  return pathname;
}

createServer((request, response) => {
  try {
    const requestPath = resolveRequestPath(request.url || "/");
    const filePath = normalize(join(root, requestPath));
    if (!filePath.startsWith(`${root}/`)) throw new Error("path traversal");
    const stats = statSync(filePath);
    const resolvedFile = stats.isDirectory() ? join(filePath, "index.html") : filePath;
    response.writeHead(200, {
      "Content-Type": contentTypes[extname(resolvedFile).toLowerCase()] || "application/octet-stream",
      "Content-Length": statSync(resolvedFile).size,
      "Cache-Control": "no-cache",
    });
    if (request.method === "HEAD") return response.end();
    createReadStream(resolvedFile).pipe(response);
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("Not found\n");
  }
}).listen(port, host, () => {
  console.log(`BIA docs available at http://localhost:${port}/docs-site/`);
});
