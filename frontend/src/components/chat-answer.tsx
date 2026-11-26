import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

const plugins = [remarkGfm];

export function ChatAnswer({
  text,
  streaming = false,
}: {
  text: string;
  streaming?: boolean;
}) {
  return (
    <div className="agent-answer" aria-live={streaming ? "off" : undefined}>
      <Markdown
        remarkPlugins={plugins}
        skipHtml
        components={{
          img: ({ alt }) => alt ?? "",
          table: ({ children }) => (
            <div className="chat-table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {text}
      </Markdown>
    </div>
  );
}
