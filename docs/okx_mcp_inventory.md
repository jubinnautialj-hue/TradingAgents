# Exposed OKX MCP tools — 2026-10-22

Metadata inventory only; no credentials or account results. Server capabilities:
version 1.4.9; hasAuth=true; demo=false; readOnly=false. Actual API-key permissions
were not inspected. Only system_get_capabilities was called during assessment.
TradingAgents does not connect to any of these tools.

## market

- `market_filter` — read/query — not invoked (except capabilities)
- `market_filter_oi_change` — read/query — not invoked (except capabilities)
- `market_get_candles` — read/query — not invoked (except capabilities)
- `market_get_funding_rate` — read/query — not invoked (except capabilities)
- `market_get_index_candles` — read/query — not invoked (except capabilities)
- `market_get_index_ticker` — read/query — not invoked (except capabilities)
- `market_get_indicator` — read/query — not invoked (except capabilities)
- `market_get_instruments` — read/query — not invoked (except capabilities)
- `market_get_instruments_by_category` — read/query — not invoked (except capabilities)
- `market_get_mark_price` — read/query — not invoked (except capabilities)
- `market_get_oi_history` — read/query — not invoked (except capabilities)
- `market_get_open_interest` — read/query — not invoked (except capabilities)
- `market_get_orderbook` — read/query — not invoked (except capabilities)
- `market_get_pair_spread` — read/query — not invoked (except capabilities)
- `market_get_price_limit` — read/query — not invoked (except capabilities)
- `market_get_stock_tokens` — read/query — not invoked (except capabilities)
- `market_get_ticker` — read/query — not invoked (except capabilities)
- `market_get_tickers` — read/query — not invoked (except capabilities)
- `market_get_trades` — read/query — not invoked (except capabilities)
- `market_list_indicators` — read/query — not invoked (except capabilities)

## account

- `account_get_asset_balance` — read/query — not invoked (except capabilities)
- `account_get_balance` — read/query — not invoked (except capabilities)
- `account_get_balance_all` — read/query — not invoked (except capabilities)
- `account_get_bills` — read/query — not invoked (except capabilities)
- `account_get_bills_archive` — read/query — not invoked (except capabilities)
- `account_get_config` — read/query — not invoked (except capabilities)
- `account_get_max_avail_size` — read/query — not invoked (except capabilities)
- `account_get_max_size` — read/query — not invoked (except capabilities)
- `account_get_max_withdrawal` — read/query — not invoked (except capabilities)
- `account_get_positions` — read/query — not invoked (except capabilities)
- `account_get_positions_history` — read/query — not invoked (except capabilities)
- `account_get_trade_fee` — read/query — not invoked (except capabilities)
- `account_set_position_mode` — WRITE — prohibited
- `account_transfer` — WRITE — prohibited

## spot

- `spot_amend_algo_order` — WRITE — prohibited
- `spot_amend_order` — WRITE — prohibited
- `spot_batch_amend` — WRITE — prohibited
- `spot_batch_cancel` — WRITE — prohibited
- `spot_batch_orders` — WRITE — prohibited
- `spot_cancel_algo_order` — WRITE — prohibited
- `spot_cancel_order` — WRITE — prohibited
- `spot_get_algo_orders` — read/query — not invoked (except capabilities)
- `spot_get_fills` — read/query — not invoked (except capabilities)
- `spot_get_order` — read/query — not invoked (except capabilities)
- `spot_get_orders` — read/query — not invoked (except capabilities)
- `spot_place_algo_order` — WRITE — prohibited
- `spot_place_order` — WRITE — prohibited
- `spot_set_leverage` — WRITE — prohibited

## swap

- `swap_amend_algo_order` — WRITE — prohibited
- `swap_batch_amend` — WRITE — prohibited
- `swap_batch_cancel` — WRITE — prohibited
- `swap_batch_orders` — WRITE — prohibited
- `swap_cancel_algo_orders` — WRITE — prohibited
- `swap_cancel_order` — WRITE — prohibited
- `swap_close_position` — WRITE — prohibited
- `swap_get_algo_orders` — read/query — not invoked (except capabilities)
- `swap_get_fills` — read/query — not invoked (except capabilities)
- `swap_get_leverage` — read/query — not invoked (except capabilities)
- `swap_get_order` — read/query — not invoked (except capabilities)
- `swap_get_orders` — read/query — not invoked (except capabilities)
- `swap_get_positions` — read/query — not invoked (except capabilities)
- `swap_place_algo_order` — WRITE — prohibited
- `swap_place_move_stop_order` — WRITE — prohibited
- `swap_place_order` — WRITE — prohibited
- `swap_set_leverage` — WRITE — prohibited

## system

- `system_get_capabilities` — read/query — not invoked (except capabilities)

## trade

- `trade_get_history` — read/query — not invoked (except capabilities)


