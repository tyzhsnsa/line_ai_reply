"""Bybitテストネット向けの自動売買スクリプト。

Gemini APIを用いて売買シグナルを判定し、BybitのWebSocketで取得した
BTCUSDTの1分足データを基にエントリーを行う。
"""

import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import google.generativeai as genai
import requests
import websocket


# ===================== ユーザー設定エリア =====================
# BybitテストネットのAPIキーとシークレットを設定する
BYBIT_API_KEY = "YOUR_BYBIT_TESTNET_API_KEY"
BYBIT_API_SECRET = "YOUR_BYBIT_TESTNET_API_SECRET"

# Gemini APIキーを設定する
GEMINI_API_KEY = "YOUR_GEMINI_API_KEY"

# 取引する銘柄や数量などの設定
SYMBOL = "BTCUSDT"
ORDER_QTY = 0.001  # 取引数量（例）

# 利確・損切りの割合
TAKE_PROFIT_PERCENT = 0.2 / 100  # 0.2%
STOP_LOSS_PERCENT = 0.1 / 100  # 0.1%

# WebSocketのエンドポイント（USDT無期限テストネット）
BYBIT_WS_ENDPOINT = "wss://stream-testnet.bybit.com/v5/public/linear"

# REST APIエンドポイント
BYBIT_REST_ENDPOINT = "https://api-testnet.bybit.com"

# Geminiモデル名
GEMINI_MODEL = "gemini-1.5-flash"

# 直近の足データを何本分AIに渡すか
MAX_CANDLES_FOR_AI = 20

# ==============================================================


@dataclass
class Candle:
    """ローソク足を保持するデータクラス。"""

    start: float
    open: float
    high: float
    low: float
    close: float
    volume: float


class GeminiSignalProvider:
    """Gemini APIを用いて売買シグナルを取得するクラス。"""

    def __init__(self, api_key: str, model: str) -> None:
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model)

    def _build_prompt(self, candles: List[Candle]) -> str:
        """AIに渡すプロンプトを生成する。"""

        lines = [
            "あなたは短期トレード向けのアナリストです。",
            "以下のBTCUSDTの1分足データを読み取り、直近の値動きを分析してください。",
            "出力は必ず `BUY` `SELL` `WAIT` のいずれか一語にしてください。",
        ]

        lines.append("timestamp,open,high,low,close,volume")
        for candle in candles:
            lines.append(
                f"{candle.start},{candle.open},{candle.high},{candle.low},{candle.close},{candle.volume}"
            )

        lines.append(
            "短期の順張り戦略を想定し、強い上昇ならBUY、強い下落ならSELL、判断が難しい場合はWAITとしてください。"
        )

        return "\n".join(lines)

    def get_signal(self, candles: List[Candle]) -> str:
        """ローソク足データから売買シグナルを取得する。"""

        if not candles:
            return "WAIT"

        prompt = self._build_prompt(candles)

        try:
            response = self._model.generate_content(prompt)
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Gemini APIの呼び出しに失敗しました: %s", exc)
            return "WAIT"

        if not response or not response.text:
            return "WAIT"

        decision = response.text.strip().upper()
        if decision not in {"BUY", "SELL", "WAIT"}:
            logging.warning("Geminiから未知の応答を受け取りました: %s", decision)
            return "WAIT"

        return decision


class BybitClient:
    """Bybit REST APIで注文を発注するクライアント。"""

    def __init__(self, api_key: str, api_secret: str, endpoint: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.endpoint = endpoint.rstrip("/")

    def _sign(self, timestamp: int, recv_window: int, body: Dict) -> str:
        """Bybitの署名を生成する。"""

        import hashlib
        import hmac

        payload = (
            str(timestamp)
            + self.api_key
            + str(recv_window)
            + json.dumps(body, separators=(",", ":"))
        )
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def place_order(
        self,
        side: str,
        qty: float,
        take_profit: float,
        stop_loss: float,
    ) -> Optional[Dict]:
        """マーケット注文をBybitに送信する。"""

        url = f"{self.endpoint}/v5/order/create"
        timestamp = int(time.time() * 1000)
        recv_window = 5000

        body = {
            "category": "linear",
            "symbol": SYMBOL,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "takeProfit": str(round(take_profit, 2)),
            "stopLoss": str(round(stop_loss, 2)),
            "tpslMode": "Full",
            "timeInForce": "GoodTillCancel",
        }

        signature = self._sign(timestamp, recv_window, body)

        headers = {
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": str(recv_window),
            "X-BAPI-SIGN": signature,
        }

        try:
            response = requests.post(url, headers=headers, json=body, timeout=10)
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.error("Bybit注文リクエストに失敗しました: %s", exc)
            return None

        data = response.json()
        if data.get("retCode") != 0:
            logging.error("Bybitからエラーが返されました: %s", data)
            return None

        return data


class TradeManager:
    """売買ロジック全体を管理するクラス。"""

    def __init__(self, signal_provider: GeminiSignalProvider, client: BybitClient) -> None:
        self.signal_provider = signal_provider
        self.client = client
        self.candles: List[Candle] = []
        self.current_position: Optional[str] = None

    def on_new_candle(self, candle_data: Dict) -> None:
        """WebSocketからのローソク足データを処理する。"""

        candle = Candle(
            start=float(candle_data["start"]) / 1000,
            open=float(candle_data["open"]),
            high=float(candle_data["high"]),
            low=float(candle_data["low"]),
            close=float(candle_data["close"]),
            volume=float(candle_data["volume"]),
        )

        self.candles.append(candle)
        self.candles = self.candles[-MAX_CANDLES_FOR_AI :]

        signal = self.signal_provider.get_signal(self.candles)
        logging.info("AIシグナル: %s", signal)

        if signal == "WAIT":
            return

        if self.current_position == signal:
            logging.debug("既に同方向のポジションを保有中のためスキップ: %s", signal)
            return

        latest_price = candle.close
        take_profit, stop_loss = self._calc_tp_sl(signal, latest_price)

        order_response = self.client.place_order(
            side=signal,
            qty=ORDER_QTY,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

        if order_response:
            self.current_position = signal
            logging.info(
                "エントリーしました: %s 価格=%.2f TP=%.2f SL=%.2f", signal, latest_price, take_profit, stop_loss
            )
            logging.debug("Bybitレスポンス: %s", order_response)

    def _calc_tp_sl(self, side: str, price: float) -> Tuple[float, float]:
        """利確・損切り価格を計算する。"""

        if side == "BUY":
            take_profit = price * (1 + TAKE_PROFIT_PERCENT)
            stop_loss = price * (1 - STOP_LOSS_PERCENT)
        else:
            take_profit = price * (1 - TAKE_PROFIT_PERCENT)
            stop_loss = price * (1 + STOP_LOSS_PERCENT)

        return take_profit, stop_loss


class BybitWebSocket:
    """BybitのWebSocketクライアントを管理するクラス。"""

    def __init__(self, endpoint: str, symbol: str, trade_manager: TradeManager) -> None:
        self.endpoint = endpoint
        self.symbol = symbol
        self.trade_manager = trade_manager
        self.ws: Optional[websocket.WebSocketApp] = None

    def start(self) -> None:
        """WebSocket接続を開始する。"""

        def on_open(ws: websocket.WebSocketApp) -> None:  # pylint: disable=unused-argument
            logging.info("WebSocket接続が確立されました。")
            subscribe_msg = json.dumps(
                {
                    "op": "subscribe",
                    "args": [f"kline.1.{self.symbol}"],
                }
            )
            ws.send(subscribe_msg)
            logging.info("1分足データを購読しました: %s", subscribe_msg)

        def on_message(ws: websocket.WebSocketApp, message: str) -> None:  # pylint: disable=unused-argument
            data = json.loads(message)

            if "topic" not in data:
                return

            if data.get("topic") != f"kline.1.{self.symbol}":
                return

            for candle_data in data.get("data", []):
                if candle_data.get("confirm"):
                    self.trade_manager.on_new_candle(candle_data)

        def on_error(ws: websocket.WebSocketApp, error: Exception) -> None:  # pylint: disable=unused-argument
            logging.error("WebSocketエラー: %s", error)

        def on_close(
            ws: websocket.WebSocketApp, close_status_code: int, close_msg: str  # pylint: disable=unused-argument
        ) -> None:
            logging.warning("WebSocket接続が終了しました: %s %s", close_status_code, close_msg)

        self.ws = websocket.WebSocketApp(
            self.endpoint,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )

        wst = threading.Thread(target=self.ws.run_forever, kwargs={"ping_interval": 20}, daemon=True)
        wst.start()

    def stop(self) -> None:
        """WebSocket接続を停止する。"""

        if self.ws:
            self.ws.close()


def setup_logger() -> None:
    """ロガーの初期設定を行う。"""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    """エントリーポイント。"""

    setup_logger()

    if "YOUR_BYBIT_TESTNET_API_KEY" in BYBIT_API_KEY or "YOUR_GEMINI_API_KEY" in GEMINI_API_KEY:
        logging.error("APIキーを設定してください。")
        sys.exit(1)

    signal_provider = GeminiSignalProvider(GEMINI_API_KEY, GEMINI_MODEL)
    client = BybitClient(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_REST_ENDPOINT)
    trade_manager = TradeManager(signal_provider, client)
    ws_client = BybitWebSocket(BYBIT_WS_ENDPOINT, SYMBOL, trade_manager)

    ws_client.start()

    def shutdown_handler(signum, frame):  # pylint: disable=unused-argument
        logging.info("終了シグナルを受け取りました。シャットダウンします。")
        ws_client.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # メインスレッドを維持するためのループ
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
