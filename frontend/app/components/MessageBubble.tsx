type Props = {
  role: "user" | "assistant";
  text: string;
};

export default function MessageBubble({ role, text }: Props) {
  const isUser = role === "user";
  return (
    <div
      style={{
        alignSelf: isUser ? "flex-end" : "flex-start",
        maxWidth: "80%",
        background: isUser ? "#2563eb" : "#1e293b",
        borderRadius: "12px",
        padding: "10px 12px",
        whiteSpace: "pre-wrap"
      }}
    >
      {text}
    </div>
  );
}
