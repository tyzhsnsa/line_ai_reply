import "dotenv/config";
import express from "express";
import { middleware, Client } from "@line/bot-sdk";
import { makeSuggestions } from "./ai.js";

const config = {
  channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN,
  channelSecret: process.env.LINE_CHANNEL_SECRET
};
const app = express();

// 起動時に環境変数が入っているかだけ確認
console.log("ENV CHECK:", {
  hasSecret: !!process.env.LINE_CHANNEL_SECRET,
  hasToken: !!process.env.LINE_CHANNEL_ACCESS_TOKEN,
  hasGeminiKey: !!process.env.GEMINI_API_KEY,
});

// ★ ここが肝：LINEのミドルウェアだけで受け取り→200を返す
app.post("/webhook", middleware(config), async (req, res) => {
  try {
    const events = req.body?.events || [];
    await Promise.all(events.map(handleEvent));
    res.status(200).end(); // ← Verifyが求める200
  } catch (e) {
    console.error("Webhook error:", e);
    // Verifyを通すために200で返しておく（ログだけ残す）
    res.status(200).end();
  }
});

// 署名検証付きミドルウェア
app.use("/webhook", middleware(config), express.json());

// Webhookエンドポイント
app.post("/webhook", async (req, res) => {
  const events = req.body.events;
  await Promise.all(events.map(handleEvent));
  res.status(200).end();
});

const client = new Client(config);


// --- AI返信候補を送信する部分 ---
async function sendReplySuggestions(event, suggestions) {
  const { casual, polite, concise } = suggestions;

  const messages = [
    {
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
              text: "🔮 AIの返信候補です。タップでコピーできます：",
              weight: "bold",
              size: "md",
              wrap: true,
            },
            {
              type: "text",
              text: `・カジュアル：\n${casual}\n\n・丁寧：\n${polite}\n\n・要点：\n${concise}`,
              wrap: true,
              margin: "md",
            },
          ],
        },
        footer: {
          type: "box",
          layout: "horizontal",
          contents: [
            {
              type: "button",
              style: "primary",
              color: "#00bcd4",
              action: {
                type: "uri",
                label: "カジュアル📋",
                uri: `line://msg/text/?${encodeURIComponent(casual)}`,
              },
            },
            {
              type: "button",
              style: "secondary",
              color: "#4CAF50",
              action: {
                type: "uri",
                label: "丁寧📋",
                uri: `line://msg/text/?${encodeURIComponent(polite)}`,
              },
            },
            {
              type: "button",
              style: "secondary",
              color: "#9C27B0",
              action: {
                type: "uri",
                label: "要点📋",
                uri: `line://msg/text/?${encodeURIComponent(concise)}`,
              },
            },
          ],
        },
      },
    },
  ];

  await fetch("https://api.line.me/v2/bot/message/reply", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${process.env.LINE_ACCESS_TOKEN}`,
    },
    body: JSON.stringify({ replyToken: event.replyToken, messages }),
  });
}

  
}

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Listening on ${port}`));
