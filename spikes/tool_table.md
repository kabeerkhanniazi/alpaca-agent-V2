
#### `account`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_account_activities` | READ |  | Returns a list of account activities such as fills, dividends, and transfers. |
| `get_account_activities_by_type` | READ |  | Returns account activity entries for a specific type of activity. |
| `get_account_config` | READ |  | Retrieves the current account configuration settings, including trading restrictions, margin se… |
| `get_account_info` | READ | ✅ | Retrieves and formats the current account information including balances and status. |
| `get_portfolio_history` | READ |  | Retrieves account portfolio history (equity and P/L) over a requested time window. |
| `update_account_config` | **WRITE** |  | Updates one or more account configuration settings. Only the fields you provide will be changed… |

#### `trading`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `cancel_all_orders` | **WRITE** |  | Cancel all open orders. |
| `cancel_order_by_id` | **WRITE** |  | Cancel a specific order by its ID. |
| `close_all_positions` | **WRITE** |  | Closes all open positions by placing sell orders for each. If the market is closed, the sell or… |
| `close_position` | **WRITE** |  | Closes a specific position for a single symbol by placing a sell order. If the market is closed… |
| `do_not_exercise_options_position` | **WRITE** |  | Submits a do-not-exercise instruction for a held option contract. |
| `exercise_options_position` | **WRITE** |  | Exercises a held option contract, converting it into the underlying asset. |
| `get_all_positions` | READ | ✅ | Retrieves all current positions in the portfolio as JSON. |
| `get_open_position` | READ | ✅ | Retrieves and formats details for a specific open position. |
| `get_order_by_client_id` | READ |  | Retrieves a single order specified by the client order ID. Note: if the order was replaced, thi… |
| `get_order_by_id` | READ |  | Retrieves a single order by its ID. |
| `get_orders` | READ | ✅ | Retrieves and formats orders with the specified filters. |
| `place_crypto_order` | **WRITE** |  | Place a cryptocurrency order. |
| `place_option_order` | **WRITE** |  | Place an options order (single-leg or multi-leg). For single-leg orders, provide symbol, side, … |
| `place_stock_order` | **WRITE** |  | Place a stock or ETF order. |
| `replace_order_by_id` | **WRITE** |  | Replaces an existing open order with updated parameters. At least one optional field must be pr… |

#### `assets`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_all_assets` | READ |  | Get all available assets with optional filtering. WARNING: The unfiltered response is very larg… |
| `get_asset` | READ |  | Retrieves and formats detailed information about a specific asset. |
| `get_calendar` | READ |  | Retrieves and formats market calendar for specified date range. WARNING: Always provide start a… |
| `get_clock` | READ | ✅ | Retrieves and formats current market status and next open/close times. |
| `get_corporate_action_announcement` | READ |  | Retrieves a single corporate action announcement by ID. |
| `get_corporate_action_announcements` | READ |  | Retrieves corporate action announcements (dividends, mergers, splits, spinoffs). Use a narrow d… |
| `get_option_contract` | READ |  | Retrieves a single option contract by symbol or contract ID. |
| `get_option_contracts` | READ | ✅ | Retrieves option contracts for underlying symbol(s). |

#### `stock-data`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_crypto_bars` | READ |  | Retrieve historical price bars (OHLCV) for one or more cryptocurrencies. When start is omitted,… |
| `get_crypto_quotes` | READ |  | Retrieve historical bid/ask quotes for one or more cryptocurrencies. When start is omitted, it … |
| `get_crypto_trades` | READ |  | Retrieve historical trade data for one or more cryptocurrencies. When start is omitted, it is a… |
| `get_market_movers` | READ |  | Returns the top market movers (gainers and losers) based on real-time SIP data. |
| `get_most_active_stocks` | READ |  | Screens the market for most active stocks by volume or trade count. |
| `get_stock_bars` | READ | ✅ | Retrieve historical price bars (OHLCV) for one or more stocks. When start is omitted, it is aut… |
| `get_stock_latest_bar` | READ |  | Get the latest minute bar for one or more stocks. |
| `get_stock_latest_quote` | READ |  | Retrieves and formats the latest quote for one or more stocks. |
| `get_stock_latest_trade` | READ | ✅ | Get the latest trade for one or more stocks. |
| `get_stock_quotes` | READ |  | Retrieve historical bid/ask quotes (level 1) for one or more stocks. When start is omitted, it … |
| `get_stock_snapshot` | READ | ✅ | Retrieves comprehensive snapshots of stock symbols including latest trade, quote, minute bar, d… |
| `get_stock_trades` | READ |  | Retrieve historical trade data for one or more stocks. When start is omitted, it is automatical… |

#### `options-data`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_option_bars` | READ |  | Retrieves historical bar (OHLCV) data for one or more option contracts. |
| `get_option_chain` | READ | ✅ | Retrieves option chain data for an underlying symbol, including latest trade, quote, implied vo… |
| `get_option_exchange_codes` | READ |  | Retrieves the mapping of exchange codes to exchange names for option market data. Useful for in… |
| `get_option_latest_quote` | READ |  | Retrieves and formats the latest quote for one or more option contracts including bid/ask price… |
| `get_option_latest_trade` | READ |  | Retrieves the latest trade for one or more option contracts. |
| `get_option_snapshot` | READ | ✅ | Retrieves comprehensive snapshots of option contracts including latest trade, quote, implied vo… |
| `get_option_trades` | READ |  | Retrieves historical trade data for one or more option contracts. |

#### `crypto-data`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_crypto_latest_bar` | READ |  | Returns the latest minute bar for one or more crypto symbols. The loc parameter is required — a… |
| `get_crypto_latest_orderbook` | READ |  | Returns the latest orderbook for one or more crypto symbols. The loc parameter is required — al… |
| `get_crypto_latest_quote` | READ |  | Returns the latest quote for one or more crypto symbols. The loc parameter is required — always… |
| `get_crypto_latest_trade` | READ |  | Returns the latest trade for one or more crypto symbols. The loc parameter is required — always… |
| `get_crypto_snapshot` | READ |  | Returns a snapshot for one or more crypto symbols including latest trade, quote, minute bar, da… |

#### `watchlists`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `add_asset_to_watchlist_by_id` | **WRITE** |  | Add an asset by symbol to a specific watchlist. |
| `create_watchlist` | **WRITE** |  | Creates a new watchlist with specified symbols. |
| `delete_watchlist_by_id` | **WRITE** |  | Delete a specific watchlist by its ID. |
| `get_watchlist_by_id` | READ |  | Get a specific watchlist by its ID. |
| `get_watchlists` | READ |  | Get all watchlists for the account. |
| `remove_asset_from_watchlist_by_id` | **WRITE** |  | Remove an asset by symbol from a specific watchlist. |
| `update_watchlist_by_id` | **WRITE** |  | Update an existing watchlist. IMPORTANT: this replaces the entire watchlist. You must include t… |

#### `corporate-actions`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_corporate_actions` | READ |  | Retrieves and formats corporate action announcements. |

#### `news`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_news` | READ |  | Retrieves news articles for stocks and crypto. Filter by symbols, date range, and sort order. R… |

#### `fixed-income-data`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_fixed_income_latest_quotes` | READ |  | Returns the latest quotes for fixed income securities (bonds, treasuries). Provide a comma-sepa… |

#### `index-data`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `get_index_latest_values` | READ |  | Returns the latest values for market indices (e.g. SPX, VIX, DJI). Provide a comma-separated li… |
| `get_index_values` | READ |  | Returns historical values for market indices over a time interval. Supports pagination, sorting… |

#### `(none)`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `create_locate` | **WRITE** |  | Creates a locate request for a short sale. Requires a symbol and quantity. Optionally set a lim… |
| `get_locate` | READ |  | Returns a single locate request by its ID. |
| `get_locate_quotes` | READ |  | Returns locate availability and pricing for one or more symbols. Provide a comma-separated list… |
| `get_locates` | READ |  | Returns locate requests for the account, filtered by status, symbol, or date range. Results are… |

#### `(always-on)`

| Tool | R/W | Allowlisted | Description |
|---|---|---|---|
| `fetch_alpaca_doc` | READ |  | Fetch one Alpaca ReadMe documentation page by page ID. Use after search_alpaca_docs returns a r… |
| `get_alpaca_endpoint_docs` | READ |  | Fetch reference docs for one exact Alpaca API endpoint by method and path. Use when the endpoin… |
| `list_alpaca_api_endpoints` | READ |  | List endpoints for one allowed Alpaca OpenAPI spec. Use when browsing the available endpoint in… |
| `search_alpaca_api_specs` | READ |  | Search Alpaca API reference endpoints by topic, operation, path fragment, parameter, or schema … |
| `search_alpaca_docs` | READ |  | Search Alpaca documentation pages and guides. Use for conceptual or product questions about set… |

<!-- totals: 57 READ / 17 WRITE / 74 total -->
