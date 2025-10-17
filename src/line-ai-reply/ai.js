import axios from "axios";

export async function makeSuggestions(userMessage) {
  try {
    // ---- Geminiへ送るプロンプト ----
    const prompt = `
あなたはLINEの返信文を提案するAIアシスタントです。
次の入力メッセージに対して、以下3スタイルの返信をJSON形式で出力してください。

1) casual（カジュアル）: 親しみやすい日常会話スタイル
2) polite（丁寧）: ビジネス〜フォーマルな返信
3) brief（要点）: 簡潔でスピーディーな返信（50〜90文字程度）

条件:
- どれも自然な日本語で、60〜180字以内
- 絵文字は1つまで（自然な文脈でのみ）
- 個人情報・誹謗中傷・機密内容は避ける
入力文：
"""${userMessage}"""

出力は必ず以下のJSON形式で：
{"casual":"...", "polite":"...", "brief":"..."}
    `;

    // ---- Gemini API呼び出し ----
    const res = await axios.post(
      "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
      {
        contents: [{ role: "user", parts: [{ text: prompt }] }],
        generationConfig: { temperature: 0.7 }
      },
      { params: { key: process.env.GEMINI_API_KEY } }
    );

    // ---- レスポンスからテキスト抽出 ----
    const text = res.data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";

    // ---- JSON部分を抽出 ----
    const match = text.match(/\{[\s\S]*\}/);
    if (!match) {
      throw new Error("JSON出力が見つかりませんでした");
    }

    // ---- パースして安全に取り出す ----
    const parsed = JSON.parse(match[0]);

    // ---- undefined対策（fallback文付き） ----
    return {
      casual:
        parsed.casual ||
        "こんにちは！メッセージありがとうございます😊 どんなご用件でしょうか？お気軽に教えてくださいね！",
      polite:
        parsed.polite ||
        "ご連絡ありがとうございます。どのようなご用件でしょうか？内容を確認の上、ご返信させていただきます。",
      brief:
        parsed.brief ||
        "ご連絡ありがとうございます。詳細をお聞かせください。"
    };
  } catch (err) {
    console.error("AI返信生成エラー:", err.message);
    return {
      casual: "（候補生成に失敗しました。もう一度お試しください）",
      polite: "（候補生成に失敗しました。もう一度お試しください）",
      brief: "（候補生成に失敗しました。もう一度お試しください）"
    };
  }
}
