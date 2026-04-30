type Props = {
  role: "user" | "assistant";
  text: string;
};

export default function MessageBubble({ role, text }: Props) {
  const isUser = role === "user";
  return (
    <div className={isUser ? "chat-bubble chat-bubble-user" : "chat-bubble chat-bubble-assistant"}>
      {text}
    </div>
  );
}
