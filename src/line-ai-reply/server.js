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

async function handleEvent(event) {
  if (event.type !== "message" || event.message.type !== "text") return;

  const userText = event.message.text;

  // 1) AIで返信案を作る
  const s = await makeSuggestions(userText);

  // 2) Quick Reply（タップで即送信）を返す
  const quickItems = [
    { type: "action", action: { type: "message", label: "カジュアル", text: s.casual } },
    { type: "action", action: { type: "message", label: "丁寧", text: s.polite } },
    { type: "action", action: { type: "message", label: "要点", text: s.brief } }
  ];

  // 3) 本文には候補を見やすく載せ、下にQuick Replyを付ける
  const body = [
    "🔮 AIの返信候補です。タップで送信：",
    "",
    `・カジュアル：\n${s.casual}`,
    "",
    `・丁寧：\n${s.polite}`,
    "",
    `・要点：\n${s.brief}`
  ].join("\n");

  await client.replyMessage(event.replyToken, {
    type: "text",
    text: body,
    quickReply: { items: quickItems }
  });
  
}

const port = process.env.PORT || 3000;
app.listen(port, () => console.log(`Listening on ${port}`));
