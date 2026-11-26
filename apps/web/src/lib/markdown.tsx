import type { ReactNode } from 'react';

/**
 * Renders the small Markdown subset that chunk text uses (headings, lists, tables, code,
 * paragraphs) as React elements. No HTML is ever injected, so document text cannot run script.
 */
export function Markdown({ text }: { text: string }) {
  const parts = splitBlocks(text);
  const out: ReactNode[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  const flush = () => {
    if (!list) return;
    const items = list.items.map((it, i) => <li key={i}>{it}</li>);
    out.push(list.ordered ? <ol key={out.length}>{items}</ol> : <ul key={out.length}>{items}</ul>);
    list = null;
  };

  for (const part of parts) {
    const bullet = /^[-*•]\s+(.*)$/s.exec(part);
    const numbered = /^\(?\d{1,3}[.)]\s+(.*)$/s.exec(part);
    if (bullet || numbered) {
      const ordered = !bullet;
      if (list && list.ordered !== ordered) flush();
      list ??= { ordered, items: [] };
      list.items.push((bullet ?? numbered)![1]!);
      continue;
    }
    flush();
    const key = out.length;
    const heading = /^(#{1,6})\s+(.*)$/s.exec(part);
    if (heading) {
      const level = heading[1]!.length;
      const Tag = `h${level}` as 'h1';
      out.push(<Tag key={key}>{heading[2]}</Tag>);
    } else if (part.startsWith('```')) {
      out.push(<pre key={key}>{part.replace(/^```\w*\n?/, '').replace(/\n?```$/, '')}</pre>);
    } else if (part.startsWith('$$')) {
      out.push(<pre key={key}>{part}</pre>);
    } else if (part.split('\n').every((l) => l.trim().startsWith('|'))) {
      out.push(<Table key={key} source={part} />);
    } else {
      const lines = part.split('\n');
      out.push(
        <p key={key}>
          {lines.map((l, i) => (
            <span key={i}>
              {i > 0 && <br />}
              {l}
            </span>
          ))}
        </p>,
      );
    }
  }
  flush();
  return <div className="md">{out}</div>;
}

function splitBlocks(text: string): string[] {
  const parts: string[] = [];
  let fence: string[] | null = null;
  for (const raw of text.split(/\n{2,}/)) {
    if (fence) {
      fence.push(raw);
      if (raw.trimEnd().endsWith('```')) {
        parts.push(fence.join('\n\n'));
        fence = null;
      }
      continue;
    }
    if (raw.startsWith('```') && !(raw.length > 3 && raw.trimEnd().endsWith('```'))) fence = [raw];
    else if (raw.trim()) parts.push(raw.trim());
  }
  if (fence) parts.push(fence.join('\n\n'));
  return parts;
}

function cells(line: string): string[] {
  return line
    .trim()
    .replace(/^\||\|$/g, '')
    .split(/(?<!\\)\|/)
    .map((c) => c.trim().replace(/\\\|/g, '|'));
}

function Table({ source }: { source: string }) {
  const lines = source.split('\n').filter((l) => !/^\s*\|?\s*:?-{3,}/.test(l));
  const [head, ...body] = lines.map(cells);
  return (
    <table>
      {head && (
        <thead>
          <tr>
            {head.map((c, i) => (
              <th key={i}>{c}</th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>
        {body.map((row, r) => (
          <tr key={r}>
            {row.map((c, i) => (
              <td key={i}>{c}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}
