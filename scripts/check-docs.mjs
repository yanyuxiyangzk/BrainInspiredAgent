import fs from "node:fs";

const source = fs.readFileSync("docs-site/app.js", "utf8");
const paths = [...source.matchAll(/\["[^"]+", "(docs\/[^"]+\.md)"\]/g)].map((match) => match[1]);
const missing = paths.filter((path) => !fs.existsSync(path));

if (missing.length) {
  console.error(`缺少文档：\n${missing.join("\n")}`);
  process.exit(1);
}

console.log(`文档树检查通过，共 ${paths.length} 篇。`);
