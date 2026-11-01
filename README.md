# LP Monitor

Read-only multi-chain LP position monitor for EVM wallets. Tracks Uniswap v3/v4, PancakeSwap v3, Aerodrome/Velodrome Slipstream, and SushiSwap v3 positions across 7 chains.

## Features

- **Position tracking**: OPEN/CLOSE/ADD/REMOVE detection via `lp_checker.py`
- **Range monitoring**: tick position %, in-range/out-of-range status
- **MC range**: converts ticks to token prices × total supply
- **IL & PnL**: impermanent loss and floating PnL from on-chain entry data (IncreaseLiquidity/ModifyLiquidity events — never asks for manual input)
- **Auto-notifications**: range transitions, <10% from range edge, new/closed positions
- **Live prices**: DefiLlama primary, DexScreener fallback
- **Read-only**: never signs or sends transactions

## Usage

```bash
# Via environment variable
LP_WALLET=0xYourWallet python3 monitor.py

# Via flag
python3 monitor.py --wallet 0xYourWallet

# One-shot report of all positions
python3 monitor.py --wallet 0xYourWallet --report
```

## Requirements

- Python 3.8+
- `lp_checker.py` in `../lp-checker/` (or set path in `CHECKER`)
- No API keys required

## State

- `state.json` — position entries, history (gitignored)
- `notifications.log` — alert log (gitignored)

## Report Format

```
🟢 USDG/INU · 1.9% · Uniswap v4 · Robinhood · #4113660
IN RANGE · 28.6%
|—————●———————————————|
MC $1.90M → $941.4K · now $1.56M
Value $367.89 · Fees $3.74
IL -3.00% · Float PnL $-7.51 (-2.0%)
🕒 18:49 WIB
```

## Chains

Ethereum, BSC, Base, Arbitrum, Optimism, Polygon, Robinhood Chain (4663)

## License

MIT
