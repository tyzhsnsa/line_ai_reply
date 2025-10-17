import "dotenv/config";
import express from "express";
import { middleware, Client } from "@line/bot-sdk";
import { makeSuggestions } from "./ai.js"; // { casual, polite, concise } を返す想定

// ===== LINE SDK 設定 =====
const config = {
  channelAccessToken: process.env.LINE_CHANNEL_ACCESS_TOKEN, // ← 変数名注意
  channelSecret: process.env.LINE_CHANNEL_SECRET,
};

const app = express();
const client = new Client(config);

// ヘルスチェック
app.get("/", (_req, res) => res.status(200).send("OK"));

// Webhook（ミドルウェアはここだけでOK。重複禁止）
app.post("/webhook", middleware(config), async (req, res) => {
  try {
    const events = req.body?.events || [];
    await Promise.all(events.map(handleEvent));
    res.sendStatus(200);
  } catch (e) {
    console.error("Webhook error:", e);
    // Verify を通すため 200 を返す
    res.sendStatus(200);
  }
});

// ===== イベント処理 =====
async function handleEvent(event) {
  // テキスト以外は無視
  if (event.type !== "message" || event.message?.type !== "text") {
    return;
  }

  const userText = event.message.text;

  // 返信候補生成
  let suggestions;
  try {
    suggestions = await makeSuggestions(userText);
  } catch (e) {
    console.error("makeSuggestions error:", e);
    // 失敗時はシンプルに返す
    await client.replyMessage(event.replyToken, {
      type: "text",
      text: "ごめんね、いま返信候補の生成に失敗しちゃった…もう一度試してみて！",
    });
    return;
  }

  // 候補を Flex で返す（タップでコピー）
  await sendReplySuggestions(event, suggestions);
}

// ===== Flex 返信（タップでコピー）=====
async function sendReplySuggestions(event, suggestions) {
  const { casual, polite, concise } = suggestions;

  const bubble = {
    type: "bubble",
    body: {
      type: "box",
      layout: "vertical",
      contents: [
        {
          type: "text",
          text: "🔮 AIの返信候補（タップでコピー）",
          weight: "bold",
          size: "md",
          wrap: true,
        },
        {
          type: "text",
          text:
            `・カジュアル：\n${casual}\n\n` +
            `・丁寧：\n${polite}\n\n` +
            `・要点：\n${concise}`,
          wrap: true,
          margin: "md",
        },
      ],
    },
    footer: {
      type: "box",
      layout: "horizontal",
      spacing: "md",
      contents: [
        {
          type: "button",
          style: "primary",
          color: "#00BCD4",
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
  };

  await client.replyMessage(event.replyToken, {
    type: "flex",
    altText: "AIの返信候補です（タップでコピー）",
    contents: bubble,
  });
}

// ===== 起動 =====
const port = process.env.PORT || 10000;
app.listen(port, () => {
  console.log("ENV CHECK:", {
    hasSecret: !!process.env.LINE_CHANNEL_SECRET,
    hasToken: !!process.env.LINE_CHANNEL_ACCESS_TOKEN,
    hasGeminiKey: !!process.env.GEMINI_API_KEY,
  });
  console.log(`Listening on ${port}`);
});
