#!/usr/bin/env python3
"""
LP Monitor v3 — user spec (2026-11-01 18:43 WIB):
- Read-only. Never signs transactions.
- Per posisi open, tiap 1 menit:
  1. Range: tickLower, tickUpper, tick sekarang. In range jika tickLower <= tick < tickUpper.
     Posisi di range (%) = (tick - tickLower)/(tickUpper - tickLower) * 100.
  2. MC range: tick -> harga token non-stable -> x total supply. MC bawah, atas, sekarang.
  3. Isi posisi: amount0, amount1 + unclaimed fee, harga live (DefiLlama).
  4. Entry: dari event IncreaseLiquidity (v4: ModifyLiquidity) tokenId ini. Jangan tanya user.
  5. V_LP, V_hold, IL%, Floating PnL = V_LP + unclaimed fee - nilai entry ($ dan %).
- Format notifikasi sesuai template user.
- Notif otomatis: OOR transition, harga <10% dari ujung range, OPEN/CLOSE.
- "data belum lengkap" kalau data gagal diambil, jangan menebak angka.
"""
import json, os, sys, subprocess, time, datetime, math, urllib.request

def get_wallet():
    """Wallet from --wallet flag or LP_WALLET env var."""
    if "--wallet" in sys.argv:
        idx = sys.argv.index("--wallet")
        if idx + 1 < len(sys.argv):
            return sys.argv[idx + 1]
    w = os.environ.get("LP_WALLET", "")
    if w:
        return w
    print("Usage: monitor.py --wallet 0x... [--report]", file=sys.stderr)
    print("   or: LP_WALLET=0x... python3 monitor.py", file=sys.stderr)
    sys.exit(2)

WALLET = None  # set in main()
BASE = os.path.dirname(os.path.abspath(__file__))
CHECKER = os.path.join(os.path.expanduser("~/workspace/lp/lp-checker"), "lp_checker.py")
STATE_PATH = os.path.join(BASE, "state.json")
NOTIFY_PATH = os.path.join(BASE, "notifications.log")
CHAINS = ["ethereum", "bsc", "base", "arbitrum", "optimism", "polygon", "robinhood"]

# Token total supply cache (fetched dynamically per token, not hardcoded)
_SUPPLY_CACHE = {}
_CHAINS_CACHE = None

def get_chain_rpc(chain_name):
    """Get first RPC URL for a chain from lp-checker chains.json."""
    global _CHAINS_CACHE
    if _CHAINS_CACHE is None:
        try:
            chains_path = os.path.join(os.path.dirname(CHECKER), "chains.json")
            with open(chains_path) as f:
                _CHAINS_CACHE = json.load(f)
        except Exception:
            _CHAINS_CACHE = {}
    cfg = _CHAINS_CACHE.get(chain_name, {})
    rpcs = cfg.get("rpcs", [])
    return rpcs[0] if rpcs else None

def get_token_supply(chain_rpc_url, token_address, decimals):
    """Fetch totalSupply from token contract. Returns float or None."""
    key = token_address.lower()
    if key in _SUPPLY_CACHE:
        return _SUPPLY_CACHE[key]
    try:
        import json as _json
        import urllib.request as _urlreq
        # totalSupply() = 0x18160ddd
        payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                   "params": [{"to": token_address, "data": "0x18160ddd"}, "latest"]}
        req = _urlreq.Request(chain_rpc_url,
                              data=_json.dumps(payload).encode(),
                              headers={"Content-Type": "application/json",
                                       "User-Agent": "Mozilla/5.0"})
        with _urlreq.urlopen(req, timeout=15) as r:
            d = _json.load(r)
        if "error" in d or not d.get("result"):
            return None
        raw = int(d["result"], 16)
        supply = raw / (10 ** decimals)
        _SUPPLY_CACHE[key] = supply
        return supply
    except Exception:
        return None

# DefiLlama chain slugs
LLAMA_CHAIN = {
    "ethereum": "ethereum", "bsc": "bsc", "base": "base",
    "arbitrum": "arbitrum", "optimism": "optimism",
    "polygon": "polygon", "robinhood": "robinhood",
}

def wib_now():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))

def log(msg):
    ts = wib_now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts} WIB] {msg}"
    print(line, flush=True)
    with open(NOTIFY_PATH, "a") as f:
        f.write(line + "\n")

def fetch_dexscreener_price(chain, address):
    """Fallback for tokens DefiLlama doesn't cover (e.g. INU on Robinhood)."""
    if chain != "robinhood":
        return None
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={address}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        for p in d.get("pairs", []):
            if "robinhood" in p.get("url", "").lower():
                price = p.get("priceUsd")
                if price:
                    return float(price)
        return None
    except Exception:
        return None

def fetch_llama_prices(items):
    """items: list of (chain, address). Returns dict {(chain,addr_lower): price_usd}.
    Primary: DefiLlama. Fallback: DexScreener (for tokens Llama misses, e.g. INU)."""
    out = {}
    if not items:
        return out
    coins = ",".join(f"{LLAMA_CHAIN.get(c, c)}:{a}" for c, a in items)
    url = f"https://coins.llama.fi/prices/current/{coins}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        for k, v in d.get("coins", {}).items():
            parts = k.split(":")
            if len(parts) == 2:
                out[(parts[0], parts[1].lower())] = v.get("price")
    except Exception as e:
        log(f"DefiLlama error: {e}")
    # Fallback: DexScreener for missing prices
    for chain, addr in items:
        key = (LLAMA_CHAIN.get(chain, chain), addr.lower())
        if key not in out or out[key] is None:
            fb = fetch_dexscreener_price(chain, addr)
            if fb:
                out[key] = fb
    return out

def run_checker():
    cmd = [sys.executable, CHECKER, WALLET, "--json", "--include-closed",
           "--chains", ",".join(CHAINS)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout)
    except Exception:
        return None

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"positions": {}, "history": []}

def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)

def pos_key(p):
    return f"{p.get('chain','?')}:{p.get('dex_key', p.get('dex','?'))}:{p.get('tokenId')}"

def is_fake(p):
    for field in ("symbol0", "symbol1", "pair"):
        v = p.get(field)
        if isinstance(v, str) and "!" in v:
            return True
    for tk in ("token0", "token1"):
        t = p.get(tk, {})
        if isinstance(t, dict) and "!" in str(t.get("symbol", "")):
            return True
    return False

def tick_to_price(tick):
    """1.0001^tick = amount1/amount0 (token1 per token0)."""
    return math.pow(1.0001, tick)

def fmt_mc(v):
    if v is None:
        return "?"
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.2f}"

def build_report(p, entry, prices):
    """Build the user-format report. Returns string or 'data belum lengkap'."""
    chain = p.get("chain", "?")
    dex = p.get("dex", "?")
    tid = p.get("tokenId", "?")
    pair = p.get("pair", "?")
    fee_pct = p.get("fee_tier_pct", "?")

    t0 = p.get("token0", {}) if isinstance(p.get("token0"), dict) else {}
    t1 = p.get("token1", {}) if isinstance(p.get("token1"), dict) else {}
    a0 = float(p.get("amount0") or 0)
    a1 = float(p.get("amount1") or 0)
    f0 = float(p.get("fees0") or 0)
    f1 = float(p.get("fees1") or 0)

    # Live prices from DefiLlama (fallback to checker usd)
    addr0 = t0.get("address", "").lower()
    addr1 = t1.get("address", "").lower()
    p0 = prices.get((LLAMA_CHAIN.get(chain, chain), addr0)) or t0.get("usd")
    p1 = prices.get((LLAMA_CHAIN.get(chain, chain), addr1)) or t1.get("usd")
    try:
        p0 = float(p0) if p0 else None
        p1 = float(p1) if p1 else None
    except (TypeError, ValueError):
        p0 = p1 = None

    if p0 is None or p1 is None:
        return None  # data belum lengkap

    # Range
    tl = p.get("tick_lower")
    tu = p.get("tick_upper")
    tick = p.get("current_tick")
    if tl is None or tu is None or tick is None:
        return None
    in_range = tl <= tick < tu
    range_pct = (tick - tl) / (tu - tl) * 100 if tu != tl else 0

    # MC range: use pool prices directly (token1 per token0) -> token1 USD price -> x supply
    # price_lower/upper/current_price are "token1 per token0" from the checker
    # Supply is fetched dynamically from the token contract (not hardcoded)
    try:
        pl = float(p.get("price_lower") or 0)
        ph = float(p.get("price_upper") or 0)
        pn = float(p.get("current_price") or 0)
        # token1 price in USD = token0_price / (token1 per token0)
        # Fetch token1 total supply dynamically
        t1_addr = t1.get("address", "")
        t1_dec = t1.get("decimals", 18)
        try:
            t1_dec = int(t1_dec)
        except (TypeError, ValueError):
            t1_dec = 18
        rpc_url = get_chain_rpc(chain)
        supply = get_token_supply(rpc_url, t1_addr, t1_dec) if rpc_url and t1_addr else None
        if supply and pl:
            mc_low = (p0 / pl * supply)
        else:
            mc_low = None
        mc_high = (p0 / ph * supply) if (supply and ph) else None
        mc_now = (p0 / pn * supply) if (supply and pn) else None
    except (ZeroDivisionError, TypeError, ValueError):
        mc_low = mc_high = mc_now = None

    # Values
    v_lp = a0 * p0 + a1 * p1
    fees_usd = f0 * p0 + f1 * p1

    # Entry / IL / PnL
    entry_usd = entry.get("entry_usd")
    e_a0 = entry.get("amount0_setor") or 0
    e_a1 = entry.get("amount1_setor") or 0
    il_str = "?"
    pnl_str = "?"
    if entry_usd:
        v_hold = e_a0 * p0 + e_a1 * p1
        il_pct = ((v_lp - v_hold) / v_hold * 100) if v_hold else 0
        total_fees = fees_usd + (entry.get("fees_collected_usd") or 0)
        pnl = v_lp + total_fees - entry_usd - (entry.get("gas_usd") or 0)
        pnl_pct = (pnl / entry_usd * 100) if entry_usd else 0
        il_str = f"{il_pct:+.2f}%"
        pnl_str = f"{pnl:+.2f} ({pnl_pct:+.1f}%)"
    else:
        il_str = "data belum lengkap"
        pnl_str = "data belum lengkap"

    # Status line
    if in_range:
        emoji = "\U0001F7E2"
        status = "IN RANGE"
    else:
        emoji = "\U0001F534"
        status = "OOR ⬇️ di bawah" if tick < tl else "OOR ⬆️ di atas"

    # Range bar: |———●———|
    bar_len = 20
    pos = max(0, min(bar_len, int(range_pct / 100 * bar_len)))
    bar = "|" + "—" * pos + "●" + "—" * (bar_len - pos) + "|"

    jam = wib_now().strftime("%H:%M")

    lines = [
        f"{emoji} {pair} · {fee_pct}% · {dex} · {chain.capitalize()} · #{tid}",
        f"{status} · {range_pct:.1f}%",
        bar,
        f"MC {fmt_mc(mc_low)} → {fmt_mc(mc_high)} · now {fmt_mc(mc_now)}",
        f"Value ${v_lp:.2f} · Fees ${fees_usd:.2f}",
        f"IL {il_str} · Float PnL ${pnl_str}",
        f"\U0001F553 {jam} WIB",
    ]
    return "\n".join(lines)

def main():
    global WALLET
    WALLET = get_wallet()
    data = run_checker()
    if data is None:
        log("data belum lengkap (checker gagal)")
        return 1

    positions = [p for p in data.get("positions", []) if not is_fake(p)]
    state = load_state()
    prev = state["positions"]

    # Collect tokens for DefiLlama batch price fetch
    price_items = []
    for p in positions:
        chain = p.get("chain", "")
        for tk in ("token0", "token1"):
            t = p.get(tk, {})
            if isinstance(t, dict) and t.get("address"):
                price_items.append((chain, t["address"]))
    prices = fetch_llama_prices(list(set(price_items)))

    current_keys = set()
    for p in positions:
        key = pos_key(p)
        current_keys.add(key)

        liq = p.get("liquidity", "0")
        try:
            liq_f = float(liq)
        except (TypeError, ValueError):
            liq_f = 0
        closed = p.get("closed", False) or liq_f == 0
        in_range = p.get("in_range")

        if key not in prev:
            if closed:
                continue
            # OPEN — record entry (amounts now; on-chain lookup for pre-existing done manually)
            t0 = p.get("token0", {}) if isinstance(p.get("token0"), dict) else {}
            t1 = p.get("token1", {}) if isinstance(p.get("token1"), dict) else {}
            a0 = float(p.get("amount0") or 0)
            a1 = float(p.get("amount1") or 0)
            addr0 = t0.get("address", "").lower()
            addr1 = t1.get("address", "").lower()
            chain = p.get("chain", "")
            p0 = prices.get((LLAMA_CHAIN.get(chain, chain), addr0)) or t0.get("usd")
            p1 = prices.get((LLAMA_CHAIN.get(chain, chain), addr1)) or t1.get("usd")
            try:
                p0f = float(p0) if p0 else 0
                p1f = float(p1) if p1 else 0
            except (TypeError, ValueError):
                p0f = p1f = 0
            entry = {
                "chain": p.get("chain"), "dex": p.get("dex"), "tokenId": p.get("tokenId"),
                "pair": p.get("pair"),
                "amount0_setor": a0, "amount1_setor": a1,
                "price0_entry": p0f or None, "price1_entry": p1f or None,
                "entry_usd": a0 * p0f + a1 * p1f,
                "entry_time": wib_now().isoformat(),
                "entry_source": "auto-detect",
                "gas_usd": 0, "fees_collected_usd": 0,
                "last_in_range": in_range, "last_liquidity": liq,
                "last_range_pct": None,
            }
            state["positions"][key] = entry
            report = build_report(p, entry, prices)
            log(f"OPEN:\n{report if report else 'data belum lengkap'}")
            continue

        entry = prev[key]
        # Range transition + edge warning
        old_range = entry.get("last_in_range")
        old_pct = entry.get("last_range_pct")

        tl = p.get("tick_lower")
        tu = p.get("tick_upper")
        tick = p.get("current_tick")
        range_pct = None
        if tl is not None and tu is not None and tick is not None and tu != tl:
            range_pct = (tick - tl) / (tu - tl) * 100

        entry["last_liquidity"] = liq
        entry["last_in_range"] = in_range
        entry["last_range_pct"] = range_pct

        if closed:
            # CLOSE — final PnL
            report = build_report(p, entry, prices)
            state["history"].append({**entry, "close_time": wib_now().isoformat()})
            del state["positions"][key]
            log(f"CLOSE:\n{report if report else 'data belum lengkap'}")
            continue

        # ADD/REMOVE
        prev_liq = entry.get("last_liquidity", "0")
        try:
            prev_liq_f = float(prev_liq)
        except (TypeError, ValueError):
            prev_liq_f = 0
        liq_changed = abs(liq_f - prev_liq_f) > 1e-9 * max(1, prev_liq_f)

        notify = False
        reason = ""
        if old_range is not None and in_range is not None and old_range != in_range:
            notify = True
            reason = "range transition"
        elif range_pct is not None:
            # Edge warning: <10% from either end
            if range_pct < 10 or range_pct > 90:
                # Only notify on crossing into the warning zone
                if old_pct is None or not (old_pct < 10 or old_pct > 90):
                    notify = True
                    reason = "mendekati batas range"
        if liq_changed and prev_liq_f > 0:
            notify = True
            reason = "ADD" if liq_f > prev_liq_f else "REMOVE"

        if notify:
            report = build_report(p, entry, prices)
            log(f"{reason.upper()}:\n{report if report else 'data belum lengkap'}")

        state["positions"][key] = entry

    # Disappeared tokens → CLOSE
    for key in list(prev.keys()):
        if key not in current_keys:
            entry = state["positions"].pop(key)
            entry["close_time"] = wib_now().isoformat()
            entry["close_note"] = "tokenId hilang dari wallet"
            state["history"].append(entry)
            log(f"CLOSE (hilang): {entry.get('pair')} #{entry.get('tokenId')}")

    save_state(state)

    # "cek" mode: if --report flag, print all positions
    if "--report" in sys.argv:
        for key, entry in state["positions"].items():
            # Find matching position data
            for p in positions:
                if pos_key(p) == key:
                    report = build_report(p, entry, prices)
                    print(report if report else "data belum lengkap")
                    print()
                    break
    else:
        log(f"OK: {len(state['positions'])} open, {len(state['history'])} history.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
