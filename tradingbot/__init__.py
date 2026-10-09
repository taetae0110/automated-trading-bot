"""tradingbot - 주식/코인 자동매매 봇.

구성 요소
- brokers   : 거래소/증권사 어댑터 (Upbit, ccxt(Binance 등), KIS 한국투자증권, Alpaca, Paper 모의)
- strategies: 매매 전략 (SMA 교차, RSI, 볼린저, MACD, 변동성 돌파)
- risk      : 포지션 사이징, 손절/익절, 일일 손실 한도
- backtest  : 백테스터 + 성과 지표
- engine    : 실시간(모의/실거래) 매매 루프
- notify    : 텔레그램/슬랙/디스코드 알림
"""

__version__ = "0.1.0"
