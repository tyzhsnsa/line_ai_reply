import "dotenv/config";
import express from "express";
import { middleware, Client } from "@line/bot-sdk";
import { makeSuggestions } from "./ai.js";

const config = {
  channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN,
  channelSecret: process.env.LINE_CHANNEL_SECRET,
};

const app = express();
const client = new Client(config);

app.get("/", (_req, res) => res.status(200).send("OK"));

// Webhook受信
app.post("/webhook", middleware(config), async (req, res) => {
  const events = req.body.events;
  await Promise.all(events.map(handleEvent));
  res.status(200).end();
});

async function handleEvent(event) {
  if (event.type !== "message" || event.message.type !== "text") return;

  const userMessage = event.message.text;
  const suggestions = await makeSuggestions(userMessage);

  // undefined対策（fallback文あり）
  const casual = suggestions.casual || "（候補生成に失敗しました）";
  const polite = suggestions.polite || "（候補生成に失敗しました）";
  const brief = suggestions.brief || "（候補生成に失敗しました）";

  const flexMessage = {
    type: "flex",
    altText: "AIの返信候補です",
    contents: {
      type: "bubble",
      body: {
        type: "box",
        layout: "vertical",
        contents: [
          {
            type: "text",
            text: "🔮 AIの返信候補（タップでコピー）",
            weight: "bold",
            wrap: true,
          },
          { type: "separator", margin: "md" },
          {
            type: "text",
            text: `・カジュアル：\n${casual}`,
            wrap: true,
            margin: "md",
          },
          {
            type: "text",
            text: `・丁寧：\n${polite}`,
            wrap: true,
            margin: "md",
          },
          {
            type: "text",
            text: `・要点：\n${brief}`,
            wrap: true,
            margin: "md",
          },
        ],
      },
      footer: {
        type: "box",
        layout: "horizontal",
        contents: [
          makeCopyButton("カジュアル", casual),
          makeCopyButton("丁寧", polite),
          makeCopyButton("要点", brief),
        ],
      },
    },
  };

  await client.replyMessage(event.replyToken, flexMessage);
}

// ====== ✅ ボタン押下で「コピー可能」仕様 ======
function makeCopyButton(label, text) {
  return {
    type: "button",
    style: "secondary",
    color:
      label === "カジュアル"
        ? "#00BFFF"
        : label === "丁寧"
        ? "#228B22"
        : "#800080",
    action: {
      type: "uri",
      label: `${label}📋`,
      uri: `line://nv/clipboard?text=${encodeURIComponent(text)}`,
    },
  };
}

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Listening on ${port}`));
