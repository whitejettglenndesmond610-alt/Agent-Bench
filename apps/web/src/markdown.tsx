import { ReactNode, useRef } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

function safeHref(href: string | undefined): string | undefined {
  if (!href) return undefined;
  if (href.startsWith("#") || href.startsWith("/")) return href;
  try {
    const url = new URL(href, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol)
      && !url.username
      && !url.password
      ? href
      : undefined;
  } catch {
    return undefined;
  }
}

function isExternalHref(href: string): boolean {
  try {
    const url = new URL(href, window.location.origin);
    return url.protocol === "mailto:" || url.origin !== window.location.origin;
  } catch {
    return false;
  }
}

function copyText(text: string) {
  if (navigator.clipboard) void navigator.clipboard.writeText(text).catch(() => undefined);
}

function CodeBlock({ children }: { children: ReactNode }) {
  const codeRef = useRef<HTMLPreElement>(null);
  return (
    <div className="workspace-code-block">
      <button type="button" onClick={() => copyText(codeRef.current?.innerText ?? "")}>复制代码</button>
      <pre ref={codeRef}>{children}</pre>
    </div>
  );
}

export function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="workspace-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ href, children, title }) => {
            const safe = safeHref(href);
            if (!safe) return <span>{children}</span>;
            const external = isExternalHref(safe);
            return (
              <a
                href={safe}
                title={title}
                target={external ? "_blank" : undefined}
                rel={external ? "noopener noreferrer" : undefined}
                referrerPolicy={external ? "no-referrer" : undefined}
              >
                {children}
              </a>
            );
          },
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          table: ({ children }) => (
            <div className="workspace-table-scroll"><table>{children}</table></div>
          ),
          img: ({ alt }) => (
            <span className="markdown-image-placeholder">{alt ? `[图片: ${alt}]` : "[远程图片已隐藏]"}</span>
          )
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

function citationParts(citation: string): { label: string; href?: string } {
  const looksLikeLink = /^(https?:\/\/|mailto:|\/|#)/i.test(citation);
  return { label: citation, href: looksLikeLink ? safeHref(citation) : undefined };
}

export function Citations({ citations }: { citations: string[] }) {
  if (!citations.length) return null;
  return (
    <section className="workspace-citations" aria-label="引用来源">
      <span>CITATIONS / {String(citations.length).padStart(2, "0")}</span>
      <ol>
        {citations.map((citation, index) => {
          const { label, href } = citationParts(citation);
          return (
            <li key={`${label}-${index}`}>
              {href ? (
                <a
                  href={href}
                  target={isExternalHref(href) ? "_blank" : undefined}
                  rel={isExternalHref(href) ? "noopener noreferrer" : undefined}
                  referrerPolicy={isExternalHref(href) ? "no-referrer" : undefined}
                >
                  {label}
                </a>
              ) : label}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
