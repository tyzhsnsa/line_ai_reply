"""Bybitテストネット向けの自動売買スクリプト。

Gemini APIを用いて売買シグナルを判定し、BybitのWebSocketで取得した
BTCUSDTの1分足・5分足・1時間足データを基にエントリーを行う。
マルチタイムフレーム分析にRSIや出来高を含め、ボラティリティに応じた
利確・損切り調整およびLINE通知を行う。
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

# ボラティリティが計算できない場合の利確・損切り割合（フォールバック）
FALLBACK_TAKE_PROFIT_PERCENT = 0.2 / 100  # 0.2%
FALLBACK_STOP_LOSS_PERCENT = 0.1 / 100  # 0.1%

# ATRを用いたボラティリティ調整の設定
ATR_PERIOD = 14
ATR_TAKE_PROFIT_MULTIPLIER = 1.8
ATR_STOP_LOSS_MULTIPLIER = 1.0

# 分析に使用するタイムフレームと保持する足本数
TIMEFRAME_CONFIG = {
    "1": {"label": "1分足", "max_candles": 60},
    "5": {"label": "5分足", "max_candles": 48},
    "60": {"label": "1時間足", "max_candles": 48},
}

# テクニカル指標計算用のパラメータ
RSI_PERIOD = 14
VOLUME_LOOKBACK = 20

# WebSocketのエンドポイント（USDT無期限テストネット）
BYBIT_WS_ENDPOINT = "wss://stream-testnet.bybit.com/v5/public/linear"

# REST APIエンドポイント
BYBIT_REST_ENDPOINT = "https://api-testnet.bybit.com"

# Geminiモデル名
GEMINI_MODEL = "gemini-1.5-flash"

# LINE Notifyのトークン
LINE_NOTIFY_TOKEN = "YOUR_LINE_NOTIFY_TOKEN"
LINE_NOTIFY_ENDPOINT = "https://notify-api.line.me/api/notify"

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


def calculate_rsi(candles: List[Candle], period: int = RSI_PERIOD) -> Optional[float]:
    """指定期間のRSIを計算する。"""

    if len(candles) <= period:
        return None

    gains: List[float] = []
    losses: List[float] = []

    for i in range(1, len(candles)):
        change = candles[i].close - candles[i - 1].close
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calculate_average_volume(
    candles: List[Candle], lookback: int = VOLUME_LOOKBACK
) -> Optional[float]:
    """出来高の移動平均を計算する。"""

    if not candles:
        return None

    sample = candles[-min(len(candles), lookback) :]
    if not sample:
        return None

    return sum(candle.volume for candle in sample) / len(sample)


def calculate_atr(candles: List[Candle], period: int = ATR_PERIOD) -> Optional[float]:
    """ATR（平均真のレンジ）を計算する。"""

    if len(candles) <= period:
        return None

    true_ranges: List[float] = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous = candles[i - 1]
        high_low = current.high - current.low
        high_close = abs(current.high - previous.close)
        low_close = abs(current.low - previous.close)
        true_ranges.append(max(high_low, high_close, low_close))

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / period


class GeminiSignalProvider:
    """Gemini APIを用いて売買シグナルを取得するクラス。"""

    def __init__(self, api_key: str, model: str) -> None:
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(model)

    @staticmethod
    def _format_number(value: Optional[float]) -> str:
        """数値を表示用に整形する。"""

        if value is None:
            return "N/A"
        if abs(value) >= 1000:
            return f"{value:.0f}"
        if abs(value) >= 100:
            return f"{value:.1f}"
        return f"{value:.2f}"

    def _build_prompt(
        self,
        candles_by_tf: Dict[str, List[Candle]],
        indicators: Dict[str, Dict[str, Optional[float]]],
    ) -> str:
        """AIに渡すプロンプトを生成する。"""

        lines = [
            "あなたは短期トレード向けのアナリストです。",
            "以下のBTCUSDTマルチタイムフレームデータを分析し、総合判断を行ってください。",
            "出力は必ず `BUY` `SELL` `WAIT` のいずれか一語にしてください。",
        ]

        for timeframe, candles in candles_by_tf.items():
            config = TIMEFRAME_CONFIG.get(timeframe, {})
            label = config.get("label", f"{timeframe}分足")
            tf_indicators = indicators.get(timeframe, {})
            volume_sample = min(len(candles), VOLUME_LOOKBACK)
            rsi_text = self._format_number(tf_indicators.get("rsi"))
            avg_volume_text = self._format_number(tf_indicators.get("avg_volume"))
            latest_volume_text = self._format_number(tf_indicators.get("latest_volume"))

            lines.append(
                f"[{label}] RSI={rsi_text} 平均出来高(直近{volume_sample}本)={avg_volume_text} 最新出来高={latest_volume_text}"
            )
            lines.append("timestamp,open,high,low,close,volume")

            max_candles = config.get("max_candles", 20)
            for candle in candles[-max_candles:]:
                lines.append(
                    f"{int(candle.start)},{candle.open},{candle.high},{candle.low},{candle.close},{candle.volume}"
                )

        lines.append(
            "短期は1分足でタイミングを取りつつ、5分足と1時間足のトレンド方向を確認して判断してください。"
        )
        lines.append(
            "強い上昇トレンドであればBUY、強い下降トレンドであればSELL、明確でなければWAITと回答してください。"
        )

        return "\n".join(lines)

    def get_signal(
        self,
        candles_by_tf: Dict[str, List[Candle]],
        indicators: Dict[str, Dict[str, Optional[float]]],
    ) -> str:
        """ローソク足データから売買シグナルを取得する。"""

        if not candles_by_tf or not candles_by_tf.get("1"):
            return "WAIT"

        prompt = self._build_prompt(candles_by_tf, indicators)

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


class LineNotifier:
    """LINE Notifyを利用して通知を送信するクラス。"""

    def __init__(self, token: str) -> None:
        self._token = token.strip()

    @property
    def is_configured(self) -> bool:
        """トークンが設定済みかどうかを返す。"""

        return bool(self._token) and "YOUR_" not in self._token

    def send_message(self, message: str) -> None:
        """LINE Notifyにメッセージを送信する。"""

        if not self.is_configured:
            logging.debug("LINE Notifyトークンが設定されていないため通知をスキップします。")
            return

        headers = {"Authorization": f"Bearer {self._token}"}
        data = {"message": message}

        try:
            response = requests.post(
                LINE_NOTIFY_ENDPOINT, headers=headers, data=data, timeout=10
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logging.error("LINE通知の送信に失敗しました: %s", exc)


class TradeManager:
    """売買ロジック全体を管理するクラス。"""

    def __init__(
        self,
        signal_provider: GeminiSignalProvider,
        client: BybitClient,
        notifier: Optional[LineNotifier],
        timeframe_config: Dict[str, Dict[str, int]],
    ) -> None:
        self.signal_provider = signal_provider
        self.client = client
        self.notifier = notifier
        self.timeframe_config = timeframe_config
        self.candles_by_tf: Dict[str, List[Candle]] = {
            tf: [] for tf in timeframe_config
        }
        self.current_position: Optional[str] = None

    def on_new_candle(self, timeframe: str, candle_data: Dict) -> None:
        """WebSocketからのローソク足データを処理する。"""

        if timeframe not in self.candles_by_tf:
            return

        candle = Candle(
            start=float(candle_data["start"]) / 1000,
            open=float(candle_data["open"]),
            high=float(candle_data["high"]),
            low=float(candle_data["low"]),
            close=float(candle_data["close"]),
            volume=float(candle_data["volume"]),
        )

        self.candles_by_tf[timeframe].append(candle)
        max_candles = self.timeframe_config.get(timeframe, {}).get("max_candles", 60)
        self.candles_by_tf[timeframe] = self.candles_by_tf[timeframe][-max_candles:]

        if timeframe != "1":
            return

        if not self._has_all_timeframes():
            logging.debug("必要なタイムフレームが揃っていないため判断を保留します。")
            return

        indicators = self._prepare_indicators()
        signal = self.signal_provider.get_signal(self.candles_by_tf, indicators)
        logging.info("AIシグナル: %s", signal)

        if signal == "WAIT":
            return

        if self.current_position == signal:
            logging.debug("既に同方向のポジションを保有中のためスキップ: %s", signal)
            return

        latest_price = candle.close
        take_profit, stop_loss, atr = self._calc_tp_sl(signal, latest_price)

        order_response = self.client.place_order(
            side=signal,
            qty=ORDER_QTY,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )

        if order_response:
            self.current_position = signal
            logging.info(
                "エントリーしました: %s 価格=%.2f TP=%.2f SL=%.2f",
                signal,
                latest_price,
                take_profit,
                stop_loss,
            )
            logging.debug("Bybitレスポンス: %s", order_response)
            self._notify_position(signal, latest_price, take_profit, stop_loss, indicators, atr)

    def _has_all_timeframes(self) -> bool:
        """必要なタイムフレームのデータが揃っているか確認する。"""

        return all(self.candles_by_tf.get(tf) for tf in self.candles_by_tf)

    def _prepare_indicators(self) -> Dict[str, Dict[str, Optional[float]]]:
        """AIに渡すインジケーターを計算する。"""

        indicators: Dict[str, Dict[str, Optional[float]]] = {}
        for timeframe, candles in self.candles_by_tf.items():
            indicators[timeframe] = {
                "rsi": calculate_rsi(candles, RSI_PERIOD),
                "avg_volume": calculate_average_volume(candles, VOLUME_LOOKBACK),
                "latest_volume": candles[-1].volume if candles else None,
            }

        return indicators

    def _calc_tp_sl(self, side: str, price: float) -> Tuple[float, float, Optional[float]]:
        """利確・損切り価格を計算する。"""

        atr = calculate_atr(self.candles_by_tf.get("1", []), ATR_PERIOD)

        if atr:
            if side == "BUY":
                take_profit = price + atr * ATR_TAKE_PROFIT_MULTIPLIER
                stop_loss = price - atr * ATR_STOP_LOSS_MULTIPLIER
            else:
                take_profit = price - atr * ATR_TAKE_PROFIT_MULTIPLIER
                stop_loss = price + atr * ATR_STOP_LOSS_MULTIPLIER
        else:
            if side == "BUY":
                take_profit = price * (1 + FALLBACK_TAKE_PROFIT_PERCENT)
                stop_loss = price * (1 - FALLBACK_STOP_LOSS_PERCENT)
            else:
                take_profit = price * (1 - FALLBACK_TAKE_PROFIT_PERCENT)
                stop_loss = price * (1 + FALLBACK_STOP_LOSS_PERCENT)

        return take_profit, stop_loss, atr

    @staticmethod
    def _format_indicator(value: Optional[float]) -> str:
        """通知用に数値を整形する。"""

        if value is None:
            return "N/A"
        if abs(value) >= 1000:
            return f"{value:.0f}"
        if abs(value) >= 100:
            return f"{value:.1f}"
        return f"{value:.2f}"

    def _notify_position(
        self,
        side: str,
        price: float,
        take_profit: float,
        stop_loss: float,
        indicators: Dict[str, Dict[str, Optional[float]]],
        atr: Optional[float],
    ) -> None:
        """ポジション情報をLINEに通知する。"""

        if not self.notifier or not self.notifier.is_configured:
            return

        lines = [
            f"{SYMBOL} {side} エントリー",  # ポジションの方向
            f"価格: {price:.2f}",
            f"TP: {take_profit:.2f} / SL: {stop_loss:.2f}",
        ]

        if atr is not None:
            lines.append(f"ATR({ATR_PERIOD})={atr:.2f}")

        for timeframe in self.timeframe_config:
            if timeframe not in indicators:
                continue

            label = TIMEFRAME_CONFIG.get(timeframe, {}).get("label", f"{timeframe}分足")
            tf_data = indicators[timeframe]
            rsi_text = self._format_indicator(tf_data.get("rsi"))
            avg_volume_text = self._format_indicator(tf_data.get("avg_volume"))
            latest_volume_text = self._format_indicator(tf_data.get("latest_volume"))
            lines.append(
                f"{label} RSI={rsi_text} AVG_VOL={avg_volume_text} VOL={latest_volume_text}"
            )

        message = "\n".join(lines)
        self.notifier.send_message(message)


class BybitWebSocket:
    """BybitのWebSocketクライアントを管理するクラス。"""

    def __init__(
        self,
        endpoint: str,
        symbol: str,
        trade_manager: TradeManager,
        timeframes: List[str],
    ) -> None:
        self.endpoint = endpoint
        self.symbol = symbol
        self.trade_manager = trade_manager
        self.timeframes = list(dict.fromkeys(timeframes))
        self.ws: Optional[websocket.WebSocketApp] = None

    def start(self) -> None:
        """WebSocket接続を開始する。"""

        def on_open(ws: websocket.WebSocketApp) -> None:  # pylint: disable=unused-argument
            logging.info("WebSocket接続が確立されました。")
            subscribe_args = [f"kline.{tf}.{self.symbol}" for tf in self.timeframes]
            subscribe_msg = json.dumps({"op": "subscribe", "args": subscribe_args})
            ws.send(subscribe_msg)
            logging.info("ローソク足データを購読しました: %s", subscribe_msg)

        def on_message(ws: websocket.WebSocketApp, message: str) -> None:  # pylint: disable=unused-argument
            data = json.loads(message)

            topic = data.get("topic")
            if not topic or not topic.startswith("kline."):
                return

            parts = topic.split(".")
            if len(parts) < 3:
                return

            timeframe = parts[1]
            if timeframe not in self.timeframes or parts[2] != self.symbol:
                return

            for candle_data in data.get("data", []):
                if candle_data.get("confirm"):
                    self.trade_manager.on_new_candle(timeframe, candle_data)

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
    notifier = LineNotifier(LINE_NOTIFY_TOKEN)

    if not notifier.is_configured:
        logging.warning("LINE Notifyトークンが設定されていません。通知は送信されません。")

    timeframes = list(TIMEFRAME_CONFIG.keys())
    if "1" not in timeframes:
        logging.error("TIMEFRAME_CONFIGに1分足が含まれていません。")
        sys.exit(1)

    trade_manager = TradeManager(signal_provider, client, notifier, TIMEFRAME_CONFIG)
    ws_client = BybitWebSocket(BYBIT_WS_ENDPOINT, SYMBOL, trade_manager, timeframes)

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
