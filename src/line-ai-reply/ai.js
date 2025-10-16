import axios from "axios";

export async function makeSuggestions(userMessage) {
  // ここではGeminiを例示（OpenAIに差し替えOK）
  const prompt = `
あなたはLINEの返信文を提案する役割です。
入力メッセージに対して、以下の3スタイルの日本語返信を返してください:
1) カジュアル
2) ビジネス
3) 要点だけの短文（急ぎ向け）
条件:
- どれも自然で短すぎず長すぎない（60〜180字目安）
- 絵文字は1つまで/返信にふさわしい場合のみ
- NG: 個人情報の要求、攻撃的表現
入力: """${userMessage}"""
出力はJSONで:
{"casual":"...", "polite":"...", "brief":"..."}
`;
  const res = await axios.post(
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
    {
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.7 }
    },
    { params: { key: process.env.GEMINI_API_KEY } }
  );

  const text = res.data?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";
  // 簡易JSON抽出（本番は厳密パース推奨）
  const match = text.match(/\{[\s\S]*\}/);
  if (!match) {
    return {
      casual: "（候補生成に失敗しました。もう一度お試しください）",
      polite: "（候補生成に失敗しました。もう一度お試しください）",
      brief:  "（候補生成に失敗しました。もう一度お試しください）"
    };
  }
  return JSON.parse(match[0]);
}
