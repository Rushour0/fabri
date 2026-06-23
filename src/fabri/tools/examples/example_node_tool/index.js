// fabri polyglot example: a tool written in Node.js.
//
// Demonstrates the fabri tool contract from JavaScript/TypeScript-land:
//   1. Read one JSON object from stdin.
//   2. Print one JSON object to stdout.
//   3. Exit 0 on success, non-zero on failure.
//
// The tool computes basic statistics over a file (line/word/byte count,
// language guess from extension). Small but non-trivial — proves you can wire
// in the JS ecosystem without rewriting tools in Python.

const fs = require("fs");
const path = require("path");

const LANG_BY_EXT = {
  ".py": "python", ".js": "javascript", ".ts": "typescript",
  ".rs": "rust", ".go": "go", ".java": "java", ".c": "c",
  ".cpp": "c++", ".rb": "ruby", ".sh": "shell", ".md": "markdown",
  ".json": "json", ".yml": "yaml", ".yaml": "yaml",
};

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
  });
}

async function main() {
  try {
    const raw = await readStdin();
    const { path: filePath } = JSON.parse(raw);
    if (!filePath) throw new Error("missing 'path' in input");
    const stat = fs.statSync(filePath);
    const content = fs.readFileSync(filePath, "utf8");
    const lines = content.split(/\r?\n/).length;
    const words = content.split(/\s+/).filter(Boolean).length;
    const ext = path.extname(filePath).toLowerCase();
    const result = {
      path: filePath,
      bytes: stat.size,
      lines,
      words,
      language: LANG_BY_EXT[ext] || "unknown",
    };
    process.stdout.write(JSON.stringify(result));
    process.exit(0);
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: e.message }));
    process.exit(1);
  }
}

main();
