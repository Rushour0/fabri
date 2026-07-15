import type { ReactNode } from "react";

// A tiny, dependency-free Markdown renderer for the subset LLMs actually emit:
// paragraphs, headings, bold/italic, inline + fenced code, ordered/unordered
// lists, and links. It renders to React elements (never dangerouslySetInnerHTML),
// so untrusted model output can't inject markup — the trade-off for staying lean.

const INLINE_RE =
  /(`[^`]+`)|(\*\*[^*]+\*\*|__[^_]+__)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\([^)\s]+\))/g;

function renderInline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const k = `${key}-${i++}`;
    if (m[1]) {
      out.push(<code key={k} className="md-code">{tok.slice(1, -1)}</code>);
    } else if (m[2]) {
      out.push(<strong key={k}>{tok.slice(2, -2)}</strong>);
    } else if (m[3]) {
      out.push(<em key={k}>{tok.slice(1, -1)}</em>);
    } else if (m[4]) {
      const lm = /\[([^\]]+)\]\(([^)\s]+)\)/.exec(tok);
      out.push(
        lm ? (
          <a key={k} href={lm[2]} target="_blank" rel="noopener noreferrer">
            {lm[1]}
          </a>
        ) : (
          tok
        ),
      );
    }
    last = INLINE_RE.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const LIST_ITEM_RE = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/;
const HEADING_RE = /^(#{1,4})\s+(.*)$/;

export function Markdown({ text }: { text: string }): ReactNode {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  let b = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code block
    if (/^```/.test(line.trim())) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) body.push(lines[i++]);
      i++; // closing fence
      blocks.push(
        <pre key={`b${b++}`} className="md-pre">
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    // Blank line
    if (line.trim() === "") {
      i++;
      continue;
    }

    // Blockquote (consecutive `>` lines)
    if (/^\s*>\s?/.test(line)) {
      const quoted: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quoted.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      blocks.push(
        <blockquote key={`b${b++}`} className="md-quote">
          {renderInline(quoted.join(" "), `b${b}`)}
        </blockquote>,
      );
      continue;
    }

    // Heading
    const h = HEADING_RE.exec(line);
    if (h) {
      const level = Math.min(h[1].length, 4);
      const Tag = (`h${level + 2 <= 6 ? level + 2 : 6}`) as "h3" | "h4" | "h5" | "h6";
      blocks.push(
        <Tag key={`b${b++}`} className="md-h">
          {renderInline(h[2], `b${b}`)}
        </Tag>,
      );
      i++;
      continue;
    }

    // List (consecutive items of the same kind)
    if (LIST_ITEM_RE.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items: ReactNode[] = [];
      let li = 0;
      while (i < lines.length && LIST_ITEM_RE.test(lines[i])) {
        const lm = LIST_ITEM_RE.exec(lines[i])!;
        items.push(
          <li key={`li${li}`}>{renderInline(lm[3], `b${b}i${li}`)}</li>,
        );
        li++;
        i++;
      }
      blocks.push(
        ordered ? (
          <ol key={`b${b++}`} className="md-list">{items}</ol>
        ) : (
          <ul key={`b${b++}`} className="md-list">{items}</ul>
        ),
      );
      continue;
    }

    // Paragraph: gather consecutive non-blank, non-structural lines.
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !/^```/.test(lines[i].trim()) &&
      !HEADING_RE.test(lines[i]) &&
      !LIST_ITEM_RE.test(lines[i])
    ) {
      para.push(lines[i++]);
    }
    blocks.push(
      <p key={`b${b++}`} className="md-p">
        {renderInline(para.join(" "), `b${b}`)}
      </p>,
    );
  }

  return <div className="md">{blocks}</div>;
}
